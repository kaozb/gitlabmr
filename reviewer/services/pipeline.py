"""Review pipeline: event -> diff -> LLM -> markdown -> write back to GitLab.

Runs inside a ThreadPoolExecutor (submitted by the webhook view, which returns
200 immediately). The ReviewRecord row is both the audit trail and the
idempotency gate: one review per (project, MR, head sha). Every stage is
tracked in ``ReviewRecord.steps`` and rendered as a flowchart on the detail
page. Skipped reviews still publish an explanatory MR comment - never silent.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections

from ..conf import MAX_INLINE_COMMENTS, get_conf
from ..models import ReviewRecord, try_register_review
from .diffctx import DiffContext, build_diff_context, estimate_tokens
from .gitlab import GitLabClient, GitLabError
from .llm import (
    KeyIssue,
    LLMConfig,
    LLMError,
    ReviewOutput,
    filter_unverified_absence_issues,
    review_with_fallback,
)
from .prompts import build_system_prompt, build_user_prompt
from .repoctx import build_repo_context, fetch_requested_context
from .render import (
    REVIEW_MARKER,
    render_failure,
    render_progress,
    render_review,
    render_skip,
    truncate,
)
from .tokens import resolve_repo_token

logger = logging.getLogger("gitlabmr.pipeline")

_EXECUTOR: ThreadPoolExecutor | None = None

STEP_ORDER = ("token", "read_mr", "fetch_diff", "ai_review", "publish")
STEP_LABELS = {
    "token": "令牌校验",
    "read_mr": "读取 MR",
    "fetch_diff": "拉取 Diff",
    "ai_review": "AI 评审",
    "publish": "回写评论",
}

# Appended once the need_context fetch budget is spent; the next reply is
# parsed with require_review=True so a refusal to review falls into the
# normal model-fallback chain instead of looping forever.
CONTEXT_EXHAUSTED_NOTE = (
    "\n\n## Note\nThe context request budget is exhausted - no further repository "
    "content can be fetched. You MUST now answer with the final review YAML schema "
    "based on the information you already have. Do NOT answer with need_context."
)


def get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=get_conf().workers, thread_name_prefix="gitlabmr")
    return _EXECUTOR


def submit_review(project_path: str, mr_iid: int, source: str = "webhook", force: bool = False,
                  host: str = "") -> None:
    get_executor().submit(_run_safe, project_path, mr_iid, source, force, host)


def _run_safe(project_path: str, mr_iid: int, source: str, force: bool, host: str = "") -> None:
    close_old_connections()
    try:
        run_review(project_path, mr_iid, source, force=force, host=host)
    except Exception:  # worker must never die silently
        logger.exception("review pipeline crashed for %s!%s", project_path, mr_iid)
    finally:
        close_old_connections()


def duration(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class StepTracker:
    """Tracks the five pipeline stages persisted into ``ReviewRecord.steps``.

    Statuses: pending (white) -> running -> success (green) / failed (red, with
    an inspectable detail) / skipped (grey, stage intentionally not executed).
    """

    def __init__(self) -> None:
        self.steps: list[dict] = [
            {"step": name, "label": STEP_LABELS[name], "status": "pending", "detail": "", "ms": 0}
            for name in STEP_ORDER
        ]
        self._started_at: dict[str, float] = {}

    def _entry(self, step: str) -> dict:
        return next(entry for entry in self.steps if entry["step"] == step)

    def _elapsed(self, step: str) -> int:
        begin = self._started_at.get(step)
        return int((time.monotonic() - begin) * 1000) if begin is not None else 0

    def start(self, step: str) -> None:
        self._entry(step)["status"] = "running"
        self._started_at[step] = time.monotonic()

    def done(self, step: str, detail: str = "") -> None:
        entry = self._entry(step)
        entry["status"] = "success"
        entry["detail"] = detail
        entry["ms"] = self._elapsed(step)

    def skip(self, step: str, detail: str = "") -> None:
        entry = self._entry(step)
        entry["status"] = "skipped"
        entry["detail"] = detail

    def fail(self, step: str, detail: str) -> None:
        entry = self._entry(step)
        entry["status"] = "failed"
        entry["detail"] = detail
        entry["ms"] = self._elapsed(step)

    def snapshot(self) -> list[dict]:
        return [dict(entry) for entry in self.steps]

    def persist(self, record: ReviewRecord) -> None:
        record.steps = self.snapshot()
        record.save(update_fields=["steps", "updated_at"])


def run_review(project_path: str, mr_iid: int, source: str = "webhook", force: bool = False,
               host: str = "") -> None:
    conf = get_conf()
    tracker = StepTracker()

    # -- stage 1: token
    tracker.start("token")
    repo = resolve_repo_token(conf, project_path, host)
    if repo is None:
        tracker.fail("token", f"没有匹配项目 `{project_path}` 的令牌配置，也未配置默认令牌")
        _record_early_failure(project_path, mr_iid, source, "no_token", "没有匹配该项目的令牌配置", tracker)
        logger.warning("no token configured for project %s; review aborted", project_path)
        return
    masked = f"{repo.token[:4]}****{repo.token[-4:]}" if repo.token else "(empty)"
    tracker.done("token", f"命中令牌配置「{repo.product or '默认'}」（{repo.match_url or '默认地址'}），令牌 {masked}")

    # same fallback the dashboard uses for the host check: a URL entry already
    # carries its own origin, but the default token path may leave it empty.
    client = GitLabClient(repo.api_url or conf.default_api_url, repo.token, timeout=conf.gitlab_timeout)

    # -- stage 2: read MR
    tracker.start("read_mr")
    try:
        mr = client.get_merge_request(project_path, mr_iid)
    except GitLabError as exc:
        tracker.fail("read_mr", str(exc)[:500])
        _record_early_failure(project_path, mr_iid, source, "read_mr_failed", str(exc)[:500], tracker)
        logger.error("cannot read MR %s!%s: %s", project_path, mr_iid, exc)
        return
    tracker.done("read_mr", f"分支 {mr.get('source_branch', '?')} -> {mr.get('target_branch', '?')}")

    head_sha = (mr.get("sha") or "").strip()
    if not head_sha:
        tracker.fail("read_mr", "MR 数据中没有 head sha")
        _record_early_failure(project_path, mr_iid, source, "no_head_sha", "MR 数据缺少 head sha", tracker)
        return

    record = try_register_review(
        project_path=project_path,
        mr_iid=mr_iid,
        head_sha=head_sha,
        mr_url=mr.get("web_url", ""),
        source=source,
        force=force,
    )
    if record is None:
        logger.info("review already recorded/in-flight for %s!%s@%s", project_path, mr_iid, head_sha[:8])
        return
    tracker.persist(record)

    progress_note_id: int | None = None
    try:
        if mr.get("work_in_progress") or mr.get("draft"):
            _skip_with_comment(client, project_path, mr_iid, tracker, record, "draft MR")
            _finish(record, tracker, "skipped", note_field="draft MR")
            return

        # -- stage 3: fetch diff
        tracker.start("fetch_diff")
        changes_resp = client.get_mr_changes(project_path, mr_iid)
        changes = changes_resp.get("changes") or []
        if not changes:
            tracker.done("fetch_diff", "0 个变更文件")
            _skip_with_comment(client, project_path, mr_iid, tracker, record, "no changes")
            _finish(record, tracker, "skipped", note_field="no changes")
            return

        diff_ctx = build_diff_context(
            changes,
            max_tokens=conf.max_diff_tokens,
            per_file_patch_chars=conf.per_file_patch_chars,
        )
        if not diff_ctx.files:
            tracker.done("fetch_diff", f"{len(changes)} 个文件，均无可评审内容（忽略规则命中/空 diff）")
            _skip_with_comment(client, project_path, mr_iid, tracker, record, "no reviewable files")
            _finish(record, tracker, "skipped", note_field="no reviewable files")
            return
        detail = f"{len(diff_ctx.files)} 个文件纳入评审，约 {estimate_tokens(diff_ctx.diff_text)} tokens"
        repo_context = ""
        if conf.context_enrichment:
            # Layer-1 enrichment: full changed files + symbol reference search
            # over the GitLab API; best-effort, failures degrade to diff-only.
            repo_context, ctx_stats = build_repo_context(
                client,
                project_path,
                head_sha,
                diff_ctx,
                max_files=conf.context_max_files,
                per_file_chars=conf.context_max_file_chars,
                max_symbols=conf.context_max_symbols,
            )
            if repo_context:
                detail += f"；API 上下文增强：{len(ctx_stats['files'])} 个文件全文"
                if ctx_stats["symbols"]:
                    detail += f"，{len(ctx_stats['symbols'])} 个符号的引用搜索"
                if not ctx_stats["search_available"]:
                    detail += "（实例未启用高级搜索，跳过引用搜索）"
        tracker.done("fetch_diff", detail)
        record.reviewed_files = diff_ctx.reviewed_files
        record.not_reviewed_files = diff_ctx.not_reviewed_files
        record.diff_chars = len(diff_ctx.diff_text)
        tracker.persist(record)

        progress_note_id = _create_progress_note(client, project_path, mr_iid)

        # -- stage 4: AI review (with optional model-driven context fetch rounds)
        tracker.start("ai_review")
        fetch_rounds = conf.context_fetch_rounds
        system = build_system_prompt(
            conf.num_max_findings, conf.language, allow_context_requests=fetch_rounds > 0
        )
        user = build_user_prompt(
            title=mr.get("title", ""),
            description=mr.get("description", "") or "",
            author=(mr.get("author") or {}).get("name", ""),
            source_branch=mr.get("source_branch", ""),
            target_branch=mr.get("target_branch", ""),
            commit_messages=mr.get("merge_commit_message", "") or "",
            diff_ctx=diff_ctx,
            repo_context=repo_context,
        )
        record.prompt_chars = len(system) + len(user)
        tracker.persist(record)

        llm_cfg = LLMConfig(
            base_url=conf.llm_base_url,
            api_key=conf.llm_api_key,
            model=conf.model,
            fallback_models=conf.fallback_models,
            timeout=conf.llm_timeout,
            max_output_tokens=conf.llm_max_output_tokens,
        )
        exclude_paths = (
            set(diff_ctx.reviewed_files) | set(diff_ctx.filtered_files) | set(diff_ctx.not_reviewed_files)
        )
        fetched_files: list[str] = []
        fetched_symbols: list[str] = []
        rounds_used = 0
        while True:
            reply, model_used = review_with_fallback(llm_cfg, system, user, conf.num_max_findings)
            if isinstance(reply, ReviewOutput):
                review = reply
                break
            # The model asked for more repository context via need_context.
            if rounds_used >= fetch_rounds:
                user += CONTEXT_EXHAUSTED_NOTE
                review, model_used = review_with_fallback(
                    llm_cfg, system, user, conf.num_max_findings, require_review=True
                )
                break
            sections, fetch_stats = fetch_requested_context(
                client,
                project_path,
                head_sha,
                reply,
                max_file_chars=conf.context_max_file_chars,
                exclude_paths=exclude_paths,
            )
            rounds_used += 1
            fetched_files.extend(fetch_stats["files"])
            fetched_symbols.extend(fetch_stats["symbols"])
            logger.info(
                "context fetch round %d on %s!%s: +%d file(s) +%d symbol(s), %d skipped",
                rounds_used, project_path, mr_iid,
                len(fetch_stats["files"]), len(fetch_stats["symbols"]), fetch_stats["skipped"],
            )
            if not sections:
                # Nothing could be fetched - force the final review now.
                user += CONTEXT_EXHAUSTED_NOTE
                review, model_used = review_with_fallback(
                    llm_cfg, system, user, conf.num_max_findings, require_review=True
                )
                break
            user += "\n\n## Additional context (fetched on model request)\n\n" + "\n\n".join(sections)
            record.prompt_chars = len(system) + len(user)
            tracker.persist(record)

        review, dropped_claims = filter_unverified_absence_issues(review)
        if dropped_claims:
            logger.info(
                "dropped %d unverified absence claim(s) on %s!%s: %s",
                len(dropped_claims),
                project_path,
                mr_iid,
                ", ".join(i.issue_header for i in dropped_claims),
            )
        record.model_used = model_used
        record.issues_count = len(review.key_issues_to_review)
        record.score = review.score
        record.risk_level = review.risk_level or ""
        ai_detail = f"模型 {model_used}，{len(review.key_issues_to_review)} 个问题"
        if rounds_used:
            ai_detail += (
                f"（主动补上下文 {rounds_used} 轮："
                f"+{len(fetched_files)} 文件 +{len(fetched_symbols)} 符号）"
            )
        if dropped_claims:
            ai_detail += f"（已过滤 {len(dropped_claims)} 条无证据的缺失类断言）"
        tracker.done("ai_review", ai_detail)

        # -- stage 5: publish
        tracker.start("publish")
        inline_published: set[int] = set()
        if conf.inline_comments and review.key_issues_to_review:
            inline_published = _publish_inline_comments(
                client,
                project_path,
                mr_iid,
                review,
                diff_ctx,
                changes_resp.get("diff_refs") or {},
                limit=conf.inline_max_comments,
            )

        summary_issues = [i for n, i in enumerate(review.key_issues_to_review) if n not in inline_published]
        # keep the summary-only issues on the record so the detail page can list
        # them alongside the reviewed/not-reviewed file panels
        record.non_inline_issues = [
            {
                "relevant_file": i.relevant_file,
                "start_line": i.start_line,
                "end_line": i.end_line,
                "issue_header": i.issue_header,
                "issue_content": i.issue_content,
                "kind": i.kind,
                "evidence": i.evidence,
            }
            for i in summary_issues
        ]
        body = render_review(
            _with_issues(review, summary_issues),
            mr_url=mr.get("web_url", ""),
            remaining_files=diff_ctx.not_reviewed_files,
            filtered_files=diff_ctx.filtered_files,
            language=conf.language,
        )
        mode = _publish_persistent_comment(client, project_path, mr_iid, body, conf.max_comment_chars)
        publish_detail = mode if not inline_published else f"{mode}（{len(inline_published)} 条行内评论）"
        tracker.done("publish", publish_detail)
        logger.info(
            "review %s!%s@%s published (%s), %d issue(s)",
            project_path,
            mr_iid,
            head_sha[:8],
            mode,
            record.issues_count,
        )
        _finish(record, tracker, "success")
    except GitLabError as exc:
        _fail_current(tracker, str(exc)[:500])
        record.error_reason, record.error_detail = "gitlab_error", str(exc)[:500]
        _publish_failure_quietly(client, project_path, mr_iid, "gitlab_error", conf.max_comment_chars)
        tracker.done("publish", "已发布失败说明评论")
        _finish(record, tracker, "failed")
    except LLMError as exc:
        tracker.fail("ai_review", f"{exc.reason}: {str(exc)[:400]}")
        record.error_reason, record.error_detail = exc.reason, str(exc)[:500]
        _publish_failure_quietly(client, project_path, mr_iid, exc.reason, conf.max_comment_chars)
        tracker.done("publish", "已发布失败说明评论")
        _finish(record, tracker, "failed")
    except Exception as exc:  # noqa: BLE001 - top-level guard for the worker thread
        logger.exception("unexpected review failure for %s!%s", project_path, mr_iid)
        _fail_current(tracker, type(exc).__name__)
        record.error_reason, record.error_detail = "unknown", type(exc).__name__
        _publish_failure_quietly(client, project_path, mr_iid, "unknown", conf.max_comment_chars)
        tracker.done("publish", "已发布失败说明评论")
        _finish(record, tracker, "failed")
    finally:
        if progress_note_id is not None:
            try:
                client.delete_note(project_path, mr_iid, progress_note_id)
            except GitLabError:
                logger.warning("failed to delete progress note on %s!%s", project_path, mr_iid)


# ---------------------------------------------------------------- helpers


def _total_ms(tracker: StepTracker) -> int:
    return sum(entry["ms"] for entry in tracker.steps)


def _fail_current(tracker: StepTracker, detail: str) -> None:
    """Mark the in-flight stage as failed (or the publish stage if none running)."""
    running = next((e["step"] for e in tracker.steps if e["status"] == "running"), None)
    tracker.fail(running or "publish", detail)


def _skip_with_comment(
    client: GitLabClient,
    project_path: str,
    mr_iid: int,
    tracker: StepTracker,
    record: ReviewRecord,
    reason: str,
) -> None:
    """A skipped review is never silent: publish an explanatory MR comment."""
    tracker.skip("ai_review", f"跳过：{reason}")
    tracker.start("publish")
    try:
        client.create_note(project_path, mr_iid, render_skip(reason))
        tracker.done("publish", "已发布“未评审”说明评论")
    except GitLabError as exc:
        tracker.fail("publish", str(exc)[:500])
        logger.warning("failed to publish skip comment on %s!%s: %s", project_path, mr_iid, exc)
    record.error_reason = ""
    record.error_detail = reason


def _record_early_failure(
    project_path: str, mr_iid: int, source: str, reason: str, detail: str, tracker: StepTracker
) -> None:
    """Persist pre-registration failures (bad token, unreadable MR) so they are
    visible on the dashboard. head_sha stays empty - nothing was reviewed yet."""
    try:
        record = ReviewRecord.objects.filter(project_path=project_path, mr_iid=mr_iid, head_sha="").first()
        if record is None:
            record = ReviewRecord(project_path=project_path, mr_iid=mr_iid, head_sha="", source=source)
        record.status = "failed"
        record.error_reason = reason
        record.error_detail = detail
        record.duration_ms = _total_ms(tracker)
        record.steps = tracker.snapshot()
        record.save()
    except Exception:  # noqa: BLE001 - reporting must never crash the worker
        logger.exception("failed to persist early failure for %s!%s", project_path, mr_iid)


def _finish(record: ReviewRecord, tracker: StepTracker, status: str, note_field: str = "") -> None:
    record.status = status
    record.duration_ms = _total_ms(tracker)
    if note_field:
        record.error_detail = note_field
    record.steps = tracker.snapshot()
    record.save()


def _with_issues(review: ReviewOutput, issues: list[KeyIssue]) -> ReviewOutput:
    """Return a copy of the review limited to the issues that stay in the summary."""
    if len(issues) == len(review.key_issues_to_review):
        return review
    data = review.model_dump()
    data["key_issues_to_review"] = [i.model_dump() for i in issues]
    return ReviewOutput.model_validate(data)


def _create_progress_note(client: GitLabClient, project_path: str, mr_iid: int) -> int | None:
    try:
        note = client.create_note(project_path, mr_iid, render_progress())
        return int(note.get("id")) if note.get("id") else None
    except GitLabError:
        logger.warning("could not create progress note on %s!%s", project_path, mr_iid)
        return None


def _publish_persistent_comment(
    client: GitLabClient, project_path: str, mr_iid: int, body: str, max_chars: int
) -> str:
    """Update our previous review comment in place, or create one.

    Update requires BOTH the identity marker and authorship by this bot account;
    otherwise a fresh comment is created - we never overwrite a human-edited one.
    """
    body = truncate(body, max_chars)
    own_id = client.get_own_user_id()
    if own_id is not None:
        for note in client.list_notes(project_path, mr_iid):
            if REVIEW_MARKER not in (note.get("body") or ""):
                continue
            author_id = ((note.get("author") or {}).get("id"))
            if author_id is not None and int(author_id) == int(own_id):
                client.update_note(project_path, mr_iid, int(note["id"]), body)
                return "updated"
    client.create_note(project_path, mr_iid, body)
    return "created"


def _publish_inline_comments(
    client: GitLabClient,
    project_path: str,
    mr_iid: int,
    review: ReviewOutput,
    diff_ctx: DiffContext,
    diff_refs: dict,
    limit: int = MAX_INLINE_COMMENTS,
) -> set[int]:
    """Publish key issues as inline discussions; returns indices published.

    Two gates decide whether an issue may go inline:

    1. **only files with a diff** - the file must be in the diff the model was
       given, and ``start_line`` must be a line that actually exists in that
       file's new version (a hunk line, not a gap between hunks). Anything else
       would either 400 on GitLab's position API or annotate code the model
       never saw.
    2. **budget** - the budget is clamped to ``MAX_INLINE_COMMENTS`` here, so a
       huge ``num_max_findings`` (or a model that ignores the prompt) can never
       flood the MR with threads.

    Issues that fail either gate stay in the summary comment, so nothing is ever
    silently dropped.
    """
    budget = min(int(limit), MAX_INLINE_COMMENTS) if limit is not None else MAX_INLINE_COMMENTS
    budget = max(budget, 0)
    if budget == 0:
        return set()

    commentable = diff_ctx.commentable_lines_by_file
    own_id = client.get_own_user_id()
    existing: set[tuple[str, int]] = set()
    if own_id is not None:
        for discussion in client.list_discussions(project_path, mr_iid):
            for note in discussion.get("notes") or []:
                if ((note.get("author") or {}).get("id")) != own_id:
                    continue
                position = note.get("position") or {}
                if position.get("new_path") and position.get("new_line") is not None:
                    existing.add((position["new_path"], int(position["new_line"])))

    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    published: set[int] = set()
    if not (base_sha and head_sha):
        return published

    for index, issue in enumerate(review.key_issues_to_review):
        if len(published) >= budget:
            logger.info(
                "inline comment budget %d reached, %d issue(s) left in summary",
                budget,
                len(review.key_issues_to_review) - index,
            )
            break
        file_lines = commentable.get(issue.relevant_file)
        if not file_lines:
            logger.warning(
                "inline issue skipped, file has no reviewable diff in this MR: %s", issue.relevant_file
            )
            continue
        if issue.start_line not in file_lines:
            logger.warning(
                "inline issue skipped, line %d is not part of the diff hunks of %s",
                issue.start_line,
                issue.relevant_file,
            )
            continue
        if (issue.relevant_file, issue.start_line - 1) in existing:
            published.add(index)
            continue
        position = {
            "position_type": "text",
            "base_sha": base_sha,
            "start_sha": start_sha or base_sha,
            "head_sha": head_sha,
            "new_path": issue.relevant_file,
            "old_path": issue.relevant_file,
            "new_line": issue.start_line - 1,  # GitLab positions are 0-based
        }
        body = f"{issue.issue_content.strip()}\n<!-- gitlabmr:inline -->"
        try:
            client.create_inline_discussion(project_path, mr_iid, body, position)
            published.add(index)
        except GitLabError as exc:
            logger.warning("inline comment failed for %s L%d: %s", issue.relevant_file, issue.start_line, exc)
    return published


def _publish_failure_quietly(client: GitLabClient, project_path: str, mr_iid: int, reason: str, max_chars: int) -> None:
    try:
        client.create_note(project_path, mr_iid, truncate(render_failure(reason), max_chars))
    except Exception:  # noqa: BLE001 - never mask the original failure
        logger.exception("failed to publish the failure comment on %s!%s", project_path, mr_iid)
