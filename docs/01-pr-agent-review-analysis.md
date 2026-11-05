# PR-Agent MR/PR AI 评审链路深度分析

> 分析对象：PR-Agent（Qodo 开源，本仓库即其克隆）。本文只聚焦一条链路：**MR/PR 的 AI 评审（/review）——从 Webhook 事件进入到评审结论回写的完整过程**。
>
> 以下所有结论均直接来自源码阅读，关键位置标注了文件与行号（以当前仓库 HEAD `b563121` 为准），可点击跳转。

---

## 1. 总览：一条 /review 的生命周期

```
GitLab Webhook (POST /webhook, X-Gitlab-Token 校验)
   │  立即返回 200，真正的工作丢进 BackgroundTasks
   ▼
gitlab_webhook.py：事件过滤（bot/draft/ignore/分支白名单）
   │  action ∈ {open, reopen} → pr_commands（默认含 /review）
   │  action = update + oldrev（新提交推送）→ push_commands
   │  评论 "/review"                           → 命令分发
   ▼
PRAgent.handle_request：apply_repo_settings(加载仓库级 .pr_agent.toml)
   │  command2class["review"] → PRReviewer(pr_url, args).run()
   ▼
PRReviewer.run()
   ├─① GitLabProvider.get_diff_files()      拉取 MR 变更 + 文件内容
   ├─② get_pr_diff()                        diff 加工（行号、裁剪、token 预算）
   ├─③ ai_handler.chat_completion()          LLM 调用（带模型回退链）
   ├─④ _load_valid_review_yaml()             YAML 解析 + Pydantic 校验
   ├─⑤ _prepare_pr_review()                  结果整形：Markdown / 行内评论 / 标签
   └─⑥ publish_*                             回写 GitLab（总结评论 + 行内评论）
```

