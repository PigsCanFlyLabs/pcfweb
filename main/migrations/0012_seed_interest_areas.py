"""The groups people can pick from when they subscribe.

Seeded rather than left to be typed in by hand, so a fresh database and
production agree on the slugs -- an embedded signup form on another site
carries the slug in its markup, so the two disagreeing means that form
silently posts into the general group.

Only ever adds. An area that has been renamed, described differently or
deactivated in the admin is left exactly as it is: this migration seeds the
initial list, it does not enforce it.
"""

from django.db import migrations


AREAS = [
    # (slug, name, description, sort_order, catch_all)
    ("all", "All",
     "Everything below, in one subscription.", 0, True),
    ("general", "General updates",
     "News from Pigs Can Fly Labs.", 10, False),
    ("books", "Books",
     "New books, updates and discounts.", 20, False),
    ("dc4k", "Distributed Computing 4 Kids and Executives",
     "News about the book.", 30, False),
    ("high-performance-spark", "High Performance Spark",
     "News about the book.", 40, False),
    ("liberatedbread", "Liberated Bread",
     "The bread project — same company, its own site.", 50, False),
    ("fight-health-insurance", "Fight Health Insurance",
     "The separate project that helps people appeal insurance denials.",
     60, False),
]


def seed(apps, schema_editor):
    InterestArea = apps.get_model("main", "InterestArea")
    for slug, name, description, sort_order, catch_all in AREAS:
        InterestArea.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "description": description,
                      "sort_order": sort_order, "catch_all": catch_all})
    # 0010 created the general group before there was an order to put it in.
    InterestArea.objects.filter(slug="general", sort_order=100).update(
        sort_order=10)


def unseed(apps, schema_editor):
    """Remove the ones nobody subscribed to.

    A group with subscribers stays: PROTECT on the subscription's foreign key
    would refuse anyway, and the record of what somebody opted into is not
    ours to drop on the way back down a migration.
    """
    InterestArea = apps.get_model("main", "InterestArea")
    for slug, *_ in AREAS:
        if slug == "general":
            # 0010's row, and 0010 is what removes it.
            continue
        area = InterestArea.objects.filter(slug=slug).first()
        if area is not None and not area.subscriptions.exists():
            area.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0011_alter_interestarea_options_interestarea_catch_all_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
