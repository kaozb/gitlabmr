"""Drop the GitLab connection settings from the dashboard.

The dashboard no longer carries the global webhook secret, default GitLab
address or default token: they are env-only knobs now (see .env.example), and
repo tokens configured on the dashboard already carry their own match URLs.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("reviewer", "0006_repotokenentry_product_match_url"),
    ]

    operations = [
        migrations.RemoveField(model_name="repotokenentry", name="webhook_secret"),
        migrations.RemoveField(model_name="setting", name="default_api_url"),
        migrations.RemoveField(model_name="setting", name="default_token"),
        migrations.RemoveField(model_name="setting", name="webhook_secret"),
    ]
