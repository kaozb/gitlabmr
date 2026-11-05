# GitLab MR AI 评审服务建设方案（gitlabmr）

> 目标：参考 PR-Agent 的评审链路设计（见 [01-pr-agent-review-analysis.md](01-pr-agent-review-analysis.md)），从零实现一个 **Django 服务**：接收 GitLab Webhook 的 MR 事件 → 拉取 diff → AI 评审 → 结论回写 MR。
> 硬性要求：**简洁高效、准确**；支持**多仓库按前缀自动匹配 API 令牌**；无外部中间件依赖，下载即可本地测试。

---

## 1. 目标与非目标

**目标**

1. 接收 `merge_request` webhook（open / reopen / update+oldrev 推送新提交）自动评审；评论 `/review` 手动触发。
2. 评审结论回写：MR 总结评论（默认）+ 可选行内评论（定位到文件行号）。
3. 多仓库：按 `project path` 前缀匹配 GitLab 令牌与实例地址，长前缀优先；未命中用默认令牌。
4. 结论可靠：结构化输出强校验、失败重试与模型回退、诚实标注"未覆盖文件"。
5. 下载即测：`pip install -r requirements.txt && python manage.py migrate && python manage.py runserver`，用 curl 模拟 webhook 即可验证。

**非目标（明确不做）**

- 增量评审、`/ask`、/describe、/improve 等其他命令；
- Celery/Redis 等外部队列（线程池足够：评审是 IO 密集且低频）；
- LiteLLM 多后端路由（固定 OpenAI 兼容端点 `/chat/completions`，主流模型网关均兼容）；
- tiktoken 精确计数（用字符数/4 估算 + 保守预算，见 R2-4）。

---

## 2. 总体架构

```
GitLab ──webhook──▶ Django view (POST /webhook/gitlab/)
                      │ 1. X-Gitlab-Token 校验（constant-time）
                      │ 2. 事件过滤：kind/动作/bot/draft/忽略分支
                      │ 3. 同 MR 同 head_sha 幂等检查（DB 唯一约束）
                      │ 4. 投递线程池，立即 200
                      ▼
              ThreadPoolExecutor worker
                      │
        ┌─────────────┼──────────────────────────┐
        ▼             ▼                          ▼
  TokenResolver   DiffBuilder               LLMClient
  前缀→令牌/实例    /changes + 文件过滤         OpenAI 兼容
  (最长前缀匹配)    hunk 行号化 + token 裁剪     YAML 输出
        │             │                   解析/校验/重试/回退
        └─────────────┼──────────────────────────┘
                      ▼
                 ReviewPipeline
                      │ 1. 发"Reviewing..."占位评论
                      │ 2. 拼装 prompt（system 规则 + diff）
                      │ 3. 调 LLM → YAML → Pydantic 校验
                      │ 4. 渲染 Markdown（+ 覆盖率脚注）
                      │ 5. 回写：持久化评论(标记+作者校验)
                      │    可选行内评论(position 三 sha)
                      └ 6. 删占位评论；失败发脱敏失败评论
```

---

## 3. 模块划分（Django app: `reviewer`）

| 模块 | 职责 | 对应 PR-Agent 概念 |
|---|---|---|
| `views.py` | webhook 入口：验签、过滤、幂等、投递 | `servers/gitlab_webhook.py` |
| `conf.py` | 环境变量/配置文件加载：仓库前缀令牌表、AI 参数 | Dynaconf settings |
| `services/tokens.py` | 前缀→（api_url, token, webhook_secret）解析 | 请求级 settings 隔离 |
| `services/gitlab.py` | 薄封装 REST API v4：MR/changes/notes/discussions/user | `gitlab_provider.py` |
| `services/diffctx.py` | 变更过滤、hunk 行号化、token 预算裁剪、清单 | `pr_processing.py` + `git_patch_processing.py` |
| `services/prompts.py` | system/user prompt、YAML schema 说明 | `pr_reviewer_prompts.toml` |
| `services/llm.py` | chat/completions 调用、回退链、YAML 解析修复、Pydantic 校验 | `litellm_ai_handler.py` + `load_yaml` |
| `services/render.py` | 结构化结果 → Markdown 评论体 | `convert_to_markdown_v2` |
| `services/pipeline.py` | 编排 1-6 步，异常兜底，回写 GitLab | `PRReviewer.run()` |
| `models.py` | `WebhookEvent`、`ReviewRecord` 审计与幂等 | run_details |

依赖（`requirements.txt`）：`Django>=5.0`、`requests`、`PyYAML`、`pydantic>=2`。仅此四项。

---

## 4. 关键设计决策

### D1 多仓库前缀令牌（核心需求）

