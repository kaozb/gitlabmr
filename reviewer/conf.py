"""Environment-driven configuration for the GitLab MR review service.

Config sources, in priority order:
1. ``GITLABMR_REPO_CONFIG``  - path to a YAML/JSON file describing per-prefix repo tokens.
2. ``GITLABMR_REPO_TOKENS``  - the same structure inlined as JSON in an env var.
Plus plain env vars for the AI endpoint, webhook secret and tuning knobs (see README).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

logger = logging.getLogger("gitlabmr.conf")

# Dashboard-saved settings are re-read at most this often per process, so UI
# edits propagate without restarts while keeping config reads cheap.
CONF_TTL_SECONDS = 10


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in _env(name).split(",") if item.strip()]


@dataclass(frozen=True)
class RepoToken:
    """Credentials for one repo prefix (longest prefix wins)."""

    prefix: str
    api_url: str
    token: str
    webhook_secret: str | None = None


@dataclass(frozen=True)
class Conf:
    webhook_secret: str
    default_api_url: str
    default_token: str
    repo_tokens: tuple[RepoToken, ...] = field(default=())

    llm_base_url: str = ""
    llm_api_key: str = ""
    model: str = "deepseek-chat"
    fallback_models: tuple[str, ...] = ()

    max_diff_tokens: int = 24000
    num_max_findings: int = 6
    llm_max_output_tokens: int = 4000
    inline_comments: bool = False
    review_on_push: bool = True
    ignore_branches: tuple[str, ...] = ()
    bot_indicators: tuple[str, ...] = ("bot",)
    language: str = "zh-CN"
    max_comment_chars: int = 60000
    workers: int = 2
    gitlab_timeout: int = 15
    llm_timeout: int = 300
    per_file_patch_chars: int = 60000


def _parse_repo_entries(entries: list) -> tuple[RepoToken, ...]:
    tokens = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        prefix = str(entry.get("prefix", "")).strip()
        token = str(entry.get("token", "")).strip()
        if not prefix or not token:
            continue
        tokens.append(
            RepoToken(
                prefix=prefix.rstrip("/").lower(),
                api_url=str(entry.get("api_url", "")).strip().rstrip("/"),
                token=token,
                webhook_secret=(str(entry["webhook_secret"]).strip() if entry.get("webhook_secret") else None),
            )
        )
    # Longest prefix first so resolution is a simple linear scan.
    return tuple(sorted(tokens, key=lambda t: len(t.prefix), reverse=True))


def _load_repo_tokens() -> tuple[RepoToken, ...]:
    file_path = _env("GITLABMR_REPO_CONFIG")
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        return _parse_repo_entries(list(data.get("repos") or []))
    inline = _env("GITLABMR_REPO_TOKENS")
    if inline:
        data = json.loads(inline)
        return _parse_repo_entries(list(data if isinstance(data, list) else data.get("repos") or []))
    return ()


def _load_env_conf() -> Conf:
    return Conf(
        webhook_secret=_env("GITLABMR_WEBHOOK_SECRET"),
        default_api_url=_env("GITLABMR_GITLAB_URL", "https://gitlab.com").rstrip("/"),
        default_token=_env("GITLABMR_GITLAB_TOKEN"),
        repo_tokens=_load_repo_tokens(),
        llm_base_url=_env("GITLABMR_LLM_BASE_URL").rstrip("/"),
        llm_api_key=_env("GITLABMR_LLM_API_KEY"),
        model=_env("GITLABMR_MODEL", "deepseek-chat"),
        fallback_models=tuple(_env_list("GITLABMR_FALLBACK_MODELS")),
        max_diff_tokens=_env_int("GITLABMR_MAX_DIFF_TOKENS", 24000),
        num_max_findings=_env_int("GITLABMR_NUM_MAX_FINDINGS", 6),
        llm_max_output_tokens=_env_int("GITLABMR_LLM_MAX_OUTPUT_TOKENS", 4000),
        inline_comments=_env_bool("GITLABMR_INLINE_COMMENTS", False),
        review_on_push=_env_bool("GITLABMR_REVIEW_ON_PUSH", True),
        ignore_branches=tuple(_env_list("GITLABMR_IGNORE_BRANCHES")),
        bot_indicators=tuple(_env_list("GITLABMR_BOT_INDICATORS")) or ("bot",),
        language=_env("GITLABMR_LANGUAGE", "zh-CN"),
        max_comment_chars=_env_int("GITLABMR_MAX_COMMENT_CHARS", 60000),
        workers=_env_int("GITLABMR_WORKERS", 2),
        gitlab_timeout=_env_int("GITLABMR_GITLAB_TIMEOUT", 15),
        llm_timeout=_env_int("GITLABMR_LLM_TIMEOUT", 300),
        per_file_patch_chars=_env_int("GITLABMR_PER_FILE_PATCH_CHARS", 60000),
    )


_TEXT_OVERRIDES = (
    "webhook_secret",
    "default_api_url",
    "default_token",
    "llm_base_url",
    "llm_api_key",
    "model",
    "fallback_models",
    "ignore_branches",
    "language",
)
_INT_OVERRIDES = (
    "llm_max_output_tokens",
    "llm_timeout",
    "num_max_findings",
    "max_diff_tokens",
    "per_file_patch_chars",
    "max_comment_chars",
    "workers",
    "gitlab_timeout",
)
_BOOL_OVERRIDES = ("inline_comments", "review_on_push")


def _apply_db_overrides(conf: Conf) -> Conf:
    """Dashboard-saved settings win over env defaults (blank/NULL = inherit).

    Any enabled RepoTokenEntry replaces the env/file token table entirely;
    with no DB entries, env/file tokens stay in charge. A missing table
    (first migrate, fresh DB) silently falls back to env-only config.
    """
    try:
        from .models import RepoTokenEntry, Setting

        setting = Setting.objects.first()
        token_entries = list(RepoTokenEntry.objects.filter(enabled=True))
    except Exception as exc:  # noqa: BLE001 - DB simply not ready yet
        logger.debug("db config unavailable, using env only: %s", type(exc).__name__)
        return conf

    updates: dict = {}
    if setting is not None:
        for name in _TEXT_OVERRIDES:
            value = getattr(setting, name)
            if value:
                updates[name] = value
        for name in _INT_OVERRIDES:
            value = getattr(setting, name)
            if value is not None:
                updates[name] = value
        for name in _BOOL_OVERRIDES:
            value = getattr(setting, name)
            if value is not None:
                updates[name] = value

    if token_entries:
        updates["repo_tokens"] = tuple(
            sorted(
                (
                    RepoToken(
                        prefix=entry.prefix.strip("/").lower(),
                        api_url=(entry.api_url or conf.default_api_url).rstrip("/"),
                        token=entry.token,
                        webhook_secret=entry.webhook_secret or None,
                    )
                    for entry in token_entries
                ),
                key=lambda t: len(t.prefix),
                reverse=True,
            )
        )
    if not updates:
        return conf
    return replace(conf, **updates)


def load_conf() -> Conf:
    return _apply_db_overrides(_load_env_conf())


_CONF: Conf | None = None
_CONF_LOADED_AT = 0.0


def get_conf() -> Conf:
    """Process-wide conf with a short TTL; dashboard edits propagate without restart."""
    global _CONF, _CONF_LOADED_AT
    now = time.monotonic()
    if _CONF is None or now - _CONF_LOADED_AT >= CONF_TTL_SECONDS:
        _CONF = load_conf()
        _CONF_LOADED_AT = now
    return _CONF


def reload_conf() -> Conf:
    """Force-refresh the cached conf (used after UI saves and in tests)."""
    global _CONF, _CONF_LOADED_AT
    _CONF = load_conf()
    _CONF_LOADED_AT = time.monotonic()
    return _CONF
