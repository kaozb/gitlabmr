"""Layer-1 repository context enrichment over the GitLab REST API (no clone).

The reviewer model normally sees only the MR diff. That blind spot produces
both false positives ("this method is unused" - the caller lives outside the
diff) and false negatives (a signature change whose callers are never
updated). This module narrows the gap with read-only GitLab endpoints:

1. full content of the changed files at the MR head sha (raw files API), so
   the model can check imports/definitions against the whole file, not just
   the hunks;
2. blob-search results for symbols the diff defines or touches, so claims
   like "unused" / "no caller updated" become evidence-backed instead of
   guesses.

Everything here is best-effort: any API failure degrades to "no extra
context" and the review proceeds on the diff alone - enrichment must never
fail the pipeline.
"""

from __future__ import annotations

import logging
import re

from .diffctx import DiffContext
from .gitlab import GitLabClient, GitLabError

logger = logging.getLogger("gitlabmr.repoctx")

# Hard cap for the accumulated search-reference block, independent of the
# per-file budget: reference lists grow line by line and can quietly bloat.
SEARCH_REF_BLOCK_CHARS = 8000

# Definition-ish lines on added diff lines, across common languages:
# def/class (Python), function (JS/TS), func (Go), fn/struct/trait/enum/impl
# (Rust), interface/type (TS/Go/Scala). Modifier keywords are skipped.
DEF_RE = re.compile(
    r"^\s*(?:pub\s+|export\s+|default\s+|async\s+|abstract\s+|final\s+|static\s+"
    r"|public\s+|private\s+|protected\s+)*"
    r"(?:def|class|function|func|fn|struct|interface|impl|trait|enum|type)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)

MIN_SYMBOL_LEN = 4


def extract_symbols(diff_ctx: DiffContext, limit: int) -> list[str]:
    """Identifiers worth a repo-wide usage lookup, taken from added lines only.

    Definitions written by this MR are the strongest signal for "find its
    callers"; newly added files also contribute their module name. Order
    follows the diff, duplicates removed, result capped at ``limit``.
    """
    symbols: list[str] = []
    seen: set[str] = set()
    reviewed = set(diff_ctx.reviewed_files)

    def push(name: str) -> bool:
        if len(name) < MIN_SYMBOL_LEN or name.startswith("_") or name in seen:
            return False
        seen.add(name)
        symbols.append(name)
        return True

    for f in diff_ctx.files:
        if f.new_path not in reviewed:
            continue
        if f.new_file:
            push(f.new_path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        for line in f.patch.splitlines():
            if not line.startswith("+"):
                continue
            match = DEF_RE.match(line[1:])
            if match:
                push(match.group(1))
            if len(symbols) >= limit:
                return symbols
    return symbols


def _render_numbered(content: str, max_chars: int) -> str:
    """Number every line so the model can cite ``path:LINE`` as evidence."""
    body = "\n".join(f"{n:>5}  {text}" for n, text in enumerate(content.splitlines(), start=1))
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n… (truncated)"
    return body


def _search_references(
    client: GitLabClient,
    project: str,
    ref: str,
    symbols: list[str],
    *,
    exclude_paths: set[str],
) -> tuple[list[str], bool, list[str]]:
    """Blob-search each symbol; returns (blocks, search_available, searched).

    ``search_available=False`` means the instance rejected the query because
    advanced search is not enabled - remaining symbols are then skipped
    instead of hammering an endpoint that cannot answer.
    """
    blocks: list[str] = []
    searched: list[str] = []
    for symbol in symbols:
        try:
            results = client.search_blobs(project, symbol, ref)
        except GitLabError as exc:
            logger.warning("blob search failed for %r on %s: %s", symbol, project, exc)
            continue
        if not isinstance(results, list):
            return blocks, False, searched  # advanced search unavailable
        searched.append(symbol)
        hits: list[str] = []
        for item in results:
            path = item.get("path") or ""
            if not path or path in exclude_paths:
                continue  # usage inside the diff the model already sees
            snippet = (item.get("data") or "").strip().splitlines()
            first = snippet[0].strip()[:160] if snippet else ""
            line = item.get("startline")
            hits.append(f"- `{path}:{line}`: {first}" if line else f"- `{path}`: {first}")
            if len(hits) >= 5:
                break
        if hits:
            blocks.append(f"#### `{symbol}`\n" + "\n".join(hits))
        if sum(len(b) for b in blocks) > SEARCH_REF_BLOCK_CHARS:
            break
    return blocks, True, searched


def build_repo_context(
    client: GitLabClient,
    project: str,
    ref: str,
    diff_ctx: DiffContext,
    *,
    max_files: int = 6,
    per_file_chars: int = 20000,
    max_symbols: int = 8,
) -> tuple[str, dict]:
    """Fetch extra context via API; returns (markdown section, stats).

    The section is meant to be embedded in the user prompt and declared as
    citable evidence. Best-effort by design: GitLab errors on individual
    files/symbols are logged and skipped; a global "no context" outcome is a
    perfectly valid result.
    """
    stats = {"files": [], "symbols": [], "search_available": True, "symbols_with_hits": 0}
    if not diff_ctx.reviewed_files:
        return "", stats

    sections: list[str] = []
    exclude = set(diff_ctx.reviewed_files) | set(diff_ctx.filtered_files) | set(diff_ctx.not_reviewed_files)

    if max_files > 0:
        # Smallest patches first: whole-file content adds the most where the
        # diff shows the least, and more files fit under the same budget.
        candidates = sorted(
            (f for f in diff_ctx.files if f.new_path in set(diff_ctx.reviewed_files)),
            key=lambda f: len(f.patch),
        )
        for f in candidates[:max_files]:
            try:
                content = client.get_file_content(project, f.new_path, ref)
            except GitLabError as exc:
                logger.warning("file content unavailable for %s@%s: %s", f.new_path, ref[:8], exc)
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            body = _render_numbered(content, per_file_chars)
            sections.append(f"### Full content: `{f.new_path}`\n\n```\n{body}\n```")
            stats["files"].append(f.new_path)

    if max_symbols > 0:
        blocks, available, searched = _search_references(
            client, project, ref, extract_symbols(diff_ctx, max_symbols), exclude_paths=exclude
        )
        stats["symbols"] = searched
        stats["search_available"] = available
        if blocks:
            sections.append("### References found by repo search (usage evidence)\n" + "\n\n".join(blocks))
            stats["symbols_with_hits"] = len(blocks)

    if not sections:
        return "", stats
    header = f"## Repository context (fetched via GitLab API at ref `{ref[:12]}`)\n"
    return header + "\n\n".join(sections), stats