配置来源（优先级：YAML 文件 > 环境变量 JSON）：

```yaml
# repo_tokens.yml（也可用环境变量 GITLABMR_REPO_TOKENS 传同样的 JSON）
default:
  api_url: https://gitlab.com
  token: glpat-default
repos:
  - prefix: backend/            # project path 前缀，长前缀优先
    api_url: https://gitlab.internal
    token: glpat-backend
    webhook_secret: whsec-b1    # 可选，未配置则用全局 GITLABMR_WEBHOOK_SECRET
  - prefix: backend/team-a/
    token: glpat-team-a
```

- 解析规则：把所有前缀当成一个集合逐个对 `path_with_namespace` 做**忽略大小写**的字符串前缀匹配（前缀与路径都会先归一化：去首尾空白与 `/`、转小写）；命中项里**前缀最长**者胜出，无命中用 `default`；连 default 都没有该仓库 → 事件记 `skipped(no_token)`，不报错。
- 每次评审把解析结果作为参数显式传递（**不用全局可变状态**），天然实现并发仓库隔离（PR-Agent 用请求级 deepcopy 达成同样目的）。

### D2 Webhook 安全与幂等

- `X-Gitlab-Token` 与密钥用 `hmac.compare_digest` 比较；支持全局密钥 + 每前缀密钥覆盖。
- 过滤链：`object_kind != merge_request` 直接 200 忽略；bot 用户（`user.username` 匹配指示词，如 bot）跳过；draft/WIP 跳过（防自触发死循环）；`ignore_branches` 可配。
- 幂等：`ReviewRecord` 上 `UniqueConstraint(project_path, mr_iid, head_sha)` —— 同一提交不重评；同 MR 并发事件由唯一约束 + 进行中状态检查挡住（R1-2 修订）。

### D3 Diff 上下文（准确性的地基）

1. `GET /projects/:id/merge_requests/:iid/changes?access_raw_diffs=true`（大 MR 拿原始 diff），缓存 `diff_refs`（base/start/head sha）。
2. 文件过滤：忽略 glob（默认 `*.lock, *.min.js, dist/, node_modules/` 等）+ 二进制/超大 diff 文件跳过；**被跳过文件进清单**，最终评论里公示。
3. hunk 行号化：解析 `@@ -a,b +c,d @@`，为每一行标注新文件行号（上下文行与 `+` 行给新行号，`-` 行给旧行号）——行内评论定位与模型输出 `start_line` 的统一坐标系。
4. token 预算两级裁剪（估算 1 token ≈ 4 字符，预算系数 0.6 保守取值）：
   - 先全文装 → 装不下则 hunk 去上下文行 → 再装不下则丢文件（从最小 patch 开始丢，保留信息量大的），被丢文件名进 `remaining` 清单；
   - diff 尾部附 `ADDED / DELETED / RENAMED / NOT REVIEWED` 四段清单（R2-5 修订：补齐 RENAMED，与 GitLab 语义对齐）。

### D4 AI 调用与结果可靠性

