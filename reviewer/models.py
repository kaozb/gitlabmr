"""Persistence: webhook audit + review records (also the idempotency gate)."""

from __future__ import annotations

from django.db import models

STATUS_CHOICES = [
    ("running", "running"),
    ("success", "success"),
    ("failed", "failed"),
    ("skipped", "skipped"),
]


class WebhookEvent(models.Model):
    """One received webhook payload (filtered summary only, never the raw body).

    Per product decision only the FIRST ``open`` action of each MR is stored;
    later events (reopen/update/note...) run through the pipeline unrecorded.
    """

    object_kind = models.CharField(max_length=32, db_index=True)
    project_path = models.CharField(max_length=512, blank=True, default="")
    mr_iid = models.IntegerField(null=True, blank=True, db_index=True)
    action = models.CharField(max_length=32, blank=True, default="")
    head_sha = models.CharField(max_length=64, blank=True, default="")
    handled = models.CharField(max_length=32, default="received")  # received|review|ignored|rejected
    note = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.object_kind}:{self.project_path}:{self.action}:{self.handled}"


class ReviewRecord(models.Model):
    """One review attempt; unique per (project, MR, head sha) for idempotency."""

    project_path = models.CharField(max_length=512, db_index=True)
    mr_iid = models.IntegerField(db_index=True)
    mr_url = models.URLField(max_length=1024, blank=True, default="")
    head_sha = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="running", db_index=True)
    source = models.CharField(max_length=16, default="webhook")  # webhook|note
    model_used = models.CharField(max_length=128, blank=True, default="")
    issues_count = models.IntegerField(default=0)
    score = models.IntegerField(null=True, blank=True)
    risk_level = models.CharField(max_length=16, blank=True, default="")
    reviewed_files = models.JSONField(default=list, blank=True)
    not_reviewed_files = models.JSONField(default=list, blank=True)
    diff_chars = models.IntegerField(default=0)
    prompt_chars = models.IntegerField(default=0)
    completion_chars = models.IntegerField(default=0)
    error_reason = models.CharField(max_length=64, blank=True, default="")
    error_detail = models.TextField(blank=True, default="")
    steps = models.JSONField(default=list, blank=True)  # pipeline flowchart: [{step,label,status,detail,ms}]
    duration_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project_path", "mr_iid", "head_sha"],
                name="uniq_review_per_head_sha",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project_path}!{self.mr_iid}@{self.head_sha[:8]}:{self.status}"


class Setting(models.Model):
    """Global service overrides edited from the dashboard.

    Blank string / NULL means "inherit from environment defaults"; any value
    stored here wins over env vars (single row, accessed via objects.first()).
    """

    # GitLab connection
    webhook_secret = models.CharField(max_length=256, blank=True, default="")
    default_api_url = models.CharField(max_length=256, blank=True, default="")
    default_token = models.CharField(max_length=256, blank=True, default="")
    # AI model
    llm_base_url = models.CharField(max_length=256, blank=True, default="")
    llm_api_key = models.CharField(max_length=256, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    fallback_models = models.CharField(max_length=256, blank=True, default="")
    llm_max_output_tokens = models.IntegerField(null=True, blank=True)
    llm_timeout = models.IntegerField(null=True, blank=True)
    # Review behavior
    num_max_findings = models.IntegerField(null=True, blank=True)
    max_diff_tokens = models.IntegerField(null=True, blank=True)
    per_file_patch_chars = models.IntegerField(null=True, blank=True)
    max_comment_chars = models.IntegerField(null=True, blank=True)
    inline_comments = models.BooleanField(null=True, blank=True)
    inline_max_comments = models.IntegerField(null=True, blank=True)
    review_on_push = models.BooleanField(null=True, blank=True)
    ignore_branches = models.CharField(max_length=256, blank=True, default="")
    language = models.CharField(max_length=16, blank=True, default="")
    workers = models.IntegerField(null=True, blank=True)
    gitlab_timeout = models.IntegerField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"service settings (updated {self.updated_at:%Y-%m-%d %H:%M})"


class RepoTokenEntry(models.Model):
    """Per-prefix GitLab credential managed from the dashboard.

    When at least one enabled entry exists, these replace the tokens loaded
    from env/file config entirely; otherwise env/file tokens are used.
    """

    prefix = models.CharField(max_length=256, unique=True)
    api_url = models.CharField(max_length=256, blank=True, default="")
    token = models.CharField(max_length=256)
    webhook_secret = models.CharField(max_length=256, blank=True, default="")
    enabled = models.BooleanField(default=True)
    note = models.CharField(max_length=256, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["prefix"]

    def __str__(self) -> str:
        return f"{self.prefix} -> {_mask_token(self.token)}"


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


def try_register_review(
    *,
    project_path: str,
    mr_iid: int,
    head_sha: str,
    mr_url: str = "",
    source: str = "webhook",
    force: bool = False,
) -> ReviewRecord | None:
    """Register an intended review; None means "do not review" (in-flight or done).

    A previously failed attempt for the same sha is reset and retried.
    ``force=True`` re-runs even a succeeded/in-flight record (manual re-review).
    """
    from django.db import transaction

    with transaction.atomic():
        record, created = ReviewRecord.objects.get_or_create(
            project_path=project_path,
            mr_iid=mr_iid,
            head_sha=head_sha,
            defaults={
                "status": "running",
                "source": source,
                "mr_url": mr_url,
            },
        )
        if created:
            return record
        if not force and record.status in ("running", "success"):
            return None
        record.status = "running"
        record.error_reason = ""
        record.error_detail = ""
        record.steps = []
        record.source = source
        if mr_url:
            record.mr_url = mr_url
        record.save(
            update_fields=["status", "error_reason", "error_detail", "steps", "source", "mr_url", "updated_at"]
        )
        return record
