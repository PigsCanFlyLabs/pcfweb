"""Who to follow, as data rather than as markup.

There are three names on the receipt for what is really one and a half
companies, and "follow us" is ambiguous across them: Holden writes the books,
Pigs Can Fly Labs publishes them and runs the Discord, and Liberated Bread is
the same company as Pigs Can Fly Labs under its own name and its own site.
Somebody who just bought a book may want any one of those and not the other
two, so each gets its own row rather than everything being poured into one
list of icons.

Every account is read from settings -- one variable per follower target per
network -- so the checkout success page and the buyer's receipt advertise the
same accounts. A network we do not have an account on is simply unset: there
is no placeholder URL to be found and clicked, and nothing here invents a
handle from a name. A target with nothing configured at all does not render
as an empty box; it is dropped.

Every URL is checked before it renders: an https URL to a real host or
nothing at all. That is not paranoia about our own ConfigMap so much as the
same rule /discord already follows -- a half-edited or typo'd value must
drop the link rather than hand `javascript:...` to a visitor's browser.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from django.conf import settings


# Liberated Bread's site. It lives here rather than next to the homepage view
# because the checkout page needs it too, and views.py already imports this
# module -- one constant, so the homepage card, the family page and the
# "follow along" block cannot drift into pointing at different places.
# Note this currently serves a domain-parking page; the owner knows.
LIBERATED_BREAD_URL = "https://www.liberatedbread.com/"


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


@dataclass(frozen=True)
class FollowTarget:
    """One of us, and everywhere that one of us can be followed."""

    key: str
    name: str
    blurb: str
    # Its own site, where it has one that is not this one. None for Pigs Can
    # Fly Labs: a link from this page back to this page is not a follow.
    site: Optional[str] = None
    # Whether the Discord door belongs on this row. One server, run by the
    # company, so exactly one target carries it.
    discord: bool = False
    links: List[SocialLink] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Nothing configured, so nothing to render but a heading."""
        return not self.links and not self.site and not self.discord


@dataclass(frozen=True)
class TargetSpec:
    """The definition of a follow target: who, and which settings say where."""

    key: str
    name: str
    blurb: str
    # Settings prefix. The account on network X is `<prefix><X>_URL`, and this
    # target's own site is `<prefix>SITE_URL`.
    prefix: str
    site: Optional[str] = None
    discord: bool = False


# (settings suffix, label, icon), in the order they render within a target.
# Adding a network is a line here plus one default per target in
# settings.Base; the setting being empty is the normal state for most of them.
NETWORKS: List[Tuple[str, str, str]] = [
    ("MASTODON", "Mastodon", "fa-globe"),
    ("BLUESKY", "Bluesky", "fa-cloud"),
    ("YOUTUBE", "YouTube", "fa-youtube-play"),
    ("TWITCH", "Twitch", "fa-twitch"),
    ("INSTAGRAM", "Instagram", "fa-instagram"),
    ("LINKEDIN", "LinkedIn", "fa-linkedin"),
]


# In the order they render. Holden first: somebody who just bought a book
# bought something Holden wrote, and the publisher is the more abstract of
# the two. Liberated Bread last because it is the one a book buyer is least
# likely to have come for.
TARGETS: List[TargetSpec] = [
    TargetSpec(
        key="holden",
        name="Holden",
        blurb=("Writes the books, and shows some of the writing while it "
               "is still being done."),
        prefix="SOCIAL_HOLDEN_",
    ),
    TargetSpec(
        key="pigs-can-fly-labs",
        name="Pigs Can Fly Labs",
        blurb=("New books, new editions, and the Discord where they get "
               "argued about first."),
        prefix="SOCIAL_PCFL_",
        discord=True,
    ),
    TargetSpec(
        key="liberated-bread",
        name="Liberated Bread",
        blurb=("The same company as Pigs Can Fly Labs, with its own site "
               "and its own list. Coming soon."),
        prefix="SOCIAL_BREAD_",
        site=LIBERATED_BREAD_URL,
    ),
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


def target_links(spec: TargetSpec) -> List[SocialLink]:
    """The accounts configured for one target, skipping the unset ones."""
    links = []
    for suffix, label, icon in NETWORKS:
        url = usable_url(getattr(settings, f"{spec.prefix}{suffix}_URL", ""))
        if url is not None:
            links.append(SocialLink(label=label, url=url, icon=icon))
    return links


def follow_targets() -> List[FollowTarget]:
    """Everyone worth following, skipping anyone with nowhere to be followed.

    The empty case is real and is not an error: a target whose accounts are
    all unset renders as nothing rather than as a name with a blank space
    under it.
    """
    targets = []
    for spec in TARGETS:
        # A settings override wins over the built-in site so the URL can be
        # corrected without a rebuild, exactly like the accounts. It goes
        # through usable_url too: a typo'd override drops back to nothing,
        # not to a link into the unknown.
        site = usable_url(getattr(settings, f"{spec.prefix}SITE_URL", ""))
        if site is None and spec.site is not None:
            site = usable_url(spec.site)
        target = FollowTarget(
            key=spec.key,
            name=spec.name,
            blurb=spec.blurb,
            site=site,
            discord=spec.discord,
            links=target_links(spec),
        )
        if not target.is_empty:
            targets.append(target)
    return targets
