import functools
import ipaddress
import logging
import secrets

from typing import Optional

from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


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


def get_storable_client_ip(request) -> Optional[str]:
    """The client IP, but only when it is actually an IP address.

    X-Forwarded-For is client-supplied text; nginx appends to whatever
    arrived. Handing that straight to a GenericIPAddressField turns a forged
    header into a database error on a public endpoint, so anything that is not
    a literal address is recorded as unknown instead.
    """
    ip = get_client_ip(request)
    if not ip:
        return None
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        logger.debug("Ignoring an unusable client address %r.", ip)
        return None
    return ip
