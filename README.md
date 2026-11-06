# gitlabmr — GitLab MR AI 评审服务

参考 [PR-Agent](https://github.com/qodo-ai/pr-agent) 的评审链路设计，用 Django 从零实现的轻量 GitLab MR AI 评审服务：接收 Webhook → 拉取 MR diff → LLM 结构化评审 → 结论回写 MR（总结评论 + 可选行内评论）。支持**多仓库按 project path 前缀自动匹配 API 令牌**。

- 设计与分析文档：[docs/01-pr-agent-review-analysis.md](docs/01-pr-agent-review-analysis.md)（PR-Agent 链路分析）、[docs/02-design.md](docs/02-design.md)（建设方案与评审记录）

## 架构一图

```
GitLab webhook ─▶ 验签/过滤/去重 ─▶ 线程池 ─▶ TokenResolver(前缀→令牌)
                                            ─▶ DiffBuilder(行号化+预算裁剪)
                                            ─▶ LLM(YAML schema+回退链)
                                            ─▶ 回写(持久化评论/行内评论/失败评论)
```

## 快速开始

```bash
cd gitlabmr
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 按注释填写
# export $(grep -v '^#' .env | xargs)   # 或使用 direnv 等工具加载
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

跑测试（全离线，无网络依赖）：

```bash
python manage.py test reviewer -v 1
```

## 首页控制台（Web UI）

启动后打开 `http://<your-host>:8000/` 即是控制台首页：

- **评审总览**：成功/失败/进行中/跳过统计、最近 50 条评审记录（状态、评分、风险、问题数、模型、耗时）、最近 20 条 Webhook 事件，并可**手动触发评审**（直接把浏览器地址栏的 MR 链接粘进输入框即可，也支持 `group/repo!42` 简写）与**一键重试失败评审**（点进详情页）。
- **服务设置**（`/settings/`）：直接在网页上配置全局项（Webhook 密钥、默认 GitLab 地址/令牌、LLM 接口与模型、回退模型、diff 预算、行内评论、忽略分支等）以及**多仓库前缀令牌**（增删改、启用/停用；列表默认只展示表格，点「＋ 新增前缀」或某行「编辑」才展开表单；编辑态所有字段（含前缀本身）都可修改，改前缀等于按记录 ID 重命名，与其他前缀冲突时会被拒绝）。
- **立即生效**：设置保存在数据库中，优先于环境变量；文本/数字留空 = 沿用环境变量，密钥留空 = 保持不变。配置缓存约 10 秒自动刷新，无需重启进程（`workers` 除外，改后需重启）。数据库令牌表非空时整体取代 env/文件令牌，清空则回落到 env/文件配置。
- **访问保护**：设置环境变量 `GITLABMR_UI_PASSWORD` 后，控制台需要密码登录（session 会话）；不设置则免登录（仅建议内网使用）。密钥在页面中只显示掩码（`glpa****56`），不会回显明文。

## 多仓库前缀令牌

`repo_tokens.yml`（或环境变量 `GITLABMR_REPO_TOKENS` 内联 JSON）：

```yaml
default:                      # 未命中前缀时兜底（可省略，省略则未知仓库被忽略）
  api_url: https://gitlab.com
  token: glpat-default
repos:
  - prefix: backend/          # 匹配 project path 前缀，纯字符串前缀，长前缀优先
    api_url: https://gitlab.example.com
    token: glpat-backend
    webhook_secret: whsec-b1  # 可选：该仓库 webhook 的独立令牌
  - prefix: backend/team-a/
    token: glpat-team-a
```

- 所有前缀视为一个集合，逐个对 MR 的项目路径做**忽略大小写**的字符串前缀匹配，命中项里**前缀最长**的那条胜出。
- 前缀写法等价：`tp`、`tp/`、`TP` 都能命中 `tp/CNEOSv2/NetF/npp/npp.v2.0`；`tp/CNE` 同样命中（非段边界也匹配）。
- `backend/team-a/svc` 命中 `backend/team-a/`，`backend/pay` 命中 `backend/`；只配了 `backend` 时 `backendx/svc` 也会命中 `backend`——需要更精确就把更长、更具体的前缀加进集合（最长者优先）。
- 每次评审将解析出的令牌作为参数显式传递，无全局可变状态，多仓库并发天然隔离。
- 除了 env/文件，也可以直接在控制台「服务设置」页维护这批令牌（存数据库，优先级更高，见上文）。

## 手动触发评审

控制台首页的输入框只认一样东西：**MR 链接**。从浏览器地址栏复制整条 URL 粘上去提交即可，无需拆分项目路径和 IID：

```
https://gitlab.inone.nsfocus.com/tp/CNEOSv2/OS/kernel/linux-5.4.18-kylinOS/-/merge_requests/118
```

- 兼容 `.../merge_requests/118/diffs`、带 `?query`/`#fragment` 的链接、旧式无 `/-/` 的链接。
- 也接受简写 `group/sub/repo!118`（`#118`、空格分隔同样识别），方便从聊天里复制。
- 链接里的域名会与命中令牌的 `api_url` 比对，不一致直接拒绝，避免把评审发到另一个 GitLab 实例；项目路径保留原始大小写，与 Webhook 记录的评审身份一致（同一 head sha 不会重复评审）。

## GitLab 侧配置

1. 为每个（组）仓库建 Webhook：`http://<your-host>/tech_powered/code_review/`，Trigger 勾选 *Merge request events* 与 *Note events*，Secret token 填 `GITLABMR_WEBHOOK_SECRET`（或该仓库自己的 `webhook_secret`）。**两边都留空 = 不验签**（仅建议可信内网使用）。
2. 为每个前缀准备 Personal Access Token（需要 `api` 权限，能读 MR/写评论）。
3. MR 打开/重开/推送新提交会自动评审；在 MR 里评论 `/review` 可手动触发。

## 评审结论回写行为

- 总结评论：默认**新建**；后续评审如果找到"带 `<!-- gitlabmr:review -->` 标记且作者为本 bot 账号"的历史评论，会**原位更新**（绝不覆盖人工评论）。
- 行内评论（`GITLABMR_INLINE_COMMENTS=true`）：**只对本次 diff 里真实存在的文件与行**发起（文件必须被纳入评审，行号必须落在该文件的 hunk 行上；被忽略规则过滤、二进制、纯删除或超出 token 预算未送进模型的文件一律不发行内评论），position 使用 GitLab 的 0-based 行号、三 sha 取自 `diff_refs`，同一位置已有 bot 评论则跳过；不满足条件或发布失败的问题回落到总结评论，不会丢失。**条数上限**由 `GITLABMR_INLINE_MAX_COMMENTS`（默认 10）控制，代码内还有 `MAX_INLINE_COMMENTS=10` 的硬封顶——把 `num_max_findings` 调多大都不会刷屏，超出的问题留在总结评论里（设 0 = 不发行内评论）。
- 行内评论的**选取顺序**由 prompt 约束（软要求，不做事后重排）：问题按复杂度从高到低排序（崩溃/数据损坏/安全 > 逻辑错误 > 性能 > 可维护性，并考虑影响范围与触发难度），复杂度相同时把 `issue_content` 更长、更详细的一条放前面；预算是按这个顺序消费的，因此越复杂的问题越先拿到行内评论名额，其余进总结评论。
- 失败回写：LLM/GitLab 出错时在 MR 发一条脱敏失败评论（错误按白名单归类，不泄漏堆栈）。
- 幂等：同一 MR 同一 head sha 只评审一次（数据库唯一约束），failed 的记录重试时自动重置。

## 上下文增强与证据约束（第一层）

diff 之外的仓库内容模型默认不可见，这会产生两类典型误判：把"在 diff 外有调用方"的代码判成未使用（误报），以及漏掉"签名改了但调用方没同步"（漏报）。本服务用**纯 GitLab API**（无需克隆仓库）收窄盲区：

- **全文补充**：对纳入评审的变更文件（patch 小的优先，`GITLABMR_CONTEXT_MAX_FILES` 控制），通过 `repository files/raw` API 在 MR head sha 处拉取**带行号的全文**注入提示词，模型可以核对整个文件的 import/定义，并能以 `path:行号` 引用证据。
- **引用搜索**：从 diff 新增行提取定义符号（`def/class/function/func/...` 及新文件名，`GITLABMR_CONTEXT_MAX_SYMBOLS` 控制），逐个走 `search?scope=blobs` API 查全库引用，把"是否被使用"从猜测变成模型眼前的事实。自建 GitLab 未启用高级搜索时自动跳过（400/403 识别），不影响其余流程。
- **提示词边界声明**：system prompt 明确告知模型"你只能看到 patch 与 API 提供的上下文"，凡是关于仓库全局状态的断言（未使用/缺少定义/调用方未更新/缺少注册）**必须引用所给上下文中的证据**，否则不得输出；只针对变更行本身的建议（逻辑、边界、错误处理）不受此限。
- **输出契约**：每条问题带 `kind`（`verified`=引用了证据的全局断言 / `suggestion`=仅基于 diff 的建议）与 `evidence` 字段；总结评论中 `verified` 问题会附上"证据 Evidence"行。
- **代码级兜底**：LLM 返回后做后置过滤——匹配"未使用/未被调用/未定义/不存在"类**缺失断言**但 `kind != verified` 或证据为 `patch-only` 的问题直接丢弃（可配置的 `suggestion` 类建议不受影响），防止模型过度自信时臆断。

全部为尽力而为：增强失败自动退化为仅 diff 评审，`GITLABMR_CONTEXT_ENRICHMENT=false` 可整体关闭。

## 环境变量

见 [.env.example](.env.example)，全部条目均有注释。这些变量只是**默认值**：控制台「服务设置」里保存过的项优先于环境变量（留空则继续沿用环境变量）。

## 目录结构

```
gitlabmr/
├── config/            # Django 工程配置
├── reviewer/          # 业务 app
│   ├── conf.py        # 配置加载：env/文件默认 + 数据库覆盖（控制台设置）
│   ├── models.py      # WebhookEvent / ReviewRecord（幂等闸门）/ Setting / RepoTokenEntry
│   ├── views.py       # webhook 端点（验签、过滤、投递）
│   ├── ui.py          # 控制台：总览 / 设置 / 详情 / 重试 / 手动触发 / 登录
│   ├── templates/     # 控制台页面（base/dashboard/settings/review_detail/login）
│   ├── tests.py       # 离线单测
│   └── services/
│       ├── tokens.py      # 前缀→令牌解析
│       ├── gitlab.py      # GitLab REST v4 薄客户端
│       ├── diffctx.py     # diff 行号化 + token 预算裁剪
│       ├── repoctx.py     # API 上下文增强：全文拉取 + 符号引用搜索
│       ├── prompts.py     # system/user prompt
│       ├── llm.py         # LLM 调用 + YAML 解析校验 + 模型回退
│       ├── render.py      # Markdown 渲染/截断/失败文案
│       └── pipeline.py    # 编排：评审全流程 + 回写
└── docs/              # 分析文档 + 建设方案
```

## 生产部署建议

- 用 gunicorn 替代 runserver：`gunicorn config.wsgi -w 2 -b 0.0.0.0:8000`（worker 内线程池已按 `GITLABMR_WORKERS` 并发）。
- SQLite 适合单机小规模；多实例部署时把 `DATABASES` 换成 PostgreSQL 即可（幂等约束依赖数据库）。
- 所有 HTTP 调用均带超时；webhook 永远快速返回 200，重活在线程池中执行。
