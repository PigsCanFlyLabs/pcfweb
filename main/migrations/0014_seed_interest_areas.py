"""The lists people can pick from when they subscribe.

An interest area is a django-newsletter Newsletter, so these are seeded there
rather than in a model of our own. Seeded rather than left to be typed in by
hand so a fresh database and production agree on the slugs -- an embedded
signup form on another site carries one in its markup, so the two disagreeing
means that form quietly posts to the general list.

Only ever adds. A list that has been renamed, described differently or hidden
in the admin is left exactly as it is: this seeds the initial set, it does not
enforce it.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.db import migrations


# (slug, title). The slug is the string an embedded form carries, so it
# matches the site it belongs to where there is one.
NEWSLETTERS = [
    ("all", "All"),
    ("general", "General updates"),
    ("books", "Books"),
    ("dc4k", "Distributed Computing 4 Kids and Executives"),
    ("high-performance-spark", "High Performance Spark"),
    ("liberatedbread", "Liberated Bread"),
    ("fight-health-insurance", "Fight Health Insurance"),
]

SENDER = "Pigs Can Fly Labs"


def current_sites(apps):
    """The sites to attach a new list to.

    django.contrib.sites creates its example.com row from a post_migrate
    signal, which has not run yet while this migration is executing -- so on a
    fresh database there is nothing here to attach and the lists would come
    out invisible to django-newsletter's own views. Creating the row instead
    of waiting for it also means it gets our domain rather than example.com,
    and contrib.sites then skips its default.
    """
    Site = apps.get_model("sites", "Site")
    sites = list(Site.objects.all())
    if sites:
        return sites
    domain = urlparse(
        getattr(settings, "SITE_BASE_URL",
                "https://www.pigscanfly.ca")).netloc or "example.com"
    site, _created = Site.objects.get_or_create(
        pk=settings.SITE_ID,
        defaults={"domain": domain, "name": "Pigs Can Fly Labs"})
    return [site]


def seed(apps, schema_editor):
    Newsletter = apps.get_model("newsletter", "Newsletter")
    sites = current_sites(apps)
    for slug, title in NEWSLETTERS:
        newsletter, created = Newsletter.objects.get_or_create(
            slug=slug,
            defaults={"title": title,
                      "email": settings.DEFAULT_FROM_EMAIL,
                      "sender": SENDER,
                      "visible": True})
        if created and sites:
            # django-newsletter's own views filter on site, so a list with
            # none attached is one nobody can confirm or unsubscribe from.
            newsletter.site.set(sites)


def unseed(apps, schema_editor):
    """Remove the ones nobody subscribed to.

    A list with subscribers stays: the record of what somebody opted into is
    not ours to drop on the way back down a migration.
    """
    Newsletter = apps.get_model("newsletter", "Newsletter")
    for slug, _title in NEWSLETTERS:
        newsletter = Newsletter.objects.filter(slug=slug).first()
        if newsletter is not None and not newsletter.subscription_set.exists():
            newsletter.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0013_mailinglistmessage_mailinglistdelivery"),
        ("newsletter", "0001_initial"),
        ("sites", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
