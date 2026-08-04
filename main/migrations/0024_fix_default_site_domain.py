"""Point the sites-framework row at this site instead of example.com.

django-newsletter builds every activation and unsubscribe link off
``Site.objects.get_current()``, i.e. off the ``django.contrib.sites`` row
``SITE_ID`` names. On a database that predates migration 0014 -- production --
that row is the ``example.com`` default contrib.sites seeds from its
post_migrate signal, so every "confirm your subscription" email carried an
https://example.com link that led nowhere near us. 0014 only creates the row
when the table has none, which serves fresh databases and leaves the damaged
one exactly as it is; this migration repairs it in place.

Guarded so it only ever touches the untouched default: a domain somebody has
already corrected by hand in the admin is their call, not this migration's to
overwrite.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.db import migrations

# What contrib.sites' create_default_site seeds -- for both fields -- and
# therefore the only value this migration may overwrite.
DEFAULT_DOMAIN = "example.com"

SITE_NAME = "Pigs Can Fly Labs"


def our_domain():
    """The domain activation links should carry, from SITE_BASE_URL.

    The same derivation migration 0014 uses when it creates the row on a
    fresh database, so the two cannot disagree about what it ought to be.
    """
    return urlparse(
        getattr(settings, "SITE_BASE_URL",
                "https://www.pigscanfly.ca")).netloc


def point_site_at_our_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    domain = our_domain()
    if not domain or domain == DEFAULT_DOMAIN:
        # A malformed SITE_BASE_URL must not blank the domain: a link that
        # says example.com at least looks as broken as it is.
        return
    site = Site.objects.filter(
        pk=settings.SITE_ID, domain=DEFAULT_DOMAIN).first()
    if site is None:
        # Nothing to repair: 0014 already created the row correctly, or a
        # human already fixed it.
        return
    site.domain = domain
    if site.name == DEFAULT_DOMAIN:
        # The name renders anywhere a template says {{ site.name }}. It is
        # seeded alongside the domain, so fix it with the domain -- but only
        # while it is still the matching default.
        site.name = SITE_NAME
    site.save(update_fields=["domain", "name"])


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0023_purchasefeedback"),
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        # Reverse is a no-op on purpose: rolling code back does not make
        # example.com the right place to send subscribers.
        migrations.RunPython(point_site_at_our_domain,
                             migrations.RunPython.noop),
    ]
