from django.db import migrations, models


class Migration(migrations.Migration):
    """ReviewRecord gains ``non_inline_issues``: key issues kept out of the
    inline discussions, so the detail page can list them as a panel."""

    dependencies = [
        ("reviewer", "0004_setting_inline_max_comments"),
    ]

    operations = [
        migrations.AddField(
            model_name="reviewrecord",
            name="non_inline_issues",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
