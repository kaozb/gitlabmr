"""Dashboard UI: review status at a glance plus in-browser configuration.

Pages:
- ``/``                overview: stats, review records, webhook events, manual trigger
- ``/settings/llm/``   LLM + review-behavior settings (saved to DB, wins over env)
- ``/settings/repos/`` GitLab connection defaults + per-prefix repo tokens
- ``/reviews/<pk>/``          one review attempt in full detail (pipeline flowchart)
- ``/reviews/<pk>/retry/``    force re-run of that review
- ``/login/``, ``/logout/``   密码后备通道（GITLABMR_UI_PASSWORD，应急用）；
- ``/work-weixin/*``         企微扫码/免密登录（主通道，见 wework.py）
"""

from __future__ import annotations

import hmac
import os
from functools import wraps
from urllib.parse import quote, urlparse

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .conf import MAX_INLINE_COMMENTS, get_conf, normalize_match_url, reload_conf
from .models import RepoTokenEntry, ReviewRecord, Setting, WebhookEvent
from .services.mrref import MRRefError, parse_mr_ref
from .services.pipeline import submit_review
from .services.tokens import resolve_repo_token

SECRET_FIELDS = ("webhook_secret", "default_token", "llm_api_key")
BOOL_FIELDS = ("inline_comments", "review_on_push")
INT_FIELDS = (
    "llm_max_output_tokens",
    "llm_timeout",
    "num_max_findings",
    "inline_max_comments",
    "max_diff_tokens",
    "per_file_patch_chars",
    "max_comment_chars",
    "workers",
    "gitlab_timeout",
)
LLM_SETTING_GROUPS = [
    ("AI 模型", "OpenAI 兼容 /chat/completions 接口", (
        ("llm_base_url", "接口地址", "text", "如 https://api.deepseek.com/v1"),
        ("llm_api_key", "API Key", "password", "模型的访问密钥"),
        ("model", "模型", "text", "如 deepseek-chat"),
        ("fallback_models", "回退模型", "text", "逗号分隔，主模型失败时依次重试"),
        ("llm_max_output_tokens", "最大输出 tokens", "number", ""),
        ("llm_timeout", "请求超时（秒）", "number", ""),
    )),
    ("评审行为", "diff 预算、回写与触发策略", (
        ("num_max_findings", "最多问题数", "number", "每次评审最多报告的关键问题数"),
        ("inline_max_comments", "行内评论上限", "number",
         f"最多发几条行内评论，其余留在总结评论；代码硬上限 {MAX_INLINE_COMMENTS}（0 = 全部进总结）"),
        ("max_diff_tokens", "diff 预算（tokens）", "number", "超出预算时按文件裁剪"),
        ("per_file_patch_chars", "单文件 patch 上限（字符）", "number", ""),
        ("max_comment_chars", "评论长度上限（字符）", "number", "超出截断并保留隐藏标记"),
        ("inline_comments", "行内评论", "tristate", "开启后关键问题定位到文件+行发表"),
        ("review_on_push", "推送新提交时重新评审", "tristate", "MR update 且带 new commit 时触发"),
        ("ignore_branches", "忽略目标分支", "text", "逗号分隔，如 test,staging"),
        ("language", "评审输出语言", "text", "如 zh-CN"),
        ("workers", "后台线程数", "number", "评审线程池大小（改后需重启进程生效）"),
        ("gitlab_timeout", "GitLab 超时（秒）", "number", ""),
    )),
]

REPO_SETTING_GROUPS = [
    ("GitLab 连接", "webhook 地址、全局验签密钥、默认令牌", (
        ("webhook_secret", "Webhook 验签密钥", "text",
         "GitLab Webhook 的 Secret token，各仓库也可在下方单独配置；留空 = 不验签（仅建议内网）"),
        ("default_api_url", "默认 GitLab 地址", "text", "如 https://gitlab.com；也作为默认令牌的匹配地址"),
        ("default_token", "默认访问令牌", "password", "没有 API 地址命中时兜底；留空则未知仓库将被忽略"),
    )),
]

REPO_FORM_FIELDS = ("product", "api_url", "token", "webhook_secret", "note")


