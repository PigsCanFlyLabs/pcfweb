"""Rename the Liberated Bread group's slug to match its domain.

0012 seeded it as `liberated-bread`; it should be `liberatedbread`, the same
string as liberatedbread.com, because that is what somebody pasting the
signup form onto that site will reach for. 0012 itself now seeds the new
slug, so this is only for databases that already ran the old one.

Renaming a slug is normally the one thing not to do -- an embedded form on
another site carries it in its markup -- but nothing has been deployed with
the old value, so there is no such form to break. Later renames should still
add a new group instead.
"""

from django.db import migrations


OLD_SLUG = "liberated-bread"
NEW_SLUG = "liberatedbread"


def rename(apps, schema_editor, old, new):
    InterestArea = apps.get_model("main", "InterestArea")
    if InterestArea.objects.filter(slug=new).exists():
        # Already renamed, or somebody made the target by hand. Either way
        # renaming onto it would just be a unique-constraint error.
        return
    InterestArea.objects.filter(slug=old).update(slug=new)


def forwards(apps, schema_editor):
    rename(apps, schema_editor, OLD_SLUG, NEW_SLUG)


def backwards(apps, schema_editor):
    rename(apps, schema_editor, NEW_SLUG, OLD_SLUG)


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0013_mailinglistdelivery_email_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
