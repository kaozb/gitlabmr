"""Resolve a GitLab project path to its credentials via longest-prefix match."""

from __future__ import annotations

from ..conf import Conf, RepoToken


def resolve_repo_token(conf: Conf, project_path: str) -> RepoToken | None:
    """Return the RepoToken whose prefix matches ``project_path`` longest, else the default.

    ``project_path`` looks like ``group/subgroup/repo``. Prefix matching is
    segment-aware: prefix ``backend`` matches ``backend/api`` but not ``backendx``.
    Returns None when nothing matches and no default token is configured.
    """
    path = (project_path or "").strip("/").lower()
    best: RepoToken | None = None
    for entry in conf.repo_tokens:
        if path == entry.prefix or path.startswith(entry.prefix + "/"):
            if best is None or len(entry.prefix) > len(best.prefix):
                best = entry
    if best is not None:
        return best
    if conf.default_token:
        return RepoToken(prefix="", api_url=conf.default_api_url, token=conf.default_token)
    return None
