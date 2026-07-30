"""The "follow us" links, as data rather than as markup.

One list, read from settings, so the checkout success page and the buyer's
receipt advertise the same accounts. A network we do not have an account on
is simply unset -- there is no placeholder URL to be found and clicked, and
nothing here invents a handle from the site's name.

Every URL is checked before it renders: an https URL to a real host or
nothing at all. That is not paranoia about our own ConfigMap so much as the
same rule /discord already follows -- a half-edited or typo'd value must
drop the link rather than hand `javascript:...` to a visitor's browser.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from django.conf import settings


@dataclass(frozen=True)
class SocialLink:
    """One account worth following."""

    label: str
    url: str
    # A Font Awesome 4 class, which is the icon set the vendored theme ships
    # (see main/static/assets/css/font-awesome.css). It has no Discord or
    # Mastodon glyph, hence the generic ones below: a missing icon renders as
    # a blank square, which looks like a broken page rather than a link.
    icon: str


# (settings name, label, icon), in the order they render. Adding a network is
# a line here plus a default in settings.Base; the setting being empty is the
# normal state for most of them.
NETWORKS: List[Tuple[str, str, str]] = [
    ("SOCIAL_MASTODON_URL", "Mastodon", "fa-globe"),
    ("SOCIAL_BLUESKY_URL", "Bluesky", "fa-cloud"),
    ("SOCIAL_YOUTUBE_URL", "YouTube", "fa-youtube-play"),
    ("SOCIAL_TWITCH_URL", "Twitch", "fa-twitch"),
    ("SOCIAL_INSTAGRAM_URL", "Instagram", "fa-instagram"),
    ("SOCIAL_LINKEDIN_URL", "LinkedIn", "fa-linkedin"),
]


def usable_url(raw: str) -> Optional[str]:
    """The URL if it is one we are willing to put in a page, else None.

    https only, and a hostname is required: a scheme-relative value or a
    bare handle typed into the environment variable is a configuration
    mistake, and the recoverable direction is one missing link rather than a
    link that goes somewhere unintended.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    # A netloc with whitespace or a control character in it never came from a
    # real URL, and urlparse is happy to hand one back.
    if any(ord(char) < 33 or ord(char) == 127 for char in candidate):
        return None
    return candidate


def social_links() -> List[SocialLink]:
    """The accounts to advertise, skipping the ones that are not configured."""
    links = []
    for setting_name, label, icon in NETWORKS:
        url = usable_url(getattr(settings, setting_name, ""))
        if url is not None:
            links.append(SocialLink(label=label, url=url, icon=icon))
    return links
