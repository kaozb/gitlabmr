"""RepoTokenEntry: prefix -> free-form product label, api_url becomes the match key.

Old scheme: ``prefix`` was the match key (a path prefix such as ``tp/cneos``)
and ``api_url`` only carried the GitLab instance origin. New scheme: matching
is done on full GitLab URL prefixes, so ``api_url`` becomes the unique match
key (``https://host/tp/cneos``) and ``prefix`` is renamed to a free-form
``product`` label that never participates in matching.

Data migration: each entry's new api_url becomes
``<that entry's own instance origin>/<old prefix>`` (the real instance lives in
the entry's old api_url, not in Setting.default_api_url, which is often a
placeholder); ``product`` is left empty for the admin to fill in. If several
old entries collapse onto the same match URL (their old prefixes differed only
in casing or trailing slashes), the one with the LONGEST old prefix survives -
preserving the old longest-prefix-wins matching semantics.
"""

from django.db import migrations, models


def normalize_match_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    without_scheme = raw.split("://", 1)[1] if "://" in raw else raw
    scheme, sep, _ = raw.partition("://")
    head, slash, tail = without_scheme.partition("/")
    head = head.strip("/").lower()
    if not head:
        return ""
    path = tail.rstrip("/").strip()
    return f"{scheme}{sep}{head}" + (f"/{path}" if (slash or path) else "")


def forwards(apps, schema_editor):
    RepoTokenEntry = apps.get_model("reviewer", "RepoTokenEntry")

    def origin_of(value: str) -> str:
        from urllib.parse import urlparse

        raw = (value or "").strip()
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if not parsed.netloc:
            return ""
        return f"{parsed.scheme or 'https'}://{parsed.netloc}"

    # match_url -> (best pk, longest old prefix length)
    keep: dict[str, tuple[int, int]] = {}
    for entry in RepoTokenEntry.objects.all().order_by("pk"):
        prefix = (entry.product or "").strip().strip("/").lower()
        # The real instance address lives in each entry's own (old) api_url -
        # NOT in Setting.default_api_url, which is often just a placeholder
        # like https://gitlab.com and would silently rewrite every match URL.
        origin = origin_of(entry.api_url)
        if not origin:
            continue
        match_url = normalize_match_url(f"{origin}/{prefix}")
        best = keep.get(match_url)
        if best is None or len(prefix) > best[1]:
            keep[match_url] = (entry.pk, len(prefix))

    url_by_pk = {pk: url for url, (pk, _) in keep.items()}
    # order_by("pk") overrides the historical Meta.ordering (["prefix"]), which
    # no longer exists until the AlterModelOptions below has been applied.
    for entry in RepoTokenEntry.objects.all().order_by("pk"):
        if entry.pk not in url_by_pk:
            continue
        entry.product = ""
        entry.api_url = url_by_pk[entry.pk]
        entry.save(update_fields=["product", "api_url", "updated_at"])
    RepoTokenEntry.objects.order_by("pk").exclude(pk__in=url_by_pk.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("reviewer", "0005_reviewrecord_non_inline_issues"),
    ]

    operations = [
        migrations.RenameField(model_name="repotokenentry", old_name="prefix", new_name="product"),
        # Drop the UNIQUE constraint that RenameField carried over from prefix
        # BEFORE the data step: entries collapse onto "" first, uniqueness moves
        # to api_url afterwards.
        migrations.AlterField(
            model_name="repotokenentry",
            name="product",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="repotokenentry",
            name="api_url",
            field=models.CharField(max_length=256, unique=True),
        ),
        migrations.AlterModelOptions(
            name="repotokenentry",
            options={"ordering": ["api_url"]},
        ),
    ]
