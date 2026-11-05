"""Dashboard UI: review status at a glance plus in-browser configuration.

Pages:
- ``/``                overview: stats, review records, webhook events, manual trigger
- ``/settings/llm/``   LLM + review-behavior settings (saved to DB, wins over env)
- ``/settings/repos/`` GitLab connection defaults + per-prefix repo tokens
- ``/reviews/<pk>/``          one review attempt in full detail (pipeline flowchart)
- ``/reviews/<pk>/retry/``    force re-run of that review
- ``/login/``, ``/logout/``   session gate, enabled by GITLABMR_UI_PASSWORD
"""

from __future__ import annotations

import hmac
import os
from functools import wraps

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .conf import get_conf, reload_conf
from .models import RepoTokenEntry, ReviewRecord, Setting, WebhookEvent
from .services.pipeline import submit_review
from .services.tokens import resolve_repo_token

SECRET_FIELDS = ("webhook_secret", "default_token", "llm_api_key")
BOOL_FIELDS = ("inline_comments", "review_on_push")
INT_FIELDS = (
    "llm_max_output_tokens",
    "llm_timeout",
    "num_max_findings",
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
        ("default_api_url", "默认 GitLab 地址", "text", "如 https://gitlab.com，仓库未单独配置 api_url 时使用"),
        ("default_token", "默认访问令牌", "password", "未命中任何前缀时兜底；留空则未知仓库将被忽略"),
    )),
]

REPO_FORM_FIELDS = ("prefix", "api_url", "token", "webhook_secret", "note")


def ui_password() -> str:
    return os.environ.get("GITLABMR_UI_PASSWORD", "").strip()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def ui_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if ui_password() and not request.session.get("ui_authed"):
            return redirect(f"/login/?next={request.get_full_path()}")
        return view(request, *args, **kwargs)

    return wrapper


def _base_ctx(request, **extra):
    ctx = {
        "ui_password_set": bool(ui_password()),
        "ui_authed": bool(request.session.get("ui_authed")),
    }
    ctx.update(extra)
    return ctx


def _safe_next(request) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return "/"
    return candidate


# ------------------------------------------------------------------ auth


def ui_login(request):
    if not ui_password():
        request.session["ui_authed"] = True  # no password configured: console stays open
        return redirect("/")
    next_url = _safe_next(request)
    if request.method == "POST":
        password = request.POST.get("password", "")
        if hmac.compare_digest(password.encode("utf-8"), ui_password().encode("utf-8")):
            request.session["ui_authed"] = True
            return redirect(next_url)
        messages.error(request, "密码错误，请重试")
    return render(request, "reviewer/login.html", _base_ctx(request, next=next_url))


@require_POST
def ui_logout(request):
    request.session.pop("ui_authed", None)
    return redirect("/login/")


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
            events=WebhookEvent.objects.all()[:20],
        ),
    )


@ui_login_required
@require_POST
def trigger_review(request):
    project = (request.POST.get("project") or "").strip().strip("/").lower()
    iid = (request.POST.get("mr_iid") or "").strip()
    if not project or not iid.isdigit():
        messages.error(request, "请填写完整的项目路径与 MR IID（IID 须为数字）")
        return redirect("/")
    if resolve_repo_token(get_conf(), project) is None:
        messages.error(request, f"{project} 未匹配到任何令牌，请先在「仓库令牌」中添加")
        return redirect("/")
    submit_review(project, int(iid), source="manual")
    messages.success(request, f"已提交评审任务：{project}!{iid}，结果稍后显示在列表中")
    return redirect("/")


@ui_login_required
def review_detail(request, pk):
    record = get_object_or_404(ReviewRecord, pk=pk)
    return render(request, "reviewer/review_detail.html", _base_ctx(request, record=record))


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
            prefix = (request.POST.get("prefix") or "").strip().strip("/").lower()
            deleted, _ = RepoTokenEntry.objects.filter(prefix=prefix).delete()
            reload_conf()
            if deleted:
                messages.success(request, f"已删除令牌配置：{prefix}")
            else:
                messages.error(request, f"未找到令牌配置：{prefix}")
            return redirect("reviewer:settings-repos")
        _apply_setting_post(request.POST, setting or Setting(), REPO_SETTING_GROUPS)
        reload_conf()
        messages.success(request, "GitLab 连接设置已保存并立即生效")
        return redirect("reviewer:settings-repos")

    edit_prefix = (request.GET.get("edit") or "").strip().strip("/").lower()
    editing = RepoTokenEntry.objects.filter(prefix=edit_prefix).first() if edit_prefix else None
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
            using_db_tokens=RepoTokenEntry.objects.filter(enabled=True).exists(),
        ),
    )


def _save_repo_token(request):
    prefix = (request.POST.get("prefix") or "").strip().strip("/").lower()
    token = (request.POST.get("token") or "").strip()
    if not prefix or " " in prefix:
        messages.error(request, "前缀不能为空且不能包含空格，例如 backend/team-a")
        return redirect("reviewer:settings-repos")
    existing = RepoTokenEntry.objects.filter(prefix=prefix).first()
    if existing is None and not token:
        messages.error(request, f"新配置 {prefix} 必须填写访问令牌")
        return redirect("reviewer:settings-repos")
    defaults = {
        "api_url": (request.POST.get("api_url") or "").strip().rstrip("/"),
        "webhook_secret": (request.POST.get("webhook_secret") or "").strip(),
        "note": (request.POST.get("note") or "").strip(),
        "enabled": request.POST.get("enabled") == "on",
    }
    if token:  # empty token on an existing entry = keep the stored one
        defaults["token"] = token
    RepoTokenEntry.objects.update_or_create(prefix=prefix, defaults=defaults)
    reload_conf()
    messages.success(request, f"已保存令牌配置：{prefix}（长前缀优先，立即生效）")
    return redirect("reviewer:settings-repos")
