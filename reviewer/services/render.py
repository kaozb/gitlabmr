"""Render structured review output into the MR comment body.

Includes the hidden identity marker so the next run can find and update this
comment in place (only when the author is verifiably our own bot account).
"""

from __future__ import annotations

from .llm import ReviewOutput

REVIEW_MARKER = "<!-- gitlabmr:review -->"
PROGRESS_MARKER = "<!-- gitlabmr:progress -->"
RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _format_security(value: str | None) -> str:
    if not value or value.strip().lower() == "no":
        return "No"
    return value.strip()


def render_review(
    review: ReviewOutput,
    *,
    remaining_files: list[str] | None = None,
    filtered_files: list[str] | None = None,
    language: str = "zh-CN",
) -> str:
    lines: list[str] = ["## 🤖 MR AI 评审 / AI Review（技术平台部提供支持）", ""]
    if review.overall_assessment:
        lines += [review.overall_assessment.strip(), ""]

    meta: list[str] = []
    if review.score is not None:
        meta.append(f"**评分 Score**: {review.score}/100")
    if review.risk_level:
        meta.append(f"**风险 Risk**: {RISK_EMOJI.get(review.risk_level, '')} {review.risk_level}")
    if review.relevant_tests:
        meta.append(f"**测试 Tests**: {review.relevant_tests}")
    security = _format_security(review.security_concerns)
    meta.append(f"**安全 Security**: {security}")
    lines += [" | ".join(meta), ""]

    issues = review.key_issues_to_review
    if issues:
        lines += [f"### ⚡ 关键问题 ({len(issues)})", ""]
        for index, issue in enumerate(issues, start=1):
            location = f"`{issue.relevant_file}` L{issue.start_line}-L{issue.end_line}"
            lines += [f"**{index}. {issue.issue_header.strip()}** — {location}", "", issue.issue_content.strip()]
            if issue.kind == "verified" and issue.evidence:
                lines += ["", f"> 证据 Evidence: `{issue.evidence.strip()}`"]
            lines += [""]
    else:
        lines += ["### ✅ 未发现关键问题", "", "未发现明确的、可定位的问题。", ""]

    footers: list[str] = []
    if remaining_files:
        shown = ", ".join(f"`{name}`" for name in remaining_files[:20])
        extra = len(remaining_files) - 20
        footers.append(f"⚠️ **未纳入评审**（超出 token 预算）: {shown}" + (f" 等 {extra} 个文件" if extra > 0 else ""))
    if filtered_files:
        footers.append(f"ℹ️ 已跳过 {len(filtered_files)} 个忽略规则命中的文件（lock/资源/文档类）")
    if footers:
        lines += ["---", *footers, ""]

    # Invisible identity marker only - the next run finds and updates this
    # comment in place, and human-comment filtering relies on it. No visible
    # attribution footer: comments read like a normal reviewer's opinion.
    lines += ["---", REVIEW_MARKER]
    return "\n".join(lines)


def render_failure(reason: str, detail: str = "") -> str:
    """Deterministic, sanitized failure comment. Never leaks raw exceptions."""
    reasons = {
        "timeout": "模型服务超时，请稍后重试（/review）。",
        "http_error": "模型服务返回错误（限流/认证/配额），请检查服务端日志。",
        "truncated": "模型输出被截断，请重试或减小 diff 预算。",
        "bad_yaml": "模型输出无法解析为结构化结果，已重试全部模型。",
        "bad_schema": "模型输出不符合约定 schema，已重试全部模型。",
        "all_models_failed": "所有配置的模型均调用失败。",
        "gitlab_error": "读取 MR 数据或回写评论时 GitLab API 出错。",
    }
    message = reasons.get(reason, "评审过程中发生未知错误，请检查服务端日志。")
    body = [
        "## ⚠️ MR AI 评审失败",
        "",
        message,
        "",
        f"原因归类: `{reason}`",
        "",
        PROGRESS_MARKER,
        REVIEW_MARKER,
    ]
    return "\n".join(body)


def render_progress() -> str:
    return "⏳ AI 评审进行中，请稍候……" "\n" + PROGRESS_MARKER


SKIP_REASONS = {
    "draft MR": "该 MR 处于草稿（WIP）状态，暂不进行 AI 评审。",
    "no changes": "相比目标分支没有任何代码变更。",
    "no reviewable files": "变更中没有可评审的代码文件（二进制/锁文件/纯资源类改动已按规则跳过）。",
}


def render_skip(reason_detail: str) -> str:
    """Comment published on the MR even when the review is skipped (never silent)."""
    message = SKIP_REASONS.get(reason_detail, "本次未进行 AI 评审。")
    return "\n".join(
        [
            "## ℹ️ 本次未进行 AI 评审 / Skipped",
            "",
            message,
            "",
            "当前 MR 没有评审价值；如后续有新的代码变更，将自动重新评审。",
        ]
    )


def truncate(body: str, max_chars: int) -> str:
    """Hard-limit the comment size while preserving the identity marker."""
    marker = REVIEW_MARKER
    suffix = f"\n\n…（内容过长已截断）\n{marker}"
    if len(body) <= max_chars:
        return body
    keep = max(0, max_chars - len(suffix))
    return body[:keep].rstrip() + suffix
