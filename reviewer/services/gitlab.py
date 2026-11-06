"""Thin GitLab REST v4 client: only the endpoints the review pipeline needs.

Endpoints used:
- GET  /user                                    identify the bot account
- GET  /projects/:id/merge_requests/:iid        MR metadata + diff_refs
- GET  /projects/:id/merge_requests/:iid/changes  file diffs
- GET  /projects/:id/repository/files/:path/raw   full file content at a ref
- GET  /projects/:id/search?scope=blobs           repo-wide code search
- GET/POST/PUT/DELETE /projects/:id/merge_requests/:iid/notes[/:id]
- GET/POST /projects/:id/merge_requests/:iid/discussions
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import requests

from ..conf import api_origin

logger = logging.getLogger("gitlabmr.gitlab")


class GitLabError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _mask(token: str) -> str:
    return (token[:8] + "...") if len(token) > 8 else "***"


def probe_token(api_url: str, token: str, timeout: int = 10) -> tuple[bool, str, str]:
    """Probe whether a token is accepted by ``GET /api/v4/user``.

    Returns ``(valid, message, owner)`` where ``owner`` is the login name of
    the account the token belongs to (empty when the probe failed).

    Only 401/403 mean the token is *definitively* rejected; timeouts, DNS
    failures and 5xx are network/server conditions and must never be treated
    as "token dead" (callers rely on that to avoid disabling live tokens).
    """
    if not token:
        return False, "未配置令牌", ""
    try:
        resp = requests.get(
            f"{api_origin(api_url)}/api/v4/user",
            headers={"PRIVATE-TOKEN": token},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"网络异常（{exc.__class__.__name__}），无法判定", ""
    if resp.status_code == 200:
        try:
            owner = resp.json().get("username", "") or ""
        except ValueError:
            owner = ""
        return True, "有效", owner
    if resp.status_code in (401, 403):
        reason = "令牌已失效/被吊销" if resp.status_code == 401 else "令牌无 API 权限"
        return False, f"无效（{resp.status_code} {reason}）", ""
    return False, f"服务异常（HTTP {resp.status_code}），无法判定", ""


class GitLabClient:
    def __init__(self, api_url: str, token: str, timeout: int = 15):
        # The API always lives at <origin>/api/v4; normalize away any path the
        # entry carried (see conf.api_origin) so a project page URL can never
        # silently route requests to the web UI instead of the REST endpoint.
        self.api_url = api_origin(api_url)
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"PRIVATE-TOKEN": token})
        self._own_user_id: int | None = None

    # -- low level ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.api_url}/api/v4{path}"

    def _request(self, method: str, path: str, *, ok_codes=(200, 201, 204), **kwargs) -> requests.Response:
        try:
            resp = self._session.request(method, self._url(path), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise GitLabError(f"gitlab {method} {path.split('?')[0]} failed: {type(exc).__name__}") from exc
        if resp.status_code not in ok_codes:
            raise GitLabError(
                f"gitlab {method} {path.split('?')[0]} returned {resp.status_code}", status=resp.status_code
            )
        return resp

    @staticmethod
    def _project_ref(project: str | int) -> str:
        return quote(str(project), safe="") if isinstance(project, str) else str(project)

    # -- identity ----------------------------------------------------------

    def get_own_user_id(self) -> int | None:
        """Id of the account this token belongs to (cached); None when unavailable."""
        if self._own_user_id is None:
            try:
                self._own_user_id = int(self._request("GET", "/user").json()["id"])
            except (GitLabError, KeyError, TypeError, ValueError):
                logger.warning("Could not resolve the bot user id for token %s", _mask(self.token))
        return self._own_user_id

    # -- merge request reads -----------------------------------------------

    def get_merge_request(self, project: str | int, mr_iid: int | str) -> dict:
        return self._request("GET", f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}").json()

    def get_mr_changes(self, project: str | int, mr_iid: int | str) -> dict:
        return self._request(
            "GET",
            f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/changes",
            params={"access_raw_diffs": "true"},
        ).json()

    # -- repository reads (context enrichment) --------------------------------

    def get_file_content(self, project: str | int, file_path: str, ref: str) -> str:
        """Raw content of one file at ``ref``; a missing file raises GitLabError."""
        encoded = quote(file_path, safe="")
        return self._request(
            "GET",
            f"/projects/{self._project_ref(project)}/repository/files/{encoded}/raw",
            params={"ref": ref},
        ).text

    def search_blobs(self, project: str | int, query: str, ref: str) -> list | None:
        """Repo-wide blob search at ``ref``; None when advanced search is unavailable.

        Self-managed GitLab returns 400 when advanced search is not enabled
        (403 on license problems) - callers should treat None as "search
        disabled" and continue with what they already have.
        """
        try:
            return self._request(
                "GET",
                f"/projects/{self._project_ref(project)}/search",
                params={"scope": "blobs", "search": query, "ref": ref, "per_page": 20},
            ).json()
        except GitLabError as exc:
            if exc.status in (400, 403, 404, 422):
                return None
            raise

    # -- notes (MR comments) -------------------------------------------------

    def list_notes(self, project: str | int, mr_iid: int | str, max_pages: int = 10) -> list[dict]:
        notes: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._request(
                "GET",
                f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/notes",
                params={"per_page": 100, "page": page, "sort": "desc", "order_by": "created_at"},
            ).json()
            notes.extend(batch)
            if len(batch) < 100:
                break
        return notes

    def create_note(self, project: str | int, mr_iid: int | str, body: str) -> dict:
        return self._request(
            "POST",
            f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        ).json()

    def update_note(self, project: str | int, mr_iid: int | str, note_id: int, body: str) -> None:
        self._request(
            "PUT",
            f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/notes/{note_id}",
            json={"body": body},
        )

    def delete_note(self, project: str | int, mr_iid: int | str, note_id: int) -> None:
        self._request(
            "DELETE", f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/notes/{note_id}"
        )

    # -- discussions (resolvable threads / inline comments) -------------------

    def list_discussions(self, project: str | int, mr_iid: int | str, max_pages: int = 10) -> list[dict]:
        discussions: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._request(
                "GET",
                f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/discussions",
                params={"per_page": 100, "page": page},
            ).json()
            discussions.extend(batch)
            if len(batch) < 100:
                break
        return discussions

    def create_inline_discussion(self, project: str | int, mr_iid: int | str, body: str, position: dict) -> dict:
        return self._request(
            "POST",
            f"/projects/{self._project_ref(project)}/merge_requests/{mr_iid}/discussions",
            json={"body": body, "position": position},
        ).json()
