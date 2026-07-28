import functools
import logging
import secrets

from typing import List, Optional

from django.conf import settings
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


def admin_recipients() -> List[str]:
    """Who to tell when something the owner has to fix goes wrong.

    Shared by the order/digital-delivery notifications on Order and by the
    startup asset audit (main/management/commands/check_book_assets.py), so
    the two cannot end up mailing different people.
    """
    recipients = []
    for entry in getattr(settings, "ADMINS", None) or []:
        # Django 5.2 requires 2-tuples, but be forgiving about the bare
        # string form so a mis-set env var is not a crash in a webhook.
        recipients.append(entry if isinstance(entry, str) else entry[1])
    return [r for r in recipients if r]


def generate_username(email: str):
    username = email.split('@')[0]
    while User.objects.filter(username=username).exists():
        username += secrets.token_hex(8)
    return username


def get_client_ip(request) -> Optional[str]:
    # The in-pod nginx appends to X-Forwarded-For (see conf/nginx.default),
    # so the first entry is the client as seen by the ingress.
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@functools.lru_cache(maxsize=1)
def _get_geoip():
    try:
        from django.contrib.gis.geoip2 import GeoIP2

        return GeoIP2()
    except Exception as e:
        logger.warning(
            f"GeoIP database unavailable; country detection disabled: {e}")
        return None


def get_country_code(request) -> Optional[str]:
    """Resolve the visitor's ISO country code via MaxMind GeoLite2.

    Returns None whenever the lookup can't happen (no GeoLite2 database
    bundled, private/unknown address, ...) so callers fall back to the
    default links.
    """
    ip = get_client_ip(request)
    geoip = _get_geoip()
    if not ip or geoip is None:
        return None
    try:
        return geoip.country_code(ip)
    except Exception as e:
        logger.debug(f"No GeoIP country for {ip}: {e}")
        return None
