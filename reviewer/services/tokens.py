"""Resolve a GitLab project path to its credentials via longest-prefix match."""

from __future__ import annotations

from dataclasses import replace

from ..conf import Conf, RepoToken, normalize_prefix


def resolve_repo_token(conf: Conf, project_path: str) -> RepoToken | None:
    """Return the RepoToken whose prefix matches ``project_path`` longest, else the default.

    The configured prefixes are treated as one set: every prefix is tried against
    the project path (case-insensitive, plain string prefix - not segment aware,
    so ``tp/cneos`` also covers ``tp/CNEOSv2/...``), and the entry whose prefix is
    the longest wins. ``tp``, ``tp/`` and ``TP`` are all the same prefix once
    normalised. Returns None when nothing matches and no default token is set.
    """
    path = normalize_prefix(project_path)
    best: RepoToken | None = None
    best_len = -1
    for entry in conf.repo_tokens:
        prefix = normalize_prefix(entry.prefix)
        if not prefix:  # a blank prefix would swallow every project path
            continue
        if not (path == prefix or path.startswith(prefix)):
            continue
        if len(prefix) > best_len:
            # Keep the original entry but expose the canonical prefix for logs/UI.
            best, best_len = (entry if entry.prefix == prefix else replace(entry, prefix=prefix)), len(prefix)
    if best is not None:
        return best
    if conf.default_token:
        return RepoToken(prefix="", api_url=conf.default_api_url, token=conf.default_token)
    return None
