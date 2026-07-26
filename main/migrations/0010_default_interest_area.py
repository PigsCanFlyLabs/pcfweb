"""Create the general interest area.

Every signup that does not name a group lands here, so a database without it
would have the signup endpoint creating it on the first request instead --
which works (see InterestArea.get_default) but leaves the group nameless and
undescribed until somebody notices.
"""

from django.db import migrations


def create_general_area(apps, schema_editor):
    InterestArea = apps.get_model("main", "InterestArea")
    InterestArea.objects.get_or_create(
        slug="general",
        defaults={"name": "General updates",
                  "description": "News from Pigs Can Fly Labs."})


def remove_general_area(apps, schema_editor):
    """Only if nobody is in it.

    PROTECT on the subscription's foreign key would raise here anyway; being
    explicit means the reverse migration says why rather than surfacing an
    integrity error from somewhere inside the ORM.
    """
    InterestArea = apps.get_model("main", "InterestArea")
    area = InterestArea.objects.filter(slug="general").first()
    if area is not None and not area.subscriptions.exists():
        area.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0009_interestarea_mailinglistmessage_and_more"),
    ]

    operations = [
        migrations.RunPython(create_general_area, remove_general_area),
    ]
