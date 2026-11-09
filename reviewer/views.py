"""GitLab webhook endpoint.

Contract: filter cheaply, hand real work to the pipeline's thread pool, and
always answer 200 quickly (GitLab retries slow webhooks, which would cause
duplicate reviews).

No shared-secret verification: the endpoint is meant for trusted intranets
(the former webhook-secret configuration was removed together with its
dashboard UI). Unknown projects are dropped during token resolution anyway.
"""

from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .conf import get_conf
from .models import WebhookEvent
from .services.pipeline import submit_review
from .services.tokens import resolve_repo_token

logger = logging.getLogger("gitlabmr.webhook")

DRAFT_PREFIXES = ("draft:", "wip:", "[draft]", "[wip]")


def _is_bot(username: str, indicators: tuple[str, ...]) -> bool:
    lowered = (username or "").lower()
    return any(indicator.lower() in lowered for indicator in indicators)


def _is_draft(attrs: dict) -> bool:
    if attrs.get("draft") or attrs.get("work_in_progress"):
        return True
    title = (attrs.get("title") or "").strip().lower()
    return any(title.startswith(prefix) for prefix in DRAFT_PREFIXES)


def _hostname(url: str) -> str:
    """Host part of a URL ("" when there is none) - same normalisation as ui._api_host."""
    value = (url or "").strip()
    if not value:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower()


@require_POST
@csrf_exempt
def gitlab_webhook(request):
    conf = get_conf()
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"message": "bad json"}, status=400)

    kind = data.get("object_kind") or ""
    project = data.get("project") or {}
    project_path = (project.get("path_with_namespace", "") or "")[:512]
    # The payload's own origin (project.web_url) decides which instance this
    # event belongs to - never the configured default address, which may be a
    # placeholder when every entry carries its full match URL.
    host = _hostname(project.get("web_url", ""))

    event = WebhookEvent(object_kind=kind[:32], project_path=project_path)
    event.host = host  # request-scoped, never persisted
    try:
        if kind == "merge_request":
            handled, note, mr_iid, action = _handle_merge_request(conf, data, event)
        elif kind == "note":
            handled, note = _handle_note(conf, data, event)
            mr_iid, action = None, "note"
        else:
            handled, note = "ignored", f"unsupported kind {kind}"
            mr_iid, action = None, ""
        event.handled = handled
        event.note = note[:512]
    except Exception as exc:  # noqa: BLE001 - webhook must always answer
        logger.exception("webhook handling error")
        event.handled = "error"
        event.note = type(exc).__name__[:512]
        mr_iid, action = None, ""
    # Event audit policy: only the FIRST "open" of each MR is stored; reopen,
    # update, note and repeat webhooks run through the pipeline unrecorded.
    should_store = kind == "merge_request" and action == "open" and mr_iid is not None and not (
        WebhookEvent.objects.filter(
            object_kind="merge_request", project_path=project_path, mr_iid=mr_iid, action="open"
        ).exists()
    )
    if should_store:
        event.mr_iid = int(mr_iid)
        event.save()
    return JsonResponse({"message": "success"})


def _handle_merge_request(conf, data: dict, event: WebhookEvent) -> tuple[str, str, int | None, str]:
    attrs = data.get("object_attributes") or {}
    mr_iid = attrs.get("iid")
    action = attrs.get("action") or ""
    event.action = action[:32]
    event.head_sha = ((attrs.get("last_commit") or {}).get("id") or attrs.get("sha") or "")[:64]
    if not mr_iid or not event.project_path:
        return "ignored", "missing project path or MR iid", None, action

    repo = resolve_repo_token(conf, event.project_path, event.host)
    if repo is None:
        return "ignored", "no token configured for this repo", int(mr_iid), action
    if _is_bot((data.get("user") or {}).get("username", ""), conf.bot_indicators):
        return "ignored", "bot user", int(mr_iid), action
    if _is_draft(attrs):
        return "ignored", "draft/WIP MR", int(mr_iid), action
    if (attrs.get("target_branch") or "") in conf.ignore_branches:
        return "ignored", "ignored target branch", int(mr_iid), action

    pushed = action == "update" and bool(attrs.get("oldrev"))
    triggered = action in ("open", "reopen") or (pushed and conf.review_on_push)
    if not triggered:
        return "ignored", f"action={action} not a review trigger", int(mr_iid), action
    if not event.head_sha:
        return "ignored", "webhook payload has no head sha", int(mr_iid), action

    submit_review(event.project_path, int(mr_iid), source="webhook", host=event.host)
    return "review", f"submitted {event.project_path}!{mr_iid}@{event.head_sha[:8]}", int(mr_iid), action


def _handle_note(conf, data: dict, event: WebhookEvent) -> tuple[str, str]:
    attrs = data.get("object_attributes") or {}
    body = (attrs.get("note") or "").strip()
    mr = data.get("merge_request") or {}
    event.action = "note"
    if not body.lower().startswith("/review"):
        return "ignored", "not a /review command"
    mr_iid = mr.get("iid")
    if not mr_iid or not event.project_path:
        return "ignored", "note event missing MR reference"
    if _is_bot((data.get("user") or {}).get("username", ""), conf.bot_indicators):
        return "ignored", "bot user"
    if not resolve_repo_token(conf, event.project_path, event.host):
        return "ignored", "no token configured for this repo"

    submit_review(event.project_path, int(mr_iid), source="note", host=event.host)
    return "review", f"submitted by comment on {event.project_path}!{mr_iid}"