def ui_password() -> str:
    return os.environ.get("GITLABMR_UI_PASSWORD", "").strip()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def ui_login_required(view):
    """全站登录保护：默认一律要求登录，未登录跳企微登录入口。

    企业微信登录为主通道（见 wework.py）；密码登录是后备通道，
    设置了 GITLABMR_UI_PASSWORD 才可用。登录流程三个路由
    （/work-weixin/login/、/work-weixin/oauth-callback、/login/）
    不挂本装饰器，业务路由一律要求登录。
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.session.get("ui_authed"):
            return redirect(f"/work-weixin/login/?next={quote(request.get_full_path(), safe='')}")
        return view(request, *args, **kwargs)

    return wrapper


def _base_ctx(request, **extra):
    ctx = {
        "ui_password_set": bool(ui_password()),
        "ui_authed": bool(request.session.get("ui_authed")),
        "wework_userid": request.session.get("wework_userid", ""),
    }
    ctx.update(extra)
    return ctx


def _safe_next(request) -> str:
    """登录后回跳目标：只接受站内绝对路径，防开放重定向。"""
    candidate = request.POST.get("next") or request.GET.get("next") or "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if not url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return "/"
    return candidate


# ------------------------------------------------------------------ auth


def ui_login(request):
    """密码后备登录通道。

    只有设置了 GITLABMR_UI_PASSWORD 才开放密码校验（应急用，
    比如企微中转服务故障时）；主通道始终是 /work-weixin/login/。
    未设置密码时本页只显示企微登录入口，不放行任何请求。
    """
    next_url = _safe_next(request)
    if request.method == "POST":
        if not ui_password():
            messages.error(request, "未设置 GITLABMR_UI_PASSWORD，密码登录不可用，请使用企业微信扫码登录")
        elif hmac.compare_digest(request.POST.get("password", "").encode("utf-8"), ui_password().encode("utf-8")):
            request.session["ui_authed"] = True
            return redirect(next_url)
        else:
            messages.error(request, "密码错误，请重试")
    return render(request, "reviewer/login.html", _base_ctx(request, next=next_url))


@require_POST
def ui_logout(request):
    request.session.flush()
    return redirect(f"/login/?next={quote(_safe_next(request), safe='')}")


# ------------------------------------------------------------------ dashboard


@ui_login_required
def dashboard(request):
    conf = get_conf()
    counts = dict(ReviewRecord.objects.values_list("status").annotate(n=Count("id")))
    db_tokens = RepoTokenEntry.objects.filter(enabled=True).count()
    stats = {
        "total": sum(counts.values()),
        "success": counts.get("success", 0),
        "failed": counts.get("failed", 0),
        "running": counts.get("running", 0),
        "skipped": counts.get("skipped", 0),
        "events": WebhookEvent.objects.count(),
    }
    summary = [
        ("LLM", f"{conf.llm_base_url or '未配置'} · {conf.model}", bool(conf.llm_base_url and conf.llm_api_key)),
        ("默认 GitLab", conf.default_api_url, bool(conf.default_token or conf.repo_tokens)),
        ("仓库令牌", (f"数据库 {db_tokens} 条" if db_tokens else f"环境/文件 {len(conf.repo_tokens)} 条"),
         bool(conf.repo_tokens)),
        ("Webhook 密钥", "已配置" if conf.webhook_secret else "未配置", bool(conf.webhook_secret)),
        ("行内评论", "开" if conf.inline_comments else "关", True),
        ("推送触发", "开" if conf.review_on_push else "关", True),
    ]
    return render(
        request,
        "reviewer/dashboard.html",
        _base_ctx(
            request,
            stats=stats,
            summary=summary,
            reviews=ReviewRecord.objects.all()[:50],
        ),
    )


@ui_login_required
def webhook_events(request):
    """Standalone page: webhook audit log (only first "open" per MR is stored)."""
    handled_counts = dict(WebhookEvent.objects.values_list("handled").annotate(n=Count("id")))
    # fixed order so the stat cards don't jump around as data changes
    stat_order = (("review", "ok"), ("error", "err"), ("rejected", ""), ("ignored", "idle"), ("received", ""))
    handled_stats = [
        (handled, label, handled_counts.get(handled, 0))
        for handled, label in stat_order
    ]
    return render(
        request,
        "reviewer/webhook_events.html",
        _base_ctx(
            request,
            events=WebhookEvent.objects.all()[:200],
            total=WebhookEvent.objects.count(),
            handled_stats=handled_stats,
        ),
    )


def _api_host(api_url: str) -> str:
    """Hostname of a configured GitLab address (``""`` when it is unusable)."""
    value = (api_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower()


@ui_login_required
@require_POST
def trigger_review(request):
    """Queue a review from a pasted MR link (legacy ``project`` + ``mr_iid`` still accepted)."""
    raw = (request.POST.get("mr_ref") or "").strip()
    if not raw:  # backwards compatible with the old two-field form / scripts
        project = (request.POST.get("project") or "").strip()
        iid = (request.POST.get("mr_iid") or "").strip()
        raw = f"{project}!{iid}" if project and iid else raw
    try:
        ref = parse_mr_ref(raw)
    except MRRefError as exc:
        messages.error(request, str(exc))
        return redirect("/")

    conf = get_conf()
    repo = resolve_repo_token(conf, ref.project_path, ref.host)
    if repo is None:
        messages.error(
            request,
            f"{ref.project_path} 未匹配到任何令牌，请先在「仓库令牌」中添加能覆盖该仓库的 API 地址（或默认令牌）",
        )
        return redirect("/")

    # A link copied from another GitLab instance would silently send the review
    # to the wrong host, so refuse it before spawning a doomed pipeline run.
    api_host = _api_host(repo.api_url or conf.default_api_url)
    if ref.host and api_host and ref.host != api_host:
        messages.error(
            request,
            f"链接域名 {ref.host} 与令牌「{repo.product or '默认'}」的 GitLab 地址 "
            f"{repo.api_url or conf.default_api_url} 不一致；请检查「仓库令牌」里的 API 地址",
        )
        return redirect("/")

    submit_review(ref.project_path, ref.iid, source="manual")
    messages.success(request, f"已提交评审任务：{ref.short}，结果稍后显示在列表中")
    return redirect("/")


@ui_login_required
def review_detail(request, pk):
    record = get_object_or_404(ReviewRecord, pk=pk)
    # non-inline issues: file + summary lines for the detail page panel
    non_inline_issues = [
        {
            "relevant_file": issue.get("relevant_file", ""),
            "location": (
                f"L{issue.get('start_line', '?')}-L{issue.get('end_line', '?')}"
                if issue.get("start_line") and issue.get("end_line")
                else ""
            ),
            "issue_header": issue.get("issue_header", ""),
            "issue_content": issue.get("issue_content", ""),
            "kind": issue.get("kind", ""),
            "evidence": issue.get("evidence", ""),
        }
        for issue in (record.non_inline_issues or [])
    ]
    return render(
        request,
        "reviewer/review_detail.html",
        _base_ctx(request, record=record, non_inline_issues=non_inline_issues),
    )


@ui_login_required
@require_POST
def retry_review(request, pk):
    record = get_object_or_404(ReviewRecord, pk=pk)
    submit_review(record.project_path, record.mr_iid, source="manual", force=True)
    messages.success(request, f"已强制重新评审：{record.project_path}!{record.mr_iid}（忽略幂等记录）")
    return redirect("reviewer:review-detail", pk=pk)


# ------------------------------------------------------------------ settings


def _build_setting_groups(setting: Setting | None, conf, groups_spec) -> list[dict]:
    """Resolve each settings field to a template-ready row.

    Values shown are the effective ones (DB override or env default); secrets
    are never prefilled - empty input on POST means "keep current".
    """
    groups = []
    for title, hint, fields in groups_spec:
        rows = []
        for name, label, kind, field_hint in fields:
            if setting is not None and getattr(setting, name) not in (None, ""):
                value = getattr(setting, name)
            else:
                value = getattr(conf, name)
            if isinstance(value, (tuple, list)):
                value = ",".join(value)
            row = {
                "name": name,
                "label": label,
                "kind": kind,
                "hint": field_hint,
                "value": "" if kind == "password" else value,
                "current": mask_secret(str(value)) if kind == "password" else "",
            }
            if kind == "tristate":
                row.update(inherit=value not in (True, False), is_true=value is True, is_false=value is False)
            rows.append(row)
        groups.append({"title": title, "hint": hint, "rows": rows})
    return groups


def _apply_setting_post(post: dict, setting: Setting, groups_spec) -> None:
    for _, _, fields in groups_spec:
        for name, _, _, _ in fields:
            raw = (post.get(name) or "").strip()
            if name in SECRET_FIELDS:
                if raw:
                    setattr(setting, name, raw)
            elif name in BOOL_FIELDS:
                setattr(setting, name, None if raw == "" else raw.lower() in ("1", "true", "yes", "on"))
            elif name in INT_FIELDS:
                try:
                    setattr(setting, name, int(raw) if raw else None)
                except ValueError:
                    continue
            else:
                setattr(setting, name, raw)
    setting.save()


@ui_login_required
def settings_llm_view(request):
    setting = Setting.objects.first()
    if request.method == "POST":
        _apply_setting_post(request.POST, setting or Setting(), LLM_SETTING_GROUPS)
        reload_conf()
        messages.success(request, "LLM 设置已保存并立即生效")
        return redirect("reviewer:settings-llm")
    return render(
        request,
        "reviewer/settings.html",
        _base_ctx(request, page="llm", groups=_build_setting_groups(setting, get_conf(), LLM_SETTING_GROUPS),
                  show_repos=False),
    )


@ui_login_required
def settings_repos_view(request):
    conf = get_conf()
    setting = Setting.objects.first()
    if request.method == "POST":
        action = request.POST.get("action") or "save_settings"
        if action == "save_repo":
            return _save_repo_token(request)
        if action == "delete_repo":
            api_url = normalize_match_url(request.POST.get("api_url"))
            deleted, _ = RepoTokenEntry.objects.filter(api_url=api_url).delete()
            reload_conf()
            if deleted:
                messages.success(request, f"已删除令牌配置：{api_url}")
            else:
                messages.error(request, f"未找到令牌配置：{api_url}")
            return redirect("reviewer:settings-repos")
        _apply_setting_post(request.POST, setting or Setting(), REPO_SETTING_GROUPS)
        reload_conf()
        messages.success(request, "GitLab 连接设置已保存并立即生效")
        return redirect("reviewer:settings-repos")

    edit_url = normalize_match_url(request.GET.get("edit"))
    editing = RepoTokenEntry.objects.filter(api_url=edit_url).first() if edit_url else None
    # the add/edit form stays collapsed until the user asks for it ("编辑" / "新增")
    show_repo_form = editing is not None or request.GET.get("new") == "1"
    repo_entries = list(RepoTokenEntry.objects.all())
    for entry in repo_entries:
        entry.masked_token = mask_secret(entry.token)
    return render(
        request,
        "reviewer/settings.html",
        _base_ctx(
            request,
            page="repos",
            groups=_build_setting_groups(setting, conf, REPO_SETTING_GROUPS),
            show_repos=True,
            repo_entries=repo_entries,
            editing=editing,
            show_repo_form=show_repo_form,
            using_db_tokens=RepoTokenEntry.objects.filter(enabled=True).exists(),
        ),
    )


def _save_repo_token(request):
    product = (request.POST.get("product") or "").strip()
    raw_api_url = (request.POST.get("api_url") or "").strip().rstrip("/")
    api_url = normalize_match_url(raw_api_url)
    token = (request.POST.get("token") or "").strip()
    # 编辑态带 entry_id：按主键定位，因此允许直接改产品名与匹配地址
    entry_id = (request.POST.get("entry_id") or "").strip()
    entry = RepoTokenEntry.objects.filter(pk=int(entry_id)).first() if entry_id.isdigit() else None
    if not api_url or " " in api_url:
        messages.error(request, "API 地址不能为空且不能包含空格，例如 https://gitlab.example.com/tp/CNEOSv2")
        return redirect("reviewer:settings-repos")
    if entry is None and not token:
        messages.error(request, f"新配置 {api_url} 必须填写访问令牌")
        return redirect("reviewer:settings-repos")
    if entry is None:
        if RepoTokenEntry.objects.filter(api_url=api_url).exists():
            messages.error(request, f"API 地址 {api_url} 已被其他配置占用，请换一个")
            return redirect("reviewer:settings-repos")
    elif RepoTokenEntry.objects.filter(api_url=api_url).exclude(pk=entry.pk).exists():
        messages.error(request, f"API 地址 {api_url} 已被其他配置占用，请换一个")
        return redirect("reviewer:settings-repos")
    fields = {
        "product": product,
        "webhook_secret": (request.POST.get("webhook_secret") or "").strip(),
        "note": (request.POST.get("note") or "").strip(),
        "enabled": request.POST.get("enabled") == "on",
    }
    if entry is None:
        entry = RepoTokenEntry(api_url=api_url, token=token, **fields)
    else:
        entry.api_url = api_url
        entry.product = product
        for name, value in fields.items():
            setattr(entry, name, value)
        if token:  # empty token on an existing entry = keep the stored one
            entry.token = token
    entry.save()
    reload_conf()
    messages.success(request, f"已保存令牌配置：{product or api_url}（最长地址优先，立即生效）")
    return redirect("reviewer:settings-repos")
