from django.urls import path
from django.views.generic import RedirectView

from . import ui, views

app_name = "reviewer"

urlpatterns = [
    path("", ui.dashboard, name="dashboard"),
    path("settings/llm/", ui.settings_llm_view, name="settings-llm"),
    path("settings/repos/", ui.settings_repos_view, name="settings-repos"),
    path("settings/", RedirectView.as_view(pattern_name="reviewer:settings-llm", permanent=False), name="settings"),
    path("login/", ui.ui_login, name="login"),
    path("logout/", ui.ui_logout, name="logout"),
    path("reviews/<int:pk>/", ui.review_detail, name="review-detail"),
    path("reviews/<int:pk>/retry/", ui.retry_review, name="review-retry"),
    path("review/trigger/", ui.trigger_review, name="review-trigger"),
    path("webhook/gitlab/", views.gitlab_webhook, name="gitlab-webhook"),
]
