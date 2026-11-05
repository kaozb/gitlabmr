"""Build the model-facing diff context from GitLab MR changes.

Pipeline (mirrors PR-Agent's get_pr_diff, simplified):
1. filter files (ignore globs, binary/oversized patches) - skipped files stay listed
2. render each patch with new-file line numbers ("12 + added", "13   context")
3. fit into the token budget: full text -> drop context lines -> drop smallest files
4. expose added/deleted/renamed lists and the not-reviewed list for the footer
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

DEFAULT_IGNORE_GLOBS = (
    "*.lock", "*.min.js", "*.min.css", "*.map", "*.svg", "*.png", "*.jpg", "*.jpeg",
    "*.gif", "*.ico", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.pdf", "*.zip",
    "*.jar", "*.so", "*.dylib", "*.bin", "*.pem", "*.crt", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock",
    "go.sum", ".gitignore", "*.md", "*.txt", "*.csv", "*.json.tmp",
)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token. Good enough for budgeting."""
    return max(1, len(text) // 4)


def is_ignored(path: str, ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob) for glob in ignore_globs)


def number_patch_lines(patch: str) -> tuple[str, int]:
    """Render a unified diff patch with new-file line numbers.

    Context and added lines get their new-file line number; removed lines are
    kept with a bare ``-`` prefix. Returns (rendered_text, max_new_line_seen).
    """
    out: list[str] = []
    new_line = 0
    max_new_line = 0
    for line in patch.splitlines():
        match = HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if line.startswith(("--- ", "+++ ", "diff --git", "index ", "\\ No newline")):
            continue
        if line.startswith("+"):
            out.append(f"{new_line} + {line[1:]}")
            max_new_line = max(max_new_line, new_line)
            new_line += 1
        elif line.startswith("-"):
            out.append(f"    - {line[1:]}")
        else:
            out.append(f"{new_line}   {line.removeprefix(' ')}")
            max_new_line = max(max_new_line, new_line)
            new_line += 1
    return "\n".join(out), max_new_line


def strip_context_lines(patch: str) -> str:
    """Keep only +/- lines and hunk headers; drops pure context lines."""
    kept = []
    for line in patch.splitlines():
        if line.startswith(("@@", "+", "-")) and not line.startswith(("--- ", "+++ ")):
            kept.append(line)
    return "\n".join(kept)


@dataclass
class DiffFile:
    new_path: str
    old_path: str
    patch: str
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    max_new_line: int = 0
    rendered: str = ""

    @property
    def edit_kind(self) -> str:
        if self.new_file:
            return "added"
        if self.deleted_file:
            return "deleted"
        if self.renamed_file:
            return "renamed"
        return "modified"


@dataclass
class DiffContext:
    diff_text: str = ""
    files: list[DiffFile] = field(default_factory=list)
    reviewed_files: list[str] = field(default_factory=list)
    not_reviewed_files: list[str] = field(default_factory=list)
    filtered_files: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    renamed_files: list[str] = field(default_factory=list)

    @property
    def max_new_line_by_file(self) -> dict[str, int]:
        return {f.new_path: f.max_new_line for f in self.files}


def build_diff_context(
    changes: list[dict],
    *,
    max_tokens: int,
    per_file_patch_chars: int = 60000,
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS,
) -> DiffContext:
    """Turn raw GitLab ``changes`` entries into a budget-fitted prompt diff."""
    ctx = DiffContext()
    for change in changes or []:
        path = change.get("new_path") or ""
        patch = change.get("diff") or ""
        if not path:
            continue
        if is_ignored(path, ignore_globs):
            ctx.filtered_files.append(path)
            continue
        if not patch:
            # Binary or unchanged-content entry; nothing to review.
            ctx.filtered_files.append(path)
            continue
        if len(patch) > per_file_patch_chars:
            ctx.not_reviewed_files.append(path)
            continue
        if change.get("new_file"):
            ctx.added_files.append(path)
        elif change.get("deleted_file"):
            ctx.deleted_files.append(path)
        elif change.get("renamed_file"):
            ctx.renamed_files.append(path)
        rendered, max_new_line = number_patch_lines(patch)
        ctx.files.append(
            DiffFile(
                new_path=path,
                old_path=change.get("old_path") or path,
                patch=patch,
                new_file=bool(change.get("new_file")),
                deleted_file=bool(change.get("deleted_file")),
                renamed_file=bool(change.get("renamed_file")),
                max_new_line=max_new_line,
                rendered=rendered,
            )
        )

    if not ctx.files:
        return ctx

    def file_block(file: DiffFile, body: str) -> str:
        return f"## File: '{file.new_path}'\n\n{body}"

    # Pass 1: full rendered diff.
    blocks = [file_block(f, f.rendered) for f in ctx.files]
    if estimate_tokens("\n\n".join(blocks)) <= max_tokens:
        ctx.diff_text = "\n\n".join(blocks)
        ctx.reviewed_files = [f.new_path for f in ctx.files]
        return ctx

    # Pass 2: drop context lines, keep +/- only.
    compact = [file_block(f, strip_context_lines(f.patch)) for f in ctx.files]
    if estimate_tokens("\n\n".join(compact)) <= max_tokens:
        ctx.diff_text = "\n\n".join(compact)
        ctx.reviewed_files = [f.new_path for f in ctx.files]
        return ctx

    # Pass 3: compact + drop smallest files until it fits.
    order = sorted(range(len(ctx.files)), key=lambda i: estimate_tokens(ctx.files[i].patch))
    included: set[int] = set()
    budget = 0
    for i in order:
        candidate = estimate_tokens(file_block(ctx.files[i], strip_context_lines(ctx.files[i].patch)))
        if budget + candidate <= max_tokens:
            included.add(i)
            budget += candidate
        else:
            ctx.not_reviewed_files.append(ctx.files[i].new_path)
    if included:
        ctx.diff_text = "\n\n".join(compact[i] for i in sorted(included))
        ctx.reviewed_files = [ctx.files[i].new_path for i in sorted(included)]
    return ctx


def build_inventory_section(ctx: DiffContext) -> str:
    """Trailing metadata so the model knows what it did not see."""
    lines: list[str] = []
    if ctx.added_files:
        lines.append("ADDED FILES:\n" + "\n".join(ctx.added_files))
    if ctx.deleted_files:
        lines.append("DELETED FILES:\n" + "\n".join(ctx.deleted_files))
    if ctx.renamed_files:
        lines.append("RENAMED FILES:\n" + "\n".join(ctx.renamed_files))
    if ctx.not_reviewed_files:
        lines.append("FILES NOT INCLUDED IN THIS REVIEW (do not comment on them):\n" + "\n".join(ctx.not_reviewed_files))  # noqa: E501
    return "\n\n".join(lines)
