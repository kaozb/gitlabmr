"""LLM call + structured output contract.

Reliability chain borrowed from PR-Agent:
- model fallback list; parse/validation failures are treated as "this model
  failed" and trigger the next model, not a task failure
- finish_reason == "length" (truncated output) is an explicit failure
- YAML extraction handles ``` fences and repairs two high-frequency breakages
  before pydantic validates the final shape
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger("gitlabmr.llm")


class LLMError(Exception):
    """Raised for call errors, truncated output, or unparseable reviews."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason  # stable bucket, e.g. http_error / timeout / truncated / bad_yaml / bad_schema


class KeyIssue(BaseModel):
    relevant_file: str = Field(min_length=1)
    issue_header: str = Field(default="Issue", min_length=1)
    issue_content: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    kind: str = ""  # verified | suggestion | "" (older models may omit it)
    evidence: str = ""

    @field_validator("relevant_file")
    @classmethod
    def _strip_path(cls, value: str) -> str:
        return value.strip().lstrip("/")

    @field_validator("end_line")
    @classmethod
    def _end_ge_start(cls, value: int, info):
        start = info.data.get("start_line", value)
        if value < start:
            raise ValueError("end_line must be >= start_line")
        return value

    @field_validator("kind", mode="before")
    @classmethod
    def _norm_kind(cls, value):
        if not isinstance(value, str):
            return ""
        lowered = value.strip().lower()
        return lowered if lowered in ("verified", "suggestion") else ""

    @field_validator("evidence", mode="before")
    @classmethod
    def _norm_evidence(cls, value):
        if not isinstance(value, str):
            return ""
        return value.strip()[:500]


class ReviewOutput(BaseModel):
    overall_assessment: str = ""
    score: int | None = Field(default=None, ge=0, le=100)
    risk_level: str | None = None
    key_issues_to_review: list[KeyIssue] = Field(default_factory=list)
    relevant_tests: str | None = None
    security_concerns: str | None = None

    @field_validator("risk_level")
    @classmethod
    def _risk_literal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        return lowered if lowered in ("low", "medium", "high") else None

    @field_validator("relevant_tests", mode="before")
    @classmethod
    def _tests_bool(cls, value):
        if isinstance(value, bool):  # YAML 1.1 parses Yes/No as booleans
            return "Yes" if value else "No"
        return value

    @field_validator("security_concerns", mode="before")
    @classmethod
    def _security_bool(cls, value):
        if isinstance(value, bool):
            return "No" if not value else "Yes"
        return value

    @field_validator("key_issues_to_review")
    @classmethod
    def _cap_issues(cls, value: list[KeyIssue], info):
        cap = info.context.get("num_max_findings") if isinstance(info.context, dict) else None
        if isinstance(cap, int) and cap > 0 and len(value) > cap:
            return value[:cap]
        return value


_FENCE_RE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)


class ContextRequestItem(BaseModel):
    """One entry of a model-issued ``need_context`` request."""

    type: str = Field(default="file")  # file | search
    path: str = ""
    symbol: str = ""
    reason: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, value):
        lowered = str(value or "").strip().lower()
        return lowered if lowered in ("file", "search") else "file"

    @field_validator("path", "symbol", "reason", mode="before")
    @classmethod
    def _clean(cls, value):
        return str(value or "").strip()[:300] if value is not None else ""


class ContextRequest(BaseModel):
    """The model asked for more repository content instead of reviewing."""

    items: list[ContextRequestItem] = Field(default_factory=list)


_BROKEN_VALUE_RE = re.compile(r"^(\s*-?\s*[\w.\[\]]+):(?!\S)(\s*)(.*:.*\S)?\s*$")

# Absence claims about repository-wide state ("X is unused", "no caller was
# updated", "the definition does not exist") are the classic diff-blind-spot
# false positives. Deliberately narrow: bare "缺少/missing" also appears in
# diff-content suggestions ("缺少错误处理") that must NOT be filtered.
_ABSENCE_RE = re.compile(
    r"未使用|未被调用|未被引用|无人调用|未被任何|无引用|未定义|不存在|未注册|"
    r"unused|never used|not used|no caller|not called|not referenced|not defined"
    r"|undefined|dead code|unregistered",
    re.IGNORECASE,
)
_PATCH_ONLY = ("", "patch-only", "patch only")


def filter_unverified_absence_issues(review: ReviewOutput) -> tuple[ReviewOutput, list[KeyIssue]]:
    """Post-filter backstop: drop absence claims that cite no evidence.

    The prompt already forbids these; this is the code-side guarantee. An
    absence claim survives only when ``kind == verified`` AND its evidence
    points beyond the patch itself (a fetched file or a search result).
    Returns (filtered review, dropped issues).
    """
    kept: list[KeyIssue] = []
    dropped: list[KeyIssue] = []
    for issue in review.key_issues_to_review:
        claims_absence = bool(_ABSENCE_RE.search(f"{issue.issue_header} {issue.issue_content}"))
        has_evidence = issue.kind == "verified" and issue.evidence.strip().lower() not in _PATCH_ONLY
        if claims_absence and not has_evidence:
            dropped.append(issue)
        else:
            kept.append(issue)
    if not dropped:
        return review, []
    data = review.model_dump()
    data["key_issues_to_review"] = [i.model_dump() for i in kept]
    return ReviewOutput.model_validate(data), dropped