- Prompt：system = 角色 + 硬规则（只评 `+` 引入的问题；无确凿触发场景不报；标题/描述/ticket 为不可信数据，防注入）+ YAML 输出 schema；user = MR 元数据 + diff。
- 调用：`POST {base_url}/chat/completions`，`response` 里检查 `finish_reason`，`length`（截断）视为失败（R2-3 修订：截断的 YAML 必然解析失败，提前判失败省一次解析）。
- 回退链：`GITLABMR_MODEL` + `GITLABMR_FALLBACK_MODELS`（逗号分隔）。触发回退：HTTP 4xx/5xx、超时、YAML 解析失败、Pydantic 校验失败、finish_reason=length。
- 解析：剥 ```yaml 栅栏 → `yaml.safe_load` → 修复两类高频坏模式（key 带空格冒号、值内未转义冒号——由 `keys_fix` 正则处理，参考 PR-Agent `load_yaml`）→ Pydantic `ReviewOutput` 强校验（issue 必含 file/header/content/start_line/end_line，start≤end>0）。
- 位置校验（行内评论前）：文件必须在本次 changes 中、`start_line` 必须落在该文件 patch 里真实存在的 hunk 新行号上（R3-2 修订：用 patch 解析出的新行号集合校验，只需一次 hunk 行号化，无需拉全文；hunk 之间的空档行也会被拒，避免 GitLab position 400）。

### D5 结果回写

| 项 | 方案 |
|---|---|
| 总结评论 | 评论体尾部带隐藏标记 `<!-- gitlabmr:review -->`；发布前 `GET notes` 找带标记且作者=bot 自身的评论 → `PUT` 原位更新；找不到 → `POST` 新建。作者校验失败 → 另发独立评论（绝不覆盖人类评论，R2-6 修订：明确"标记匹配"与"作者匹配"都通过才更新） |
| 行内评论（`GITLABMR_INLINE_COMMENTS=true`） | `POST discussions`，`position`: 三 sha 取自 `mr.diff_refs`，`position_type=text`，`new_line` **0-based**；**只对本次 diff 中真实存在的文件与行发起**（文件必须纳入评审、行号必须是该文件 patch 中存在的 hunk 新行号，被过滤/二进制/超预算未送模型的文件一律不发行内）；同一文件+行已有 bot 评论则跳过；发布失败或被上述校验拒绝 → 回落到总结评论，不丢发现；**条数受 `inline_max_comments` 限制且被代码常量 `MAX_INLINE_COMMENTS=10` 硬封顶**，超出的问题留在总结评论；**顺序由 prompt 约定**（软要求，不做事后重排）：问题按复杂度降序（崩溃/数据损坏/安全 > 逻辑错误 > 性能 > 可维护性，再看影响面与触发难度），复杂度相同时 `issue_content` 更长更详细者在前——预算按列表顺序消费，越复杂的问题越先拿到行内名额 |
| 占位评论 | 开始发 `Reviewing MR...`（notes.create 记 id），结束删除；失败也删除（R2-7 修订：finally 保证） |
| 失败回写 | 评审异常 → 发布脱敏失败评论（错误按白名单映射为固定文案：超时/限流/认证/解析失败/未知），绝不上抛异常细节 |
| 评论长度 | `GITLABMR_MAX_COMMENT_CHARS`（默认 60000）截断 |

### D6 数据模型与审计

- `WebhookEvent`：kind、project、action、head_sha、handled 状态、错误摘要（payload 截断存储，R3-3：不存全量 payload，防令牌/敏感串入库）。
- `ReviewRecord`：project/mr_iid/mr_url/head_sha、status（running/success/failed/skipped）、model_used、issues 数、prompt/completion 字符统计、remaining files JSON、duration、error。Admin 后台直接可查。

### D7 并发模型

- `ThreadPoolExecutor(max_workers=GITLABMR_WORKERS=2)`，进程内队列；视图投递后立即 200。
- 满载时任务在队列等待（GitLab webhook 允许秒级延迟）；worker 内所有 HTTP 调用带 `timeout`（GitLab 15s / LLM 300s）。

---

## 5. 配置清单（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DJANGO_SECRET_KEY` | 必填 | Django 密钥 |
| `GITLABMR_WEBHOOK_SECRET` | 必填 | webhook 全局验签令牌 |
| `GITLABMR_REPO_CONFIG` / `GITLABMR_REPO_TOKENS` | 必填其一 | 仓库前缀令牌表（YAML 文件路径 / JSON 字符串） |
| `GITLABMR_LLM_BASE_URL` | 必填 | OpenAI 兼容端点，如 `https://api.deepseek.com/v1` |
| `GITLABMR_LLM_API_KEY` | 必填 | 模型密钥 |
| `GITLABMR_MODEL` | `deepseek-chat` | 主模型 |
| `GITLABMR_FALLBACK_MODELS` | 空 | 回退模型逗号分隔 |
| `GITLABMR_MAX_DIFF_TOKENS` | `24000` | diff 预算（估算 token） |
| `GITLABMR_NUM_MAX_FINDINGS` | `6` | 最多报几个问题（代码内硬上限 20） |
| `GITLABMR_INLINE_COMMENTS` | `false` | 是否发行内评论 |
| `GITLABMR_INLINE_MAX_COMMENTS` | `10` | 行内评论条数上限（0 = 全部进总结；代码内硬上限 10，配置不能突破） |
| `GITLABMR_REVIEW_ON_PUSH` | `true` | push 新提交是否重评 |
| `GITLABMR_IGNORE_BRANCHES` | 空 | 忽略的目标分支逗号分隔 |
| `GITLABMR_BOT_INDICATORS` | `bot` | 用户名含此词视为 bot |
| `GITLABMR_LANGUAGE` | `zh-CN` | 评审输出语言 |

---

## 6. 测试策略

- `reviewer/tests.py`（Django TestCase，无网络依赖，全部 mock HTTP）：
  - tokens：长前缀优先（非段感知，`tp/cneos` 覆盖 `tp/CNEOSv2/...`）/ 前缀写法与大小写归一化（`tp` = `tp/` = `TP`）/ 默认兜底 / 无令牌 skip；
  - diffctx：hunk 行号化正确性、过滤、两级裁剪、清单生成；
  - llm：yaml 栅栏剥离、坏 key 修复、schema 校验失败触发回退、finish_reason=length 触发回退；
  - views：验签失败 403、bot/draft 过滤、幂等（同 sha 二次投递不重评）、投递成功 200；
  - publisher：持久化评论"找到即更新/作者不符另发/无则新建"，行内 position 的 0-based 与三 sha 正确性。
