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

logger = logging.getLogger("gitlabmr.gitlab")


class GitLabError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _mask(token: str) -> str:
    return (token[:8] + "...") if len(token) > 8 else "***"


class GitLabClient:
    def __init__(self, api_url: str, token: str, timeout: int = 15):
        self.api_url = (api_url or "https://gitlab.com").rstrip("/")
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