def extract_yaml_block(text: str) -> str:
    fence = _FENCE_RE.search(text)
    return fence.group(1).strip() if fence else text.strip()


def _repair_lines(raw: str) -> str:
    """Repair the two YAML breakages models emit most:
    unquoted scalar values containing ': ' and stray tabs in indentation."""
    fixed = []
    for line in raw.replace("\t", "  ").splitlines():
        match = _BROKEN_VALUE_RE.match(line)
        if match:
            key, gap, value = match.groups()
            if value:
                line = f"{key}:{gap}\"{value.strip()}\""
        fixed.append(line)
    return "\n".join(fixed)


def parse_review(text: str, num_max_findings: int = 6) -> ReviewOutput:
    data = _load_yaml_object(text)
    review = data.get("review", data)  # accept both nested and flat forms
    if not isinstance(review, dict):
        raise LLMError("bad_yaml", "missing 'review' mapping")
    try:
        return ReviewOutput.model_validate(review, context={"num_max_findings": num_max_findings})
    except ValidationError as exc:
        raise LLMError("bad_schema", exc.errors()[0].get("msg", "validation failed")) from exc


def _load_yaml_object(text: str) -> dict:
    raw = extract_yaml_block(text)
    data = None
    for attempt in (raw, _repair_lines(raw)):
        try:
            data = yaml.safe_load(attempt)
            break
        except yaml.YAMLError:
            continue
    if not isinstance(data, dict):
        raise LLMError("bad_yaml", "response is not a YAML mapping")
    return data


def parse_response(
    text: str, num_max_findings: int = 6, require_review: bool = False
) -> ReviewOutput | ContextRequest:
    """Dispatch a model reply to a review or a context request.

    A reply carrying a top-level ``need_context`` list is the model asking
    for more repository content before it can review. ``require_review=True``
    rejects such replies with a parse error - used once the fetch budget is
    exhausted, so the refusal falls into the normal model-fallback chain.
    """
    data = _load_yaml_object(text)
    if "need_context" in data:
        if require_review:
            raise LLMError("bad_schema", "context requested after budget exhausted")
        items = data.get("need_context")
        if not isinstance(items, list) or not items:
            raise LLMError("bad_schema", "need_context must be a non-empty list")
        try:
            return ContextRequest.model_validate({"items": items})
        except ValidationError as exc:
            raise LLMError("bad_schema", exc.errors()[0].get("msg", "invalid need_context")) from exc
    review = data.get("review", data)  # accept both nested and flat forms
    if not isinstance(review, dict):
        raise LLMError("bad_yaml", "missing 'review' mapping")
    try:
        return ReviewOutput.model_validate(review, context={"num_max_findings": num_max_findings})
    except ValidationError as exc:
        raise LLMError("bad_schema", exc.errors()[0].get("msg", "validation failed")) from exc


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    fallback_models: tuple[str, ...] = ()
    timeout: int = 300
    max_output_tokens: int = 4000
    temperature: float = 0.2


def chat(cfg: LLMConfig, system: str, user: str) -> tuple[str, str]:
    """One chat completion. Returns (text, finish_reason); raises LLMError."""
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_output_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=cfg.timeout,
        )
    except requests.RequestException as exc:
        raise LLMError("timeout" if isinstance(exc, requests.Timeout) else "http_error", type(exc).__name__) from exc
    if resp.status_code != 200:
        raise LLMError("http_error", f"status {resp.status_code}")
    try:
        body = resp.json()
        text = body["choices"][0]["message"]["content"] or ""
        finish_reason = body["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError("bad_response", "malformed chat completion payload") from exc
    if finish_reason == "length":
        raise LLMError("truncated", "output cut by max_tokens")
    return text, finish_reason


def review_with_fallback(
    cfg: LLMConfig, system: str, user: str, num_max_findings: int = 6, require_review: bool = False
) -> tuple[ReviewOutput | ContextRequest, str]:
    """Try each model until one returns a valid structured reply.

    The reply may be a finished review or a ``need_context`` request (see
    ``parse_response``); a parse/transport error moves on to the next model.
    """
    errors: list[str] = []
    for model in (cfg.model, *cfg.fallback_models):
        attempt = LLMConfig(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=model,
            timeout=cfg.timeout,
            max_output_tokens=cfg.max_output_tokens,
            temperature=cfg.temperature,
        )
        try:
            text, _ = chat(attempt, system, user)
            return parse_response(text, num_max_findings, require_review=require_review), model
        except LLMError as exc:
            errors.append(f"{model}: {exc}")
            logger.warning("review attempt failed for model %s: %s", model, exc)
    raise LLMError("all_models_failed", "; ".join(errors))
