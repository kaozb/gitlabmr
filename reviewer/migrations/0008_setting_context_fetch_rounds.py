from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the model-driven context fetch budget to dashboard settings."""

    dependencies = [
        ("reviewer", "0007_remove_gitlab_connection_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="setting",
            name="context_fetch_rounds",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
