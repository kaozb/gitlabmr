"""Resolve a GitLab project to its credentials via longest URL-prefix match.

Each entry's ``match_url`` is a full GitLab URL prefix such as
``https://gitlab.example.com/tp/CNEOSv``; the real repo
``https://gitlab.example.com/tp/CNEOSv2/Engine/epi-5.0`` is matched by every
entry whose URL it starts with, and the LONGEST such URL wins (so the set is
treated as one collection - ``.../tp``, ``.../tp/CNEOSv`` and
``.../tp/CNEOSv2/Engin`` may all hit, but only the deepest one's token is
used). Matching is case-insensitive end to end, on the canonical forms from
:func:`reviewer.conf.normalize_match_url` (no trailing slash either).
"""

from __future__ import annotations

from ..conf import Conf, RepoToken, normalize_match_url, normalize_prefix


def build_project_url(conf: Conf, project_path: str, host: str = "") -> str:
    """Canonical full URL of a project, for URL-prefix matching.

    ``host`` (from a pasted MR link or a webhook payload) wins when given;
    otherwise the default GitLab address provides the scheme://host part. Both
    go through normalize_match_url so matching compares canonical forms.
    """
    origin = normalize_match_url(host) if host else normalize_match_url(conf.default_api_url)
    path = normalize_prefix(project_path)
    return f"{origin}/{path}" if (origin and path) else origin


def match_key(repo: RepoToken) -> str:
    """Canonical match URL of an entry (already normalised at load time)."""
    return normalize_match_url(repo.match_url)


def resolve_repo_token(conf: Conf, project_path: str, host: str = "") -> RepoToken | None:
    """Return the RepoToken whose match URL covers this project longest, else the default.

    Every enabled entry forms one candidate set; an entry matches when its
    canonical match URL is a prefix of the project's full URL, and the longest
    matching URL wins (matching is case-insensitive end to end). Falls back to
    the default token (matched by the default address alone) when no entry
    covers the project; returns None when there is no default token either.
    """
    project_url = build_project_url(conf, project_path, host)
    best: RepoToken | None = None
    best_len = -1
    for entry in conf.repo_tokens:
        match_url = match_key(entry)
        if not match_url or not project_url.startswith(match_url):
            continue
        if len(match_url) > best_len:
            best, best_len = entry, len(match_url)
    if best is not None:
        return best
    default_url = normalize_match_url(conf.default_api_url)
    if conf.default_token and (not default_url or project_url.startswith(default_url)):
        return RepoToken(match_url=default_url, api_url=conf.default_api_url, token=conf.default_token)
    return None
