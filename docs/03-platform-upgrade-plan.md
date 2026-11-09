# 03 · 平台化建设方案：前端专业化 · 流程配置化 · 提示词中心

> 状态：方案评审稿
> 范围：前端（控制台）、后端（流程引擎）、提示词体系、配置体系、Django Admin
> 原则：保持 Django 单体 + 内网轻量部署不动摇，把"写死在代码里"的部分全部改为"后端配置驱动 + 可审计"。

---

## 一、现状诊断

对现有代码逐项体检后，"草合感"来自五个层面的问题：

### 1.1 前端（控制台）

| # | 问题 | 证据 |
|---|------|------|
| F1 | 样式全部内联在 `base.html`，无组件化，页面各写一套 | [base.html](../reviewer/templates/reviewer/base.html) 139 行内联 CSS；[review_detail.html](../reviewer/templates/reviewer/review_detail.html) 里还有第二段内联 `<style>` |
| F2 | 评审总览统计卡重复渲染"跳过"两次 | [dashboard.html L11-12](../reviewer/templates/reviewer/dashboard.html#L11-L12) |
| F3 | 列表无筛选/搜索：不能按状态、项目、风险、模型过滤，MR 一多只能翻页扫 | [dashboard.html](../reviewer/templates/reviewer/dashboard.html) 评审记录表只有时间/项目/状态/评分/风险/耗时 |
| F4 | 详情页信息组织扁平：键值对 + 三个列表卡片，问题不与代码上下文对照、看不到最终发到 GitLab 的评论内容、同 MR 多次评审无法对比 | [review_detail.html](../reviewer/templates/reviewer/review_detail.html) |
| F5 | `running` 状态不自动刷新，必须手动 F5；无任务取消 | 详情页/列表页均为静态渲染 |
| F6 | Webhook 事件与评审记录割裂：事件页看不到"这个事件最终产生了哪次评审"，也没有入口跳详情 | [webhook_events.html](../reviewer/templates/reviewer/webhook_events.html) |
| F7 | 视图、表单、业务规则、排序、探测全部塞在 `ui.py`（597 行）单文件 | [ui.py](../reviewer/ui.py) |
| F8 | 无 JSON API，前端被锁死在 Django 模板，无法做局部刷新，也无法被其他系统集成 | 全站无 JsonResponse 数据接口（webhook 入口除外） |

### 1.2 评审流程

| # | 问题 | 证据 |
|---|------|------|
| P1 | 流程步骤硬编码：`STEP_ORDER` / `STEP_LABELS` 写死在 pipeline 模块常量里，增删阶段要改代码 | [pipeline.py L39-46](../reviewer/services/pipeline.py#L39-L46) |
| P2 | 只有"一次 LLM 调用"的评审，没有可编排的多阶段（初评→复核→汇总），每阶段无法独立配提示词 | [run_review](../reviewer/services/pipeline.py#L125) stage 4 |
| P3 | 上下文增强藏在 fetch_diff 阶段内部，不是独立可配置阶段 | [pipeline.py L204-222](../reviewer/services/pipeline.py#L204-L222) |
| P4 | 进程崩溃/重启后 `running` 记录永远卡死，无看门狗回收 | 无任何超时标记逻辑 |
| P5 | `completion_chars` 只在成功路径有落点，实际从未写入（详情页恒显示 0） | [models.py L58](../reviewer/models.py#L58)、[run_review](../reviewer/services/pipeline.py#L125) 无赋值 |
| P6 | 线程池在内存里，重启即丢队列；无排队可见性 | `_EXECUTOR` 全局单例 |

### 1.3 提示词

| # | 问题 | 证据 |
|---|------|------|
| T1 | system/schema 模板硬编码在 Python 常量里，改一句提示词要改代码发版 | [prompts.py](../reviewer/services/prompts.py) |
| T2 | 只支持 2 个变量（`language`、`num_max_findings`），无法引用 MR 元数据/diff 统计等 | 同上 |
| T3 | 无版本管理、无生效历史、无"这次评审用的是哪版提示词"的记录 | `ReviewRecord` 无 prompt 版本字段 |
| T4 | 无 dry-run：改完提示词只能真发一次评审验证 | 无预览能力 |
| T5 | 无法按仓库/产品线差异化提示词（CNEOSv2 和 WAF 想用不同评审侧重点做不到） | 全局仅一套 |

### 1.4 配置体系

| # | 问题 | 证据 |
|---|------|------|
| C1 | 一个配置项散落 4 处：`models.Setting` 字段、`ui.py` 的 `LLM_SETTING_GROUPS`、`conf.py` 的 `_TEXT/_INT/_BOOL_OVERRIDES`、`_load_env_conf()`，加一个配置要改 4 个文件，极易漏 | [ui.py L50-73](../reviewer/ui.py#L50-L73)、[conf.py L230-249](../reviewer/conf.py#L230-L249) |
| C2 | 不支持按项目/产品线差异化（全局一套配置） | `Setting` 单行表 |
| C3 | 配置无变更审计：谁在何时把 `num_max_findings` 从 6 改成 20 无从查起 | 无审计表 |
| C4 | 配置不支持变量/引用（例如提示词里想引用配置项、详情页想展示生效值链路） | — |

### 1.5 Django Admin

| # | 问题 | 证据 |
|---|------|------|
| A1 | 裸注册：`ReviewRecord` 只有 6 个字段进 list_display，`non_inline_issues` JSON 原样塞进只读页，无筛选、无内联、无关联跳转 | [admin.py](../reviewer/admin.py) |

---

## 二、建设目标

1. **前端专业化**：统一设计系统 + 组件化模板 + 列表可筛选 + 详情页"MR 档案"化 + 实时状态。
2. **后端方便控制**：流程阶段、每阶段提示词、评审行为全部数据库配置驱动，改配置即生效，全程留痕。
3. **提示词中心**：每阶段模板可配置、支持变量插值、版本化可回滚、dry-run 预览、可按项目差异化。
4. **配置单一事实来源**：一处声明（注册表），模型/UI/env 三端生成，杜绝 4 处同步。
5. **专业后台**：Django Admin 与控制台各司其职——Admin 面向运维排障（全字段只读+筛选），控制台面向日常使用。
6. **平滑演进**：不推翻现有 Django 模板体系，渐进增强；REST API 先行，为未来 SPA/集成留口。

**非目标**（本期不做）：引入 Node 构建链/重前端框架、Celery/Redis 队列（仍用线程池，仅加看门狗与排队可见性）、多租户权限体系（仍沿用企微登录）。

---

## 三、目标架构

```
┌──────────────────────────── 控制台前端（Django 模板 + htmx/Alpine）────────────────────────────┐
│  总览仪表盘 │ 评审列表(筛选/搜索) │ MR 评审档案(详情) │ 提示词中心 │ 流程编排 │ 仓库令牌 │ 审计 │
└──────────────┬────────────────────────────────────────────────────────────┬───────────────────┘
               │ 渐进增强（局部刷新/预览）                                     │ 只读运维视图
┌──────────────▼────────────────── API / 视图层 ─────────────────────────────▼───────────────────┐
│  /api/v1/*  REST JSON（reviews/prompts/stages/configs/events/stats，session+Token 双认证）     │
│  模板视图仅做组装，数据一律出自 service 层（视图不再直接碰 ORM 细节）                              │
└──────────────┬─────────────────────────────────────────────────────────────────────────────────┘
┌──────────────▼────────────────── 服务层 ───────────────────────────────────────────────────────┐
│ SettingRegistry（配置注册表，单一事实来源）                                                      │
│ PromptEngine（模板渲染 ${var} 插值 + 变量注册表校验 + 版本快照 + dry-run）                        │
│ Pipeline Engine（阶段注册表驱动：token→read_mr→fetch_diff→enrich→llm_*→publish，阶段事件落库）   │
│ Watchdog（running 超时回收）+ 结构化日志（request_id 串联）                                      │
└──────────────┬─────────────────────────────────────────────────────────────────────────────────┘
┌──────────────▼────────────────── 数据层 ───────────────────────────────────────────────────────┐
│ ReviewRecord + ReviewIssue(新) + ReviewStageEvent(新) │ PromptTemplate + PromptVersion(新)     │
│ PipelineStage(新) │ ProjectProfile(新) │ Setting/RepoTokenEntry │ ConfigAuditLog(新) │ WebhookEvent+review FK │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 四、后端建设方案

### 4.1 配置注册表（单一事实来源，解决 C1/C4）

新增 `reviewer/settings_registry.py`，用**一份声明**驱动模型字段、控制台表单、env 回落三端：

```python
# 每个配置项一条声明：类型/标签/提示/默认值/env 名/可选值/分组/是否密钥/是否重启生效
SETTING_SPECS = [
    Spec("llm_base_url",  kind="text",  label="接口地址", group="llm",
         default="", env="GITLABMR_LLM_BASE_URL", placeholder="https://api.deepseek.com/v1"),
    Spec("llm_api_key",   kind="secret", label="API Key", group="llm", env="GITLABMR_LLM_API_KEY"),
    Spec("num_max_findings", kind="int", label="最多问题数", group="review",
         default=6, env="GITLABMR_NUM_MAX_FINDINGS", min=1, max=MAX_FINDINGS),
    Spec("inline_comments",  kind="bool", label="行内评论", group="review",
         default=False, env="GITLABMR_INLINE_COMMENTS"),
    ...
]
```

- `models.Setting` 字段仍显式声明（迁移友好），但**校验注册表与字段一一对应**（启动自检 + 单测断言，漏写直接测试失败）。
- `conf._load_env_conf()` / `_apply_db_overrides()` / `_build_setting_groups()` 全部改为遍历 `SETTING_SPECS` 生成，删除三份手写清单。
- 效果：新增配置 = 注册表加一行 + Setting 加一列，UI 表单、生效链路、env 默认自动齐全。

### 4.2 提示词中心（解决 T1–T5）

#### 4.2.1 数据模型

```python
class PromptTemplate(models.Model):
    stage_key = models.CharField(max_length=64, unique=True)   # review / refine / summarize ...
    name = models.CharField(max_length=128)
    description = models.CharField(max_length=256, blank=True, default="")
    system_template = models.TextField()                        # ${var} 插值
    user_template = models.TextField(blank=True, default="")    # 空表示沿用内置 user 组装
    output_format = models.CharField(max_length=16, default="yaml")  # yaml|markdown|json
    variables = models.JSONField(default=list)                  # 该模板声明的变量清单（来自变量注册表）
    enabled = models.BooleanField(default=True)

class PromptVersion(models.Model):
    template = models.ForeignKey(PromptTemplate, on_delete=models.CASCADE)
    version = models.IntegerField()                             # 每模板自增
    system_template = models.TextField()
    user_template = models.TextField()
    remark = models.CharField(max_length=256, blank=True, default="")
    created_by = models.CharField(max_length=64, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    # 唯一约束 (template, version)；保存模板即生成新版本，支持一键回滚
```

- `ReviewRecord` 增加 `prompt_versions = models.JSONField(default=dict)`，如 `{"review": 7, "refine": 2}` —— **每次评审固化所用版本号**，详情页可回看"这单评审用的就是 review@v7"。
- data migration 把 `prompts.py` 现有模板灌入 DB 作为 v1；代码常量保留为**最终 fallback**（DB 模板被禁用/渲染失败时使用，保证评审链路永不因配置空档而中断）。

#### 4.2.2 变量系统

- **插值语法 `${name}`**（不用 Jinja2/str.format）：
  - 提示词内含 YAML 示例，`{}` 与 `{{ }}` 都有撞车风险（现模板就靠 `{language}` format 占位）；`${}` 在 YAML/代码样例中几乎不出现，且自实现替换器零依赖、无注入面（只做查表替换，不执行任何表达式）。
  - 后续确需条件/循环时，再演进为"Jinja2 沙箱 + 可配置定界符"，接口不变。
- **变量注册表**：引擎内置一个 `VARIABLE_REGISTRY`，按命名空间声明每类变量（名称、说明、示例值）。dry-run 用示例值或**选一条历史评审的真实数据**渲染。

| 命名空间 | 变量示例 |
|---|---|
| `mr.*` | `mr.title` `mr.description` `mr.author` `mr.source_branch` `mr.target_branch` `mr.url` `mr.iid` `mr.commit_messages` |
| `project.*` | `project.path` `project.product` |
| `diff.*` | `diff.text` `diff.file_count` `diff.token_estimate` `diff.file_list` |
| `context.*` | `context.text` `context.file_count` `context.symbol_count` `context.search_available` |
| `cfg.*` | `cfg.language` `cfg.num_max_findings` `cfg.max_diff_tokens` |
| `sys.*` | `sys.now` `sys.date` `sys.version` |

- **保存时校验**：模板语法（括号配对）+ 未知变量警告（不阻断，标黄提示）+ 声明变量与实际使用一致性检查。
- **dry-run 预览**：`POST /api/v1/prompts/<stage>/preview/` 传可选 `review_record_id`，返回渲染后的 system/user 全文 + 变量命中清单。控制台编辑器右侧实时预览。
- **内置变量注入点**：pipeline 渲染前构造 `RenderContext`（集中收集 mr/diff/context/cfg 数据），阶段只声明自己需要的变量，未声明的变量渲染为空并在阶段事件里记 warning。

### 4.3 流程编排（解决 P1–P4、P6）

#### 4.3.1 阶段注册表

```python
class PipelineStage(models.Model):
    key = models.SlugField(unique=True)                # token / read_mr / fetch_diff / enrich / review / refine / publish
    name = models.CharField(max_length=64)             # 显示名（控制台/详情页用）
    kind = models.CharField(max_length=32)             # builtin | llm | publish（决定执行器）
    order = models.IntegerField()                      # 执行顺序
    enabled = models.BooleanField(default=True)
    prompt = models.ForeignKey(PromptTemplate, null=True, blank=True, on_delete=models.SET_NULL)
    params = models.JSONField(default=dict)            # 阶段级参数：超时/温度/输出语言/重试次数...
    critical = models.BooleanField(default=True)       # 失败是否中断整条流水线
```

- 内置执行器按 `kind` 分发：`builtin`（token/read_mr/fetch_diff）、`llm`（review/refine/summarize，绑定 prompt）、`publish`（回写）。
- 现在写死在 `run_review` 里的 if/else 改为**遍历启用阶段顺序执行**；每阶段执行产生 `ReviewStageEvent`：

```python
class ReviewStageEvent(models.Model):
    record = models.ForeignKey(ReviewRecord, on_delete=models.CASCADE, related_name="stage_events")
    stage_key = models.CharField(max_length=64)
    status = models.CharField(max_length=16)           # running|success|failed|skipped
    detail = models.TextField(blank=True, default="")
    model_used = models.CharField(max_length=128, blank=True, default="")
    prompt_chars = models.IntegerField(default=0)      # 修复 P5：completion 落点
    completion_chars = models.IntegerField(default=0)
    ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
```

- `ReviewRecord.steps` JSON 继续保留（列表页快照渲染用），详情页时间线改读 `stage_events`（结构化、可分页）。

#### 4.3.2 多阶段评审链（可选启用）

默认单阶段 `review`（与今天等价，行为不回退）；在流程编排页可启用：

- `review`（初评，产出结构化问题清单）→ `refine`（复核：对初评问题逐条判断误报/证据充分性，输入 `${review.json}`，输出保留/降级/删除决定）→ `summarize`（汇总生成总结评论 markdown）。
- `refine` 阶段的过滤结果同步进 `filter_unverified_absence_issues` 的兜底逻辑（代码兜底仍在，双保险）。
- 每阶段独立提示词、独立模型覆盖（`params.model` 支持复核用便宜模型）。

#### 4.3.3 看门狗与排队可见性（P4/P6）

- `manage.py reap_stalled_reviews`：将 `status=running` 且 `updated_at` 超过 `stalled_minutes`（配置项，默认 30）的记录标记 `failed(reason=stalled)`；cron/systemd timer 每分钟跑。
- 控制台读取时惰性兜底：dashboard 首屏对超时 running 一并标记（防 cron 缺位）。
- 列表页展示线程池队列深度（`executor._work_queue.qsize()`），运行中记录在详情页可见"排队中/执行中"。

### 4.4 数据模型调整

| 变更 | 说明 |
|---|---|
| 新增 `ReviewIssue` | 把 `non_inline_issues` JSON 拆表：`record FK / relevant_file / start_line / end_line / header / content / kind / evidence / severity / published_inline(bool) / created_at`。JSON 字段保留只读一版做迁移兼容，过渡期后删除 |
| `WebhookEvent.review` FK | 评审提交时回填，事件页可跳详情（F6） |
| `ReviewRecord` 增列 | `prompt_versions JSON`、`queue_wait_ms`、`language`（本次评审输出语言快照） |
| 新增 `ProjectProfile` | 按 GitLab URL 前缀差异化：`match_url / language / num_max_findings / prompt_overrides JSON / inline_comments / enabled`，解析规则复用 `resolve_repo_token` 的最长前缀匹配。`RenderContext` 组装时按项目合并覆盖 |
| 新增 `ConfigAuditLog` | `user / action / target(table,pk) / field / old / new / created_at`。Setting、PromptTemplate、PipelineStage、RepoTokenEntry、ProjectProfile 的写操作统一走 `audit_change()` 助手记录 |

### 4.5 REST API（解决 F8）

`/api/v1/`，session 认证（控制台用）+ `Authorization: Token <x>`（脚本/集成用，复用 `ui_authed` 之外的独立 token 表或简单 env token）：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/reviews/` | GET | 分页 + 筛选（`status/risk/project/model/source/date_from/date_to`）+ `?q=` 搜索 + 排序 |
| `/api/v1/reviews/<pk>/` | GET | 详情：issues、stage_events、prompt 版本、文件清单 |
| `/api/v1/reviews/<pk>/retry/` | POST | 强制重评 |
| `/api/v1/reviews/trigger/` | POST | 手动触发（body: `mr_ref`） |
| `/api/v1/prompts/` | GET/POST/PUT | 模板 CRUD（写操作生成新版本 + 审计） |
| `/api/v1/prompts/<stage>/preview/` | POST | dry-run 渲染 |
| `/api/v1/prompts/<stage>/versions/` | GET | 版本列表与 diff |
| `/api/v1/prompts/<stage>/rollback/` | POST | 回滚到指定版本（生成新版本，不删历史） |
| `/api/v1/stages/` | GET/PUT | 流程阶段编排读写 |
| `/api/v1/events/` | GET | webhook 事件（关联 review_id） |
| `/api/v1/stats/summary/` | GET | 仪表盘统计 + 近 7 天趋势 |
| `/api/v1/configs/` | GET/PUT | Setting 注册表读写（含生效值链路：DB 覆盖值 / env 默认值 / 代码默认值） |

- 实现约束：视图层薄壳，业务全部下沉 service；DRF 不引入（避免重依赖），用 Django 原生 + 一个轻量分页/序列化助手即可。

### 4.6 可观测性

- 结构化日志：pipeline/llm/gitlab 关键节点输出 JSON 行日志，带 `request_id`（webhook 入口生成、贯穿线程池）。
- LLM 用量统计：`stage_events` 聚合出按模型/按天的 token 与耗时趋势（`usage` 字段若 LLM 网关返回则透传存储）。
- 失败原因枚举化：`error_reason` 收敛为固定枚举（`no_token/read_mr_failed/timeout/http_error/bad_yaml/bad_schema/all_models_failed/gitlab_error/stalled/unknown`），列表筛选与失败率统计可用。

---

## 五、前端建设方案

### 5.1 技术选型

**保持 Django 服务端渲染 + htmx（局部刷新）+ Alpine.js（轻交互）**，不引入 Node 构建链：

- 与现有部署形态一致（gunicorn 单容器、内网、无 CDN），改一处模板即生效；
- `running` 状态自动刷新、提示词预览这类需求 htmx 局部轮询/局部提交完全覆盖；
- REST API 已就位，未来若要 SPA 可平滑迁移，不返工。

静态资源从内联迁到 `reviewer/static/reviewer/{css,js}/`，按页面拆文件；模板拆 partials。

### 5.2 设计系统

- **Design tokens**：把现有 CSS 变量体系化（色板/间距 4px 栅格/圆角/阴影/字号层级/语义色 success|warn|danger|info|idle），单文件 `tokens.css`。
- **组件 partials**（`templates/reviewer/partials/`）：`stat_card`、`data_table`、`badge`、`risk_pill`、`pager`、`empty_state`、`step_flow`（列表页用横向快照）、`timeline`（详情页用竖向）、`issue_card`、`kv_panel`、`filter_bar`、`toast`。
- 所有页面只允许组合 partials，禁止页面内再写样式（CI 可用 grep 检查 `<style>` 出现次数兜底）。

### 5.3 信息架构（导航重组）

```
总览          /                     统计卡 + 近7天趋势 + 手动触发 + 快捷入口
评审          /reviews/             列表：筛选栏 + 搜索 + 分页（替代原首页大表）
评审档案       /reviews/<pk>/        见 5.5（核心页面）
Webhook 事件   /events/              事件流水 + 关联评审跳转
提示词中心     /prompts/             模板列表 → 编辑器 + 变量面板 + 预览 + 版本历史
流程编排       /pipeline/            阶段启停/排序/参数/绑定提示词
项目差异化     /profiles/            ProjectProfile 管理（按产品线覆盖配置与提示词）
仓库令牌       /settings/repos/      现有能力保留 + 探测结果持久化（不再放 session）
服务设置       /settings/            注册表驱动的表单（分组渲染）+ 审计入口
审计日志       /audit/               配置变更流水（who/when/old→new）
```

### 5.4 列表页与总览（解决 F2/F3/F5）

- 总览：修复重复"跳过"卡；新增近 7 天评审量/成功率迷你柱状图（`stats/summary` 数据 + 纯 CSS/SVG 柱图，无图表库）；失败率突增红色提示。
- 评审列表：筛选栏（状态下拉、风险下拉、项目下拉、模型下拉、日期范围、关键字搜索 `q`），URL query 驱动（可收藏/分享）；列：时间、项目!MR、状态、评分、风险、问题数、模型、耗时、来源（webhook/note/manual）、操作。
- running 记录行 htmx `hx-trigger="every 5s"` 局部刷新该行直至终态；详情页 running 时整页局部轮询。
- 支持批量勾选 + 批量重试失败项。

### 5.5 评审档案页（"看每个 MR 的信息要专业"的核心，解决 F4）

自上而下五个区块：

1. **MR 头卡**：标题、作者、分支流向（`source → target`）、项目!iid、head sha（带复制按钮）、GitLab 外链、状态徽章、评分环形图（SVG）、风险标、触发来源、耗时。同 MR 历史 sha 的评审记录做成 tab/下拉，可横向对比两次评审的评分与问题差异。
2. **流程时间线**：竖向 timeline（读 `stage_events`），每节点：状态点 + 阶段名 + 耗时 + 模型 + prompt 版本号（如 `review@v7`）+ 输入/输出字符量；点击展开 detail 日志与该阶段的渲染后提示词（只读、脱敏）。
3. **评审问题**：卡片列表（`ReviewIssue`），每卡：severity 徽章、类型徽章（verified/suggestion）、文件路径 + 行号锚点（链接直达 GitLab 对应行 `#L{n}`）、问题正文、证据引用块、"已行内发表/留在总结"标记；卡片可展开**该 hunk 的 diff 上下文**（评审时缓存的 patch 片段，见下）。
4. **文件清单**：已评审 / 未纳入（含原因：忽略规则命中、超预算、二进制等——`not_reviewed_files` 从纯路径数组升级为 `{path, reason}` 结构）。
5. **回写快照**：最终发到 GitLab 的总结评论 markdown 原文（`ReviewRecord.summary_markdown` 新列存档），与 MR 评论区互为镜像，排查"为什么评论长这样"不用再去翻 GitLab。

> 为支撑 3/5，`publish` 阶段将最终 body 与逐问题的 patch 片段写回记录（体积可控：单问题 hunk 截断至 2KB）。

### 5.6 提示词中心页（新）

- 左侧模板列表（阶段、启用态、当前版本、最近使用时间）；右侧编辑区：
  - system / user 两个编辑器（等宽字体、行号）；
  - 变量面板：列出该阶段可用变量（来自注册表），点击插入 `${var}`，hover 显示说明与示例值；
  - 「预览」按钮 → dry-run：选择样本（默认示例值 / 指定历史评审），右栏渲染结果，未知变量高亮告警；
  - 「保存」→ 新版本 + 审计日志；「历史版本」→ 双栏 diff + 一键回滚；
  - 启用开关 + 「恢复内置默认」（从代码常量重置）。

### 5.7 流程编排页 / 项目差异化页（新）

- 流程编排：阶段卡片拖拽排序（htmx 上移/下移按钮即可，不引拖拽库）、启用开关、参数编辑（超时/重试/温度）、提示词绑定下拉；修改即时生效 + 审计。
- 项目差异化：前缀匹配规则表（复用令牌页交互模式），每条可覆盖语言、findings、行内评论开关、提示词集。

---

## 六、Django Admin 专业化（解决 A1）

定位：**运维排障视角**（控制台管日常，Admin 管全量数据与应急）。

- `ReviewRecord`：`list_display` 全维展示（项目/MR/sha/状态/评分/风险/问题数/模型/耗时/来源/创建时间）；`list_filter`（status/risk/error_reason/source/model_used/日期）；`search_fields`（project_path、head_sha、mr_url）；`inlines=[ReviewIssueInline, ReviewStageEventInline]`；字段分组只读展示（基础信息/结果/流程/错误）；页头直达控制台详情页链接。
- `WebhookEvent`：filter（object_kind/handled/action/日期）+ review FK 链接。
- `PromptTemplate`/`PromptVersion`：只读（编辑必须走控制台以留审计），仅展示与回滚入口。
- `ConfigAuditLog`/`RepoTokenEntry`/`ProjectProfile`：审计只读；令牌脱敏显示。

---

## 七、实施路线图

四期推进，每期独立可验收、可发布；依赖关系：P1 → P2 → P3，P4 可并行穿插。

### Phase 1 · 配置与提示词基建（后端）

1. `settings_registry.py` 注册表落地，`conf.py`/`ui.py` 三份手写清单收敛为注册表生成；启动自检 + 单测。
2. `PromptTemplate/PromptVersion/ConfigAuditLog` 模型 + data migration 灌入内置模板（v1）。
3. `${var}` 渲染引擎 + 变量注册表 + 校验 + dry-run API；pipeline 切换为"DB 模板优先、代码常量 fallback"；`ReviewRecord.prompt_versions` 落值。
4. 看门狗管理命令 + cron 示例；修复 `completion_chars` 断链。

**验收**：控制台改提示词保存 → 立即生成新版本 → 下次评审用新版并记录版本号 → 回滚生效；dry-run 渲染与真实评审输入一致（抽 3 条历史记录比对）；4 处配置定义收敛为 1 处（代码行数净减少）。

### Phase 2 · 流程编排 + REST API（后端）

1. `PipelineStage/ReviewStageEvent` 模型与阶段执行器改造（默认阶段序列与今天完全等价）。
2. `refine/summarize` 可选阶段执行器（默认关闭）。
3. `/api/v1/*` 全套端点 + Token 认证；`WebhookEvent.review` FK；`ReviewIssue` 拆表迁移。
4. 失败原因枚举化 + 结构化日志（request_id）。

**验收**：`manage.py test` 全绿；用 curl 走通 筛选列表→详情→重试 全链路；关闭 `enrich` 阶段评审仍成功且详情页时间线如实反映；同一 sha 重启进程后 stalled 看门狗能回收僵尸记录。

### Phase 3 · 前端专业化（控制台）

1. 设计 tokens + partials 组件化，静态资源出库；修复重复统计卡。
2. 评审列表筛选/搜索/批量重试 + running 局部刷新。
3. 评审档案页五区块重构（时间线/问题卡/文件清单/回写快照/历史对比）。
4. 提示词中心页（编辑器 + 变量面板 + 预览 + 版本 diff/回滚）。

**验收**：全站无页面级内联 `<style>`；筛选组合 URL 可分享；running 记录 5 秒内自动更新；提示词改动全程不碰代码；详情页能回答"这次评审用了哪版提示词、为什么这条问题没发行内评论、最终评论发了什么"三个问题。

### Phase 4 · 编排/差异化/Admin/审计收尾

1. 流程编排页、项目差异化页（ProjectProfile）。
2. Django Admin 专业化全套。
3. 审计日志页、总览趋势图、令牌探测结果持久化。
4. 文档：README 与 docs 同步更新。

**验收**：Admin 可独立完成一次故障排查（找某 MR → 看阶段事件 → 定位失败原因）；改配置/提示词/编排均能在审计页查到 who/when/old→new。

---

## 八、风险与权衡

| 风险 | 决策 |
|---|---|
| 提示词模板与 YAML 花括号冲突 | 用 `${var}` 自实现插值（查表替换、零依赖、无注入面）；Jinja2 列为后续演进，接口不变 |
| DB 模板配错导致评审中断 | 代码常量兜底 + 保存时语法校验 + dry-run 强推荐 + 版本秒回滚；`critical=false` 的阶段失败可配置降级跳过 |
| `ReviewIssue` 拆表迁移量 | SQLite 单机量级可控；JSON 原字段保留一版只读兼容，新写入双写一版后切换读取 |
| 阶段事件表放大写压力 | 详情页时间线分页读取；量大时（多实例化）随整体迁 PostgreSQL，无需改模型 |
| 线程池重启丢任务 | 本期不做队列外移（保持轻量）；看门狗回收 + 事件页可看到"已接收未评审"事件便于手动补评；列为一并演进项（Celery/Redis）|
| 前端局部刷新引入 htmx 新依赖 | 单文件无构建，CDN/静态文件本地化皆可，风险低收益高 |
| 提示词里渲染 diff 超预算 | 变量注入点统一截断（沿用 `max_diff_tokens` 预算），阶段事件记录实际注入字符量 |

---

## 附录 A · 变量注册表（首批）

```
mr.title / mr.description / mr.author / mr.source_branch / mr.target_branch
mr.url / mr.iid / mr.commit_messages
project.path / project.product
diff.text / diff.file_count / diff.token_estimate / diff.file_list
context.text / context.file_count / context.symbol_count / context.search_available
cfg.language / cfg.num_max_findings / cfg.max_diff_tokens / cfg.inline_max_comments
review.json        # 仅 refine/summarize 阶段：初评结构化结果
sys.now / sys.date / sys.version
```

## 附录 B · 失败原因枚举

```
no_token / read_mr_failed / no_head_sha / draft_mr / no_changes / no_reviewable_files
timeout / http_error / bad_response / truncated / bad_yaml / bad_schema / all_models_failed
gitlab_error / stalled / unknown
```

## 附录 C · API 一览

见 4.5 表格（reviews/prompts/stages/events/stats/configs 共 12 个端点，双认证：session + Token）。