入口代码：[gitlab_webhook.py](file:///workspace/pr_agent/servers/gitlab_webhook.py#L364-L526)（webhook 处理）、[pr_agent.py](file:///workspace/pr_agent/agent/pr_agent.py#L51-L73)（命令表 `command2class`，`review → PRReviewer`）。

---

## 2. 入口：Webhook 事件如何变成一次评审

文件：[servers/gitlab_webhook.py](file:///workspace/pr_agent/servers/gitlab_webhook.py)

**安全与隔离（值得借鉴的三个细节）**

1. **令牌校验先行**：`authenticate_gitlab_webhook()`（[L313-L361](file:///workspace/pr_agent/servers/gitlab_webhook.py#L313-L361)）用 `hmac.compare_digest` 比较 `X-Gitlab-Token` 与共享密钥（或密钥管理器中 `name:token` 形式的引用），防时序攻击；校验失败直接 401。
2. **请求级设置隔离**：每个请求进来第一件事是 `context["settings"] = copy.deepcopy(global_settings)`（[L367](file:///workspace/pr_agent/servers/gitlab_webhook.py#L367)），之后所有 `get_settings()` 读到的都是本请求的副本。这样 A 仓库的 `.pr_agent.toml` 覆盖绝不会泄漏到并发处理的 B 仓库请求。这是多仓库共存的根基。
3. **立即应答 + 后台执行**：真正处理放在 `background_tasks.add_task(inner, ...)`（[L523](file:///workspace/pr_agent/servers/gitlab_webhook.py#L523)），评审耗时数分钟也不会拖垮 Webhook 应答（GitLab 对 webhook 有超时，超时会重发事件造成重复评审）。

**事件过滤链**（`_perform_commands_gitlab` + 辅助函数）：

| 过滤项 | 位置 | 说明 |
|---|---|---|
| bot 用户 | `is_bot_user()` L128 | 按 `config.bot_user_indicators` 匹配用户名 + `gitlab.ignore_user_ids`，防止自触发死循环 |
| Draft MR | `is_draft()` L168 | draft 不评审，除非 `gitlab.feedback_on_draft_pr` |
| 标题/标签/分支忽略 | `should_process_pr_logic()` → `servers/utils.py` | `ignore_pr_title` 正则、ignored 标签、源/目标分支 |
| 新 MR 的 404 容忍 | `apply_new_mr_repo_settings()` L297 | MR 刚创建 API 可能查不到，按 1s/2s/4s 退避重试 |
| push 触发去重 | `push_trigger_slot()`（utils.py） | 同一 MR 的并发 push 事件用槽位去重，只评一次 |

**触发矩阵**：

- `action=open/reopen` → 执行 `gitlab.pr_commands`（默认 `["/describe", "/review", ...]`，可被仓库 `.pr_agent.toml` 覆盖）
- `action=update` 且带 `oldrev`（新提交）→ `gitlab.push_commands`（需 `handle_push_trigger=true`）
- `action=update` 且 reviewer 变化且 bot 被指派 → `gitlab.reviewer_commands`（需 `handle_reviewer_assignment=true`）
- `note` 事件且评论以 `/` 开头 → 手动命令；`/review` 等命令会先给评论加 👀 反应（`add_eyes_reaction`），结束后按成败加 ✅/❌（`react_to_outcome`）

---

## 3. 调度层：PRAgent 与仓库级配置

文件：[agent/pr_agent.py](file:///workspace/pr_agent/agent/pr_agent.py)

- `command2class`（[L51](file:///workspace/pr_agent/agent/pr_agent.py#L51-L73)）把命令名映射到工具类；`/review`、`/review_pr`、`auto_review` 都指向 `PRReviewer`。
- `_run_command()`（[L424](file:///workspace/pr_agent/agent/pr_agent.py#L424-L537)）流程：`apply_repo_settings(pr_url)`（从 MR 仓库拉取 `.pr_agent.toml` 合并进请求设置，见 [git_providers/utils.py](file:///workspace/pr_agent/git_providers/utils.py)）→ 参数校验（禁止通过命令参数改敏感配置）→ `update_settings_from_args`（`/review --pr_reviewer.num_max_findings=8` 这类内联覆盖）→ 实例化工具并 `run()`。
- 每次命令边界重置遥测收集器 `init_run_details()`，finally 中统一上报 token 用量；异常被捕获并转成 `False` 返回值，保证 webhook 永不 500。

**配置体系**（Dynaconf）：默认值在 `pr_agent/settings/configuration.toml`；每个仓库可在仓库根放 `.pr_agent.toml` 覆盖（如只对该仓库开启 `require_security_review`）；环境变量优先级最高。评审行为几乎全部由 `pr_reviewer.*` 键驱动（`num_max_findings`、`persistent_comment`、`inline_key_issues`、`enable_large_pr_chunking`……）。

---

## 4. PRReviewer：评审主流程

文件：[tools/pr_reviewer.py](file:///workspace/pr_agent/tools/pr_reviewer.py)

### 4.1 初始化：收集“上下文变量”（[L193-L288](file:///workspace/pr_agent/tools/pr_reviewer.py#L193-L288)）

构造函数不拉 diff，只准备 `self.vars` —— 一组会注入 Jinja2 模板的变量：

- PR 元数据：`title`、`branch`、`description`（含逐文件 walkthrough）、`num_pr_files`、`commit_messages_str`
- 语言探测：`get_main_pr_language()`（按文件扩展名统计主语言）
- 功能开关（直接决定 prompt 里生成哪些输出字段）：`require_score`、`require_tests`、`require_risk_assessment`、`require_security_review`、`require_failure_modes`、`require_merge_recommendation`、`require_can_be_split_review` 等
- 评审约束：`num_max_findings`、`extra_instructions`（仓库/命令级追加指令）
- **增量记忆**：`previous_findings` —— 从上一条持久化评审评论的隐藏状态块里解析出“已报告过的发现”（[L680-L693](file:///workspace/pr_agent/tools/pr_reviewer.py#L680-L693)），连同用户已 resolve 的行内评论一起交给模型，避免重复报同样的问题。

### 4.2 run()：从占位评论到最终发布（[L299-L541](file:///workspace/pr_agent/tools/pr_reviewer.py#L299-L541)）

时序（每一步的失败语义都值得注意）：

1. 无变更文件 → 直接跳过（不浪费一次 LLM 调用）。
2. 发布临时占位评论 `"Preparing review..."`（`is_temporary=True`，[L356-L362](file:///workspace/pr_agent/tools/pr_reviewer.py#L356-L362)），大 PR 分块时还会被 ChunkProgressReporter 原地改写为进度条。
3. `get_diff_files()`（在线程池里跑，不阻塞事件循环）。
4. `retry_with_fallback_models(self._prepare_prediction, ...)` —— **模型回退链**（详见 §6）。
5. `_prepare_pr_review()` 生成 Markdown（详见 §7）。
6. **发布决策树**（[L404-L500](file:///workspace/pr_agent/tools/pr_reviewer.py#L404-L500)，简化后）：
   - 持久化评论开启（默认）→ 找到“本 agent 亲自写的上一条评审评论”就**原位更新**（`publish_persistent_comment`），绝不去改一条无法证明是自己写的评论；身份验证失败时降级为发布独立的“Standalone PR Review”（带免责声明），宁可多发一条也不覆盖别人的评论。
   - `"No major issues detected"` 且 `publish_output_no_suggestions=true` → 不发总结评论（只保留行内评论）。
   - 发布失败 → 记录 `review_failed`，finally 里发一条**脱敏的失败说明**（`_review_failure_comment`，[L152-L163](file:///workspace/pr_agent/tools/pr_reviewer.py#L152-L163)：错误信息只从白名单模式映射成固定文案，不泄漏异常细节）。
7. finally：删除临时占位评论；成功且配置了 `review_json_output_path` 时把结构化结果写盘。

---

## 5. Diff 分析：从 GitLab API 到模型可读的 prompt

### 5.1 拉取变更（[gitlab_provider.py get_diff_files L1211-L1346](file:///workspace/pr_agent/git_providers/gitlab_provider.py#L1211-L1346)）

- 数据源：`GET /projects/:id/merge_requests/:iid/changes`（一次拿全量 changes 列表 + `diff_refs` 三元组 base/start/head sha）；结果缓存在 provider 实例上，同一命令内不重复请求。
- 过滤管线：`filter_ignored()`（`ignore.glob` 规则）→ `is_valid_file()`（扩展名白/黑名单，排除二进制、lock 文件）→ 被过滤文件名**不会丢**，后续作为元数据段落塞进 prompt 尾部。
- 文件内容：只对前 `MAX_FILES_ALLOWED_FULL` 个文件拉取新旧两侧全文（用于补上下文行）；之后的文件只有 patch 本身 —— 控制 API 压力。
- GitLab 特有容错：patch 为空（超大文件 GitLab 不给 diff）时用 `load_large_diff` 基于全文自造 diff；新 MR 短暂 404 有重试；`changes_count` 未就绪时不缓存空结果。

### 5.2 加工成“带行号的 hunk”（[algo/git_patch_processing.py](file:///workspace/pr_agent/algo/git_patch_processing.py)）

`get_pr_diff()`（[pr_processing.py L207-L388](file:///workspace/pr_agent/algo/pr_processing.py#L207-L388)）三步走：

1. **按主语言排序**：`sort_files_by_main_languages`，让模型先看到最相关的文件。
2. **扩展上下文**：`process_patch_lines` 给每个 hunk 前后补 `patch_extra_lines_before/after`（默认 0/0，可配）行原文，并 `decouple_and_convert_to_hunks_with_lines_numbers` 把 `@@ -x,y +a,b @@` 改写成带**新文件行号**的格式：

   ```
   ## File: 'src/main.py'

   @@ ... @@
    12  old context line
    13  removed line
    14  added line
   ```

   行号是后续行内评论定位的根基 —— 模型输出 `start_line/end_line` 直接对应新文件行。
3. **token 预算两级裁剪**（`pr_generate_extended_diff` → `pr_generate_compressed_diff`）：
   - `TokenHandler` 先用空 diff 渲染一次完整 prompt 得到基础 token 数，`软预算 = 模型上下文 - 基础 prompt - 输出缓冲(soft)`；
   - 全量扩展 diff 装得下 → 直接用（保真度最高）；
   - 装不下 → **压缩**：hunk 只保留 `+/-` 行去掉上下文行 → 还超 → 按文件丢（先丢 patch 最小的文件？实际是优先保留大文件、丢小文件凑预算），被丢的进 `remaining_files_list`；
   - 剩余空间里再补三段元数据：`ADDED FILES` / `MODIFIED FILES` / `DELETED FILES` 清单 + 被过滤文件清单，让模型知道“有哪些文件没看到”。

### 5.3 超大 PR 分块（`enable_large_pr_chunking`）

[get_pr_multi_diffs L943+](file:///workspace/pr_agent/algo/pr_processing.py#L943-L1123) 把文件贪心打包成 ≤ `max_number_of_calls` 个 chunk（每块独立满足预算），`asyncio.gather` **并行**发起多个 LLM 调用（[pr_reviewer.py L1075-L1077](file:///workspace/pr_agent/tools/pr_reviewer.py#L1075-L1077)），再由 `merge_review_chunks` 汇总：合并 key issues、按 `num_max_findings` 截断、统计失败块数（失败块在最终评论里明示“未覆盖”）。回退模型换代时还会 `resize/include` 已有 chunk（成功块结果缓存复用，不重复花钱）。

---

## 6. AI 调用执行：模型链、预算与截断防护

### 6.1 Prompt 构造（[settings/pr_reviewer_prompts.toml](file:///workspace/pr_agent/settings/pr_reviewer_prompts.toml)）

- **system prompt**：角色定义（PR-Reviewer）+ 核心规则（只看 `+` 行、只报本 PR 引入的问题）+ **把 PR 标题/描述/ticket/代码声明为不可信数据**（防 prompt 注入，[L11](file:///workspace/pr_agent/settings/pr_reviewer_prompts.toml#L11)）+ diff 格式说明 + 何时该报/不该报的判定规则（“低严重度问题必须确凿才报”“无法给出具体触发场景就不要报”）+ Jinja2 条件拼装的 Pydantic schema + YAML 示例。
- **user prompt**：MR 元数据 + diff（占位符 `{{ diff }}`）。
- 渲染用 Jinja2 `SandboxedEnvironment + StrictUndefined`（[token_handler.py](file:///workspace/pr_agent/algo/token_handler.py)）：模板引用了不存在的变量会立刻报错而不是静默输出空串 —— 这是“配置了开关但忘传变量”这类错误的快速失败保障。
- 条件 schema 是精髓：`require_score=true` 时 `Review` 类里才有 `score` 字段。**输出 schema 是由配置动态生成的**，模型永远不会被要求填无关字段。

### 6.2 调用与回退（[pr_processing.py retry_with_fallback_models L855-L908](file:///workspace/pr_agent/algo/pr_processing.py#L855-L908)）

- 模型链：`config.model` + `config.fallback_models`（列表）。
- 每个模型一轮 `_prepare_prediction(model)`：重建 `TokenHandler` → 重新做 diff 裁剪（不同模型上下文窗口不同，裁剪结果不同）→ `_get_prediction()` 里再做一次 `fit_prompt_variable` 精确装填 diff（装不下抛 `FallbackEligibleError`）→ `ai_handler.chat_completion(model, temperature, system, user)`。
- 可回退的异常：`openai.APIError`、`asyncio.TimeoutError`、`FallbackEligibleError`（token 装不下、YAML 解析失败、**finish_reason=="length" 即输出被截断**，[pr_reviewer.py L1261-L1262](file:///workspace/pr_agent/tools/pr_reviewer.py#L1261-L1262)）；其他异常直接上抛。
- 全链失败 → 汇总各模型错误信息抛出，由 run() 统一转成失败评论。
- AI Handler 默认 `LiteLLMAIHandler`：统一封装 OpenAI/Anthropic/Azure/本地 vLLM 等上百种后端，业务代码只面对 `chat_completion() -> (response, finish_reason)` 一个接口。

---

## 7. 结果处理：从自由文本到可信结构

`_load_valid_review_yaml`（[pr_reviewer.py L1266-L1325](file:///workspace/pr_agent/tools/pr_reviewer.py#L1266-L1325)）：

1. `load_yaml()`（[algo/utils.py](file:///workspace/pr_agent/algo/utils.py)）：先剥掉 ```yaml 代码栅栏；解析失败时按 `keys_fix_yaml` 列表对常见坏模式做**修复性解析**（模型爱把含 `[1-5]`、冒号的 key 写坏）；用 `first_key/last_key` 圈定有效范围丢弃前后杂质。
2. 结构校验：`PRReview.model_validate(data)`（Pydantic，[algo/output_models.py](file:///workspace/pr_agent/algo/output_models.py)）+ 按功能开关检查“要求了的字段是否存在”（`_validate_review_schema`，逐字段 log 告警）。
3. 任何一步失败 → `FallbackEligibleError` → 换下一个模型重试。**解析失败被当作“这个模型不行”，而不是整个任务失败** —— 这是输出可靠性的关键设计。

`_prepare_pr_review()`（[L1327-L1489](file:///workspace/pr_agent/tools/pr_reviewer.py#L1327-L1489)）整形：

- `convert_to_markdown_v2`（[algo/utils.py L207+](file:///workspace/pr_agent/algo/utils.py#L207)）：把结构化 dict 渲染成 GFM 表格评论 —— 顶部 PR Review 头 + score/effort/risk/tests 表 + key issues 列表（每条含文件、标题、描述、行号范围）+ security concerns。
- **行内评论发布**（`_publish_key_issues_as_inline_comments`，[L1558-L1652](file:///workspace/pr_agent/tools/pr_reviewer.py#L1558-L1652)）：位置三重校验（文件在 diff 里、行号 ≤ 新文件行数、start≤end）→ 指纹去重（文件+内容哈希 + 隐藏 HTML 注释标记写进评论体，发布后回读验证）→ 校验失败或发布失败的问题**回落到总结评论**，绝不静默丢失。
- **覆盖率脚注**：`remaining_files_list`（因预算完全没看）/ `partial_files_list`（被裁剪过）明示在评论尾部（[L1412-L1428](file:///workspace/pr_agent/tools/pr_reviewer.py#L1412-L1428)），诚实标注评审边界。
- 分块评审时追加 chunked 说明与失败块数量。
- 标签回写：`set_review_labels` 把 effort/security 结论写到 MR labels（GitLab 能力）。

---

## 8. 结果回写：GitLab API 的三种写法

全部在 [gitlab_provider.py](file:///workspace/pr_agent/git_providers/gitlab_provider.py)：

| 写法 | API | 用途 | 关键代码 |
|---|---|---|---|
| 普通评论 | `mr.notes.create({'body': ...})` | 总结评审、失败说明 | [L1511-L1543](file:///workspace/pr_agent/git_providers/gitlab_provider.py#L1511-L1543) |
| 可解决线程 | `mr.discussions.create({'body': ...})` | 评审总结（`PUBLISH_REVIEW_AS_THREAD=true` 时），人可以点 resolve | 同上 |
| 行内评论 | `mr.discussions.create({'body', 'position'})` | key issues 定位到行 | [L1769-L1825](file:///workspace/pr_agent/git_providers/gitlab_provider.py#L1769-L1825) |

**行内评论的 position 是 GitLab 的硬要求**，PR-Agent 的构造方式（[L1800-L1811](file:///workspace/pr_agent/git_providers/gitlab_provider.py#L1800-L1811)）：

```python
pos_obj = {
    "position_type": "text",
    "new_path": target_file.filename,
    "old_path": target_file.old_filename or target_file.filename,
    "base_sha": diff.base_commit_sha,   # 来自 mr.diff_refs
    "start_sha": diff.start_commit_sha,
    "head_sha": diff.head_commit_sha,
    "new_line": target_line_no - 1,     # 0-based！纯新增行
    # 或 "old_line": source_line_no - 1  纯删除行
    # 或两者同时                          修改行
}
```

注意三个坑：行号是 **0-based**；sha 必须来自 `mr.diff_refs` 的三元组（写错会 400）；评论必须落在 diff 范围内。GitLab 拒绝 position 时降级为文件级评论（`file_path` 定位）再不行才放弃。

**持久化评论的“身份”机制**（这是防止覆盖人类评论的关键）：

- 每条评审评论体末尾带隐藏 HTML 注释标记（`<!-- PR-Agent review ... -->`，[algo/comment_identity.py](file:///workspace/pr_agent/algo/comment_identity.py)）。
- 更新前双重验证：标记匹配 **且** 评论作者 id == 当前 token 认证出的 bot 用户 id（`is_comment_authored_by_pr_agent`，[L1495-L1509](file:///workspace/pr_agent/git_providers/gitlab_provider.py#L1495-L1509)）。任一不满足 → 不更新，另发独立评论。
- 更新用 `mr.notes.update(id, {'body': ...})`。

**评论长度**：`limit_output_characters(body, max_comment_chars)` 统一截断（GitLab 单评论上限 1,000,000 字符，实际配置值更保守）。

---

## 9. 值得移植的设计原则（我们的 Django 版要继承什么）

1. **立即 200 + 后台处理 + 触发去重** —— webhook 快速应答是刚性约束。
2. **请求级配置隔离** —— 多仓库各自 `.pr_agent.toml` / 各自 token 互不污染。
3. **diff 三级降级**：全文上下文 → 压缩 hunk → 丢文件留清单，永远附带“没看什么”的诚实说明。
4. **输出即协议**：prompt 动态生成 YAML schema，解析带修复逻辑，Pydantic 强校验，失败算作“模型不行”触发回退。
5. **模型回退链 + 截断检测**（finish_reason=="length" 视为失败）。
6. **身份标记 + 作者校验**：bot 只覆盖自己写的评论；指纹去重防止重复刷屏。
7. **失败也回写**：脱敏失败原因发到 MR，而不是让用户面对静默失败。
8. **不可信数据声明**写进 system prompt，防 prompt 注入。

## 10. 明确不移植的部分（保持简洁）

- LiteLLM 多后端路由 / 模型路由策略 → 我们固定 OpenAI 兼容端点 + 简单回退列表。
- 增量评审（`-i`）、`/ask`、check-run、sinks（slack/telegram）、submodule 展开、draft notes 批量发布、review finding state 的复杂恢复逻辑。
- 令牌精确计数（tiktoken）→ 用 `len(text)/4` 估算 + 保守预算系数即可满足准确性要求，省掉 tiktoken 依赖与编码缓存问题。