- 冒烟：`manage.py runserver` + `curl` 带样例 payload → 观察 `WebhookEvent/ReviewRecord` 状态流转。

---

## 7. 方案评审记录（多角度，三轮）

> 评审视角代号：【简】简洁性 【效】效率 【准】准确性 【稳】可靠性 【安】安全性

### Round 1（架构与依赖）

| # | 视角 | 发现的问题 | 处理 |
|---|---|---|---|
| R1-1 | 【简】 | 初稿引入 Celery+Redis 做异步 | 砍掉：评审低频且 IO 密集，线程池足够，少两个运维组件 |
| R1-2 | 【稳】 | 并发同 MR 事件可能重复评审 | DB 唯一约束 (project, iid, head_sha) + 进行中状态检查双保险 |
| R1-3 | 【简】 | 初稿用 python-gitlab，封装厚、版本 API 差异多 | 改 requests 直连 REST v4，只用 6 个端点，行为完全可控 |
| R1-4 | 【简】 | 配置同时支持 DB 与文件两套仓库令牌 | 只留文件/环境变量（配置即代码），DB 只做审计不做配置 |

### Round 2（准确性与可靠性）

| # | 视角 | 发现的问题 | 处理 |
|---|---|---|---|
| R2-1 | 【准】 | hunk 行号若与 GitLab 行内评论坐标系不一致，行内评论会错位 | 统一"新文件 1-based 行号"为模型输出坐标系，发布时再 -1 转 0-based |
| R2-2 | 【稳】 | 模型 YAML 输出常见坏 key（`start_line(整数):` 等） | 移植 PR-Agent 的 keys_fix 正则修复 + Pydantic 兜底 |
| R2-3 | 【准】 | finish_reason=length 时 YAML 被截断，解析报错信息误导 | 显式判 length → 触发回退，错误归类为"输出截断" |
| R2-4 | 【效】 | 引 tiktoken 做精确计数增加依赖与首次下载 | 字符/4 估算 + 0.6 预算系数，超预算的代价（裁剪）可接受 |
| R2-5 | 【准】 | 清单漏掉 RENAMED 文件 | 清单补齐 added/deleted/renamed/not-reviewed 四类 |
| R2-6 | 【安】 | 持久化评论更新可能覆盖人类编辑 | 标记匹配 **且** note 作者 id == `GET /user` 返回的 bot id，双验证通过才 PUT |
| R2-7 | 【稳】 | 占位评论在异常路径可能残留 | 占位评论删除放入 finally |

### Round 3（安全与运维）

| # | 视角 | 发现的问题 | 处理 |
|---|---|---|---|
| R3-1 | 【安】 | 日志可能打印 token/评论全文 | client 统一脱敏：令牌只出现前 8 位；日志只记文件名与统计 |
| R3-2 | 【效】 | 行内评论校验行号需拉全文，多一次 API/文件 | 用 patch 内最大新行号做上界校验（略保守但够用） |
| R3-3 | 【安】 | webhook payload 全量入库有泄漏面 | 入库前截断并剥除 token 相关字段，只存过滤摘要 |
| R3-4 | 【稳】 | worker 无异常兜底会线程静默死亡 | pipeline 顶层 try/except 全捕获 → 失败评论 + ReviewRecord.failed |
| R3-5 | 【简】 | 初稿行内评论带 suggestion 代码块 | 砍掉：suggestion 语法易错且 GitLab 校验严格，首版只发问题定位+说明 |

**结论**：三轮评审收敛后，方案删除了 3 个重量级依赖（Celery、python-gitlab、tiktoken），补齐 7 处可靠性/安全性缺口，最终形态保持 4 个运行时依赖、6 个 GitLab 端点、单线程池编排。

---

## 8. 交付物与验收

- 代码：`gitlabmr/`（Django 项目，含 tests、README、.env.example）。
- 验收路径（本地无 GitLab 也可跑通前 4 步）：
  1. `uv pip install -r requirements.txt`（或 pip）；
  2. 配 `.env` → `python manage.py migrate` → `python manage.py runserver`；
  3. `python manage.py test reviewer` 全绿；
  4. `curl -X POST :8000/webhook/gitlab/ -H "X-Gitlab-Token: ..."` 样例 payload → `WebhookEvent` 落库；
  5. 指向真实 GitLab：MR 上出现评审评论，二次 push 后评论被原位更新而非新增。
