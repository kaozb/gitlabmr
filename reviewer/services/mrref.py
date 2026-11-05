"""Parse what the user pastes into the dashboard's manual-trigger box.

The primary format is the MR page URL copied straight from the browser, e.g.
``https://gitlab.example.com/group/sub/repo/-/merge_requests/42``; the
``group/repo!42`` shortcut is accepted too because it is easy to type in chat.
Everything is parsed locally - this module never touches the network.

Project paths keep their original casing (webhook payloads do the same), so the
manual and webhook paths produce the same ``ReviewRecord`` identity; prefix
matching in :mod:`reviewer.services.tokens` is case-insensitive by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EXAMPLE_URL = "https://gitlab.example.com/group/repo/-/merge_requests/42"


class MRRefError(ValueError):
    """The pasted text is not a recognisable merge-request reference."""


@dataclass(frozen=True)
class MRRef:
    """A merge request referenced by user input."""

    project_path: str  # "group/sub/repo", original casing, no surrounding slashes
    iid: int
    host: str = ""  # GitLab hostname when the input was a URL ("" otherwise)
    url: str = ""  # the pasted text, echoed back to the user

    @property
    def short(self) -> str:
        return f"{self.project_path}!{self.iid}"


# .../group/sub/repo/-/merge_requests/42, optionally without the "/-" (legacy)
_MR_URL_RE = re.compile(
    r"(?<!\S)(?:(?P<scheme>[a-z][a-z0-9+.-]*://))?"
    r"(?P<host>[^\s/?#]+)/"
    r"(?P<project>[^\s?#]*?)/?(?:-/)?/?merge_requests/(?P<iid>\d+)",
    re.IGNORECASE,
)

# group/sub/repo!42 / "group/sub/repo 42" / group/sub/repo#42
_MR_SHORT_RE = re.compile(r"(?P<project>[^\s!！#]+?)\s*(?:[!！#]|\s)\s*(?P<iid>\d+)$")

_HOSTLIKE = re.compile(r"[.:]|localhost", re.IGNORECASE)


def _clean_project(raw: str) -> str:
    project = (raw or "").strip().strip("/")
    segments = project.split("/")
    if not project or len(segments) < 2 or any(not seg.strip() for seg in segments):
        raise MRRefError(f"无法识别项目路径，请粘贴完整的 MR 链接，例如 {EXAMPLE_URL}")
    return project


def _hostname(raw: str) -> str:
    """Host part of an URL (port and credentials dropped, lower-cased)."""
    host = (raw or "").rsplit("@", 1)[-1].strip()
    for sep in (":",):
        host = host.split(sep, 1)[0]
    return host.strip().lower()


def _ref(project: str, iid: str, *, host: str = "", url: str = "") -> MRRef:
    try:
        number = int(iid)
    except (TypeError, ValueError):
        raise MRRefError(f"MR 编号必须是数字：{iid}") from None
    if number <= 0:
        raise MRRefError(f"MR 编号无效：{iid}")
    return MRRef(project_path=_clean_project(project), iid=number, host=host, url=url)


def parse_mr_ref(raw: str) -> MRRef:
    """Turn a pasted MR link (or ``project!iid`` shortcut) into an :class:`MRRef`."""
    text = (raw or "").strip()
    if not text:
        raise MRRefError(f"请粘贴 MR 链接，例如 {EXAMPLE_URL}")

    match = _MR_URL_RE.search(text)
    if match:
        host = match.group("host")
        project = match.group("project")
        # "group/repo/-/merge_requests/42" pasted without a scheme: keep the
        # first segment as part of the project path instead of a hostname.
        if not match.group("scheme") and not _HOSTLIKE.search(host):
            project = f"{host}/{project}"
            host = ""
        return _ref(project, match.group("iid"), host=_hostname(host) if host else "", url=text)

    match = _MR_SHORT_RE.match(text)
    if match:
        return _ref(match.group("project"), match.group("iid"), url=text)

    raise MRRefError(f"无法识别 MR 链接，请粘贴浏览器地址栏里的 MR 页面链接，例如 {EXAMPLE_URL}")
