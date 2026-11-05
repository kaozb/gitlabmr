from django.apps import AppConfig


class ReviewerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "reviewer"
    verbose_name = "GitLab MR AI Reviewer"
