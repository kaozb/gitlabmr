from django.contrib import admin

from .models import IssueFeedback, RepoTokenEntry, ReviewRecord, Setting, WebhookEvent


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "object_kind", "project_path", "action", "head_sha", "handled", "created_at")
    list_filter = ("object_kind", "handled")
    search_fields = ("project_path",)
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]


@admin.register(ReviewRecord)
class ReviewRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id", "project_path", "mr_iid", "head_sha", "status", "model_used",
        "issues_count", "score", "risk_level", "duration_ms", "updated_at",
    )
    list_filter = ("status", "risk_level", "source")
    search_fields = ("project_path", "mr_url")
    readonly_fields = [f.name for f in ReviewRecord._meta.fields]


@admin.register(IssueFeedback)
class IssueFeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "project_path", "mr_iid", "relevant_file", "start_line", "issue_header",
                    "marked_by", "created_at")
    list_filter = ("project_path",)
    search_fields = ("project_path", "relevant_file", "issue_header")
    readonly_fields = [f.name for f in IssueFeedback._meta.fields]


@admin.register(RepoTokenEntry)
class RepoTokenEntryAdmin(admin.ModelAdmin):
    list_display = ("product", "api_url", "token", "enabled", "note", "updated_at")
    search_fields = ("product", "api_url", "note")


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ("id", "model", "llm_base_url", "updated_at")
    readonly_fields = [f.name for f in Setting._meta.fields]

    def has_add_permission(self, request):
        return not Setting.objects.exists()
