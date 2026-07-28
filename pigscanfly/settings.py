"""
Django settings for pigscanfly project.

Settings are organized as django-configurations classes: Base, Dev, and Prod.
Select one with the DJANGO_CONFIGURATION environment variable (see manage.py).

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.2/ref/settings/
"""
import os

from typing import *

from pathlib import Path

from configurations import Configuration
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def parse_comma_list(raw: str) -> List[str]:
    """Split a comma-separated environment variable into stripped entries.

    Every entry is stripped, not just tested for being non-blank: a value
    written as a conventional "a, b" list, or pulled out of a file into a
    Secret with a trailing newline, otherwise carries whitespace into a
    comparison that has to be exact.
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_int(raw: Optional[str], default: int) -> int:
    """An integer setting from the environment, tolerant of an empty value.

    A Secret created out of a file, or a key set to "", would otherwise raise
    ValueError at settings import -- on every pod, before anything can log it.
    The deploy gate reports that as "Schema is behind (or the database is
    unreachable)" and retries forever, so a blank environment variable presents
    as a database outage. Falling back to the default is the recoverable
    direction.
    """
    try:
        return int((raw or "").strip())
    except (TypeError, ValueError):
        return default


def parse_shipping_rates(raw: str) -> List[str]:
    """Split a comma-separated STRIPE_SHIPPING_RATES value into ids.

    Stripe matches the id exactly, so " shr_two" is not the rate, it is a
    resource_missing that fails every physical checkout.
    """
    return parse_comma_list(raw)


def parse_lowercase_comma_list(raw: str) -> List[str]:
    """Split a comma-separated setting and lowercase each entry.

    Hostnames are case-insensitive, so a mixed-case allowlist entry should not
    silently stop a legitimate redirect from matching. `lower()`, not
    `casefold()`: this is ASCII-style hostname normalisation, not broader
    Unicode equivalence.
    """
    return [item.lower() for item in parse_comma_list(raw)]


def parse_invite_half(raw: str) -> str:
    """Clean one half of the Discord invite (see DISCORD_INVITE_PART_ONE).

    Same hazard as the shipping rates above: a ConfigMap value written as a
    YAML block, or pasted with a trailing newline, arrives with whitespace
    attached. Here that whitespace lands in the middle of a URL, so the halves
    would rejoin into something that is not the invite -- and the page would
    quietly serve the "e-mail us" fallback instead of a link.
    """
    return raw.strip()


def parse_email_flag(raw: str) -> bool:
    """Read one of the EMAIL_USE_* switches from its env string.

    "0", "false", "no", "off" and the empty string are off; anything else is
    on. Unlike STRIPE_AUTOMATIC_TAX's inline parser, empty means OFF: these
    flags pick a wire protocol, and `EMAIL_USE_SSL: ""` in a manifest reads
    as "not set" -- it must not wrap the connection in TLS. Absent stays
    distinct from empty because the default is supplied by the caller
    through os.getenv.
    """
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _email_encryption_flags() -> Tuple[bool, bool]:
    """(EMAIL_USE_TLS, EMAIL_USE_SSL) as the environment currently says.

    One reader for the pair, because it has two callers that must agree: the
    Prod class body, which turns them into settings, and Prod.pre_setup,
    which refuses to boot when both are on (see the guard there). Inlining
    the getenv calls twice would let the defaults drift apart.
    """
    return (parse_email_flag(os.getenv("EMAIL_USE_TLS", "false")),
            parse_email_flag(os.getenv("EMAIL_USE_SSL", "true")))


class Base(Configuration):
    COOKIE_CONSENT_ENABLED = True
    COOKIE_CONSENT_LOG_ENABLED = True
    LOGIN_URL = 'login'
    LOGIN_REDIRECT_URL = '/'

    # SECURITY WARNING: keep the secret key used in production secret!
    # The fallback is for local development only; Prod requires the env var.
    SECRET_KEY = os.getenv(
        "SECRET_KEY",
        'django-insecure-dev-only-do-not-use-in-prod')

    # SECURITY WARNING: don't run with debug turned on in production!
    DEBUG = False

    # Both easy-thumbnails and static-thumbnails read this setting directly.
    # Keep missing source images non-fatal outside development; in particular,
    # static-thumbnails does not provide a default and raises AttributeError
    # while rendering when the setting is absent.
    THUMBNAIL_DEBUG = False

    NEWSLETTER_THUMBNAIL = 'sorl-thumbnail'
    NEWSLETTER_USE_HTTPS = True

    ALLOWED_HOSTS: List[str] = ['localhost', '127.0.0.1']

    # Application definition

    SITE_ID=1

    INSTALLED_APPS = [
        'django.contrib.admin',
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'django.contrib.sessions',
        'django.contrib.messages',
        'django.contrib.staticfiles',
        'django.contrib.sites',
        'main',
        'sorl.thumbnail',
        'easy_thumbnails',
        'newsletter',
        'cookie_consent',
        'django_extensions',
        "static_thumbnails",
    ]

    STATICFILES_FINDERS = (
        "django.contrib.staticfiles.finders.FileSystemFinder",
        "django.contrib.staticfiles.finders.AppDirectoriesFinder",
    )

    MIDDLEWARE = [
        # Must stay first: it answers /healthz before the HTTPS redirect,
        # the ALLOWED_HOSTS check and the cookie-consent database query, each
        # of which would otherwise stop the Kubernetes probes from reflecting
        # whether the app actually works. See main/middleware.py.
        'main.middleware.HealthCheckMiddleware',
        'django.middleware.security.SecurityMiddleware',
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.middleware.common.CommonMiddleware',
        'django.middleware.csrf.CsrfViewMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'django.contrib.messages.middleware.MessageMiddleware',
        'django.middleware.clickjacking.XFrameOptionsMiddleware',
        "cookie_consent.middleware.CleanCookiesMiddleware",
        'django_user_agents.middleware.UserAgentMiddleware',
    ]

    GOOGLE_ANALYTICS = {
        'google_analytics_id': 'G-2EDT623L0V',
    }

    ROOT_URLCONF = 'pigscanfly.urls'

    TEMPLATES = [
        {
            'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [BASE_DIR / "templates"],
            'APP_DIRS': True,
            'OPTIONS': {
                'context_processors': [
                    'django.template.context_processors.debug',
                    'django.template.context_processors.request',
                    'django.contrib.auth.context_processors.auth',
                    'django.contrib.messages.context_processors.messages',
                ],
            },
        },
    ]

    WSGI_APPLICATION = 'pigscanfly.wsgi.application'

    # Database
    # https://docs.djangoproject.com/en/5.2/ref/settings/#databases

    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

    # Password validation
    # https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

    AUTH_PASSWORD_VALIDATORS = [
        {
            'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
        },
        {
            'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        },
        {
            'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
        },
        {
            'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
        },
    ]

    # Internationalization
    # https://docs.djangoproject.com/en/5.2/topics/i18n/

    LANGUAGE_CODE = 'en-us'

    TIME_ZONE = 'UTC'

    USE_I18N = True

    USE_TZ = True

    # Static files (CSS, JavaScript, Images)
    # https://docs.djangoproject.com/en/5.2/howto/static-files/

    STATIC_URL = 'static/'
    STATIC_ROOT = os.path.join(BASE_DIR, 'static')

    # MEDIA FILE SETTINGS

    MEDIA_URL = '/media/'
    MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

    # Default primary key field type
    # https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

    DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

    # GeoIP (country detection for region-specific buy links). The directory
    # holds GeoLite2-Country.mmdb; when it's absent, country detection is
    # disabled and visitors just get the default links.
    GEOIP_PATH = os.getenv("GEOIP_PATH", os.path.join(BASE_DIR, 'geoip'))

    # DIGITAL FULFILMENT
    # Where the book archives live. build.sh copies them out of the sibling
    # pcfweb-book-assets checkout and the Dockerfile lands them here; BASE_DIR
    # is /opt/app in the image, so this resolves to /opt/app/book-assets
    # there and to ./book-assets locally.
    #
    # Deliberately neither STATIC_ROOT nor MEDIA_ROOT: conf/nginx.default
    # aliases /static and /media straight off disk, so anything under those is
    # world-readable, and these are files people paid for. The only route to
    # one is a signed link through DigitalDownloadView.
    BOOK_ASSET_ROOT = os.getenv(
        "BOOK_ASSET_ROOT", os.path.join(BASE_DIR, 'book-assets'))

    # How long an emailed download link keeps working.
    DIGITAL_DOWNLOAD_MAX_AGE = 7 * 24 * 60 * 60

    # Absolute base for those links. They are built from inside the Stripe
    # webhook, where the request's Host header belongs to Stripe's delivery
    # rather than to this site, so there is nothing to derive it from.
    SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://www.pigscanfly.ca")

    # STRIPE SETTINGS
    # Test key for dev; Prod overrides with STRIPE_LIVE_SECRET_KEY.
    STRIPE_API_KEY = os.getenv("STRIPE_TEST_SECRET_KEY", "")
    STRIPE_AUTOMATIC_TAX = os.getenv("STRIPE_AUTOMATIC_TAX", "true").lower() not in {
        "0", "false", "no", "off"}

    # Signing secret for the /stripe/webhook endpoint, from the Stripe
    # Dashboard (Developers -> Webhooks -> the endpoint -> signing secret).
    # Left empty the webhook rejects every delivery with a 400, which is the
    # safe direction to fail: no unverified payload is ever processed.
    # Prod refuses to boot without it -- see the property below.
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    # Seconds. Applies to every Stripe call except the webhook's line-item
    # lookup, which sets its own tighter budget (see Payments).
    #
    # The SDK's default is ~80s, which is longer than gunicorn's own worker
    # timeout: a hung Stripe connection would get the worker killed rather
    # than returning an error the view could handle. Keep this comfortably
    # under GUNICORN_TIMEOUT (see scripts/start-server.sh).
    STRIPE_TIMEOUT = int(os.getenv("STRIPE_TIMEOUT", "15"))

    # Stripe Checkout shipping rates offered for physical goods, most
    # permissive first.
    #
    # These ids are LIVEMODE-SCOPED: a shr_... created in test mode does not
    # exist under a live key and vice versa, and Stripe rejects the whole
    # session with resource_missing rather than skipping the bad rate. They
    # are therefore overridable per environment instead of hardcoded at the
    # call site. Empty means "no shipping options", which is a valid session.
    STRIPE_SHIPPING_RATES: List[str] = parse_shipping_rates(
        os.getenv("STRIPE_SHIPPING_RATES", ",".join([
            "shr_0MJrPInkDnSOC1s7tidX8eMN",  # YOLO
            "shr_0MJrIYnkDnSOC1s7fthNSlhb",  # sf only
            "shr_0MJrL4nkDnSOC1s7cPSy15CO",  # media mail
            "shr_0MNOZrnkDnSOC1s7TSLZig6Z",  # faster
        ])))

    # DISCORD INVITE
    # The invite link for /discord, held as two halves that are only ever
    # joined in the visitor's browser (see main/templates/discord.html). Discord
    # invites are unauthenticated URLs: anything that reads one can join, so the
    # halves are what goes in the manifest and what goes on the wire, and the
    # page only emits them at all once a captcha has been answered.
    #
    # Rotating the invite is a ConfigMap edit (see deploy.yaml), not a rebuild.
    # Split them wherever you like -- the view only cares that the two halves
    # concatenate to a https://discord.gg/<code> URL, and renders the
    # "e-mail us to join" page instead when they don't.
    DISCORD_INVITE_PART_ONE = parse_invite_half(
        os.getenv("DISCORD_INVITE_PART_ONE", "https://discord.gg/aVU2"))
    DISCORD_INVITE_PART_TWO = parse_invite_half(
        os.getenv("DISCORD_INVITE_PART_TWO", "2HAmb"))

    # Where to write when the invite is broken, expired, or the halves are
    # misconfigured -- the fallback path off /discord.
    DISCORD_SUPPORT_EMAIL = os.getenv(
        "DISCORD_SUPPORT_EMAIL", "support@pigscanfly.ca")

    # MAILING LIST SETTINGS

    # Where confirmations and mailings come from. Falls back to
    # DEFAULT_FROM_EMAIL; set it when list mail should come from a different
    # address than order mail (a separate list@ address is easier to filter
    # on and easier for a mail host to rate-limit separately).
    MAILING_LIST_FROM_EMAIL = os.getenv("MAILING_LIST_FROM_EMAIL", "")

    # Unsubscribe links built without a request to derive a host from use
    # SITE_BASE_URL, the same setting the emailed download links use -- one
    # absolute base for the site, not one per feature.

    # Confirmation emails one address's source may trigger per hour. The
    # signup endpoint is CSRF exempt and open to the internet, so without a
    # ceiling it is a way to have us mail whoever somebody points it at. Kept
    # generous because a school or an office is one address to us. 0 disables.
    MAILING_LIST_SIGNUP_RATE_LIMIT = parse_int(
        os.getenv("MAILING_LIST_SIGNUP_RATE_LIMIT"), 20)

    # Absolute hosts the embeddable signup form may redirect back to through
    # its `next` field. Matched case-insensitively, but still as exact netlocs,
    # so a host with a port must be listed with that port.
    MAILING_LIST_ALLOWED_NEXT_HOSTS: List[str] = parse_lowercase_comma_list(
        os.getenv("MAILING_LIST_ALLOWED_NEXT_HOSTS", ""))

    # How many freshly imported addresses the import page will email the
    # "we've updated our list" notice to. Above this it imports and says to
    # write a mailing instead: the notice loop runs inside one request, and a
    # big import would outlive the worker timeout half-sent, whereas the send
    # page batches and is resumable.
    MAILING_LIST_IMPORT_NOTICE_MAX = parse_int(
        os.getenv("MAILING_LIST_IMPORT_NOTICE_MAX"), 500)

    # Recipients per click of "send" in the admin, and per SMTP connection.
    # The whole batch has to fit inside GUNICORN_TIMEOUT, so this trades
    # clicks against the risk of a half-finished request -- which is survivable
    # either way, because each recipient's delivery row is written as its mail
    # goes out and a resumed send skips it.
    MAILING_LIST_SEND_BATCH_SIZE = parse_int(
        os.getenv("MAILING_LIST_SEND_BATCH_SIZE"), 100)

    # Who gets told about a paid order so they can ship it. Env-driven so the
    # owner's address is not baked into the repo.
    ORDER_NOTIFICATION_EMAIL = os.getenv(
        "ORDER_NOTIFICATION_EMAIL", "support@pigscanfly.ca")
    ADMINS = [("Pigs Can Fly Labs Orders", ORDER_NOTIFICATION_EMAIL)]


class Dev(Base):
    DEBUG = True
    THUMBNAIL_DEBUG = True

    ALLOWED_HOSTS: List[str] = ['*']

    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = os.path.join(BASE_DIR, "sent_emails")


# The environment variables Prod refuses to boot without, and the one-line
# reason each is fatal (the properties below carry the long version). Held as
# data so Prod.pre_setup can name every missing one in a single failure.
# Lower-cased deliberately: an upper-case module-level name would be copied
# into the settings themselves.
_prod_required_env = {
    "SECRET_KEY": "signs sessions and password-reset tokens",
    "STRIPE_LIVE_SECRET_KEY": "every checkout call to Stripe",
    "STRIPE_WEBHOOK_SECRET": "without it no order is ever marked paid",
}


class Prod(Base):

    @classmethod
    def pre_setup(cls):
        """Fail on a missing environment variable while the message survives.

        The per-setting guards below cannot be relied on to report anything.
        Django's ManagementUtility touches settings.INSTALLED_APPS inside a
        try/except, stores whatever ImproperlyConfigured comes back, and --
        seeing settings.configured is still False -- skips django.setup() and
        runs the command anyway. The command's system checks then walk into an
        empty app registry, so `manage.py migrate` in a fresh pod dies with
        "AppRegistryNotReady: Models aren't loaded yet." and never a word
        about the variable that is actually missing.

        pre_setup runs before any of that: django-configurations calls it
        while importing the settings module, so raising here reaches the
        console intact. Checking the whole set at once matters too -- the
        properties are evaluated in alphabetical order, so one guard at a time
        would mean one failed rollout per missing variable.

        The same early exit hosts the cross-variable email check: a value
        that is only wrong in combination (EMAIL_USE_TLS with EMAIL_USE_SSL)
        has no single property to guard it.
        """
        super().pre_setup()
        missing = [name for name in _prod_required_env if not os.getenv(name)]
        if missing:
            raise ImproperlyConfigured(
                "Prod is missing required environment variable(s) "
                + ", ".join(missing)
                + ". In the cluster these come from the pcfweb-secret Secret "
                "(see deploy.yaml). Details:\n"
                + "\n".join(f"  {name}: {_prod_required_env[name]}"
                            for name in missing))

        use_tls, use_ssl = _email_encryption_flags()
        if use_tls and use_ssl:
            # Django's SMTP backend refuses the pair too, but only when a
            # connection is opened -- at send time, inside the Stripe
            # webhook, where every caller catches the failure and records it
            # on the order row. That is a silently mail-less site. Failing
            # the rollout here keeps the previous pods serving instead.
            raise ImproperlyConfigured(
                "EMAIL_USE_TLS and EMAIL_USE_SSL are both on, and they pick "
                "the wire protocol for the same connection: STARTTLS on a "
                "plaintext port (587/25) versus TLS from the first byte "
                "(465). Turn one of them off in the pcfweb-db-config "
                "ConfigMap (see deploy.yaml).")

    ALLOWED_HOSTS: List[str] = [
        'www.pigscanfly.ca',
        'pigscanfly.ca',
        # Kube probes hit the pod directly.
        'localhost',
        '127.0.0.1',
    ]

    # TLS terminates at the ingress; trust its forwarded proto header so
    # CSRF origin checking sees https.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Ramp HSTS gradually: 3600 -> 86400 -> 31536000 after HTTPS is stable.
    # Only add includeSubDomains/preload after verifying every current and
    # planned pigscanfly.ca subdomain serves HTTPS; preload is effectively
    # irreversible.
    SECURE_HSTS_SECONDS = 3600
    CSRF_TRUSTED_ORIGINS = [
        "https://www.pigscanfly.ca",
        "https://pigscanfly.ca",
    ]

    @property
    def SECRET_KEY(self):
        key = os.getenv("SECRET_KEY")
        if not key:
            raise ImproperlyConfigured(
                "The SECRET_KEY environment variable must be set in Prod.")
        return key

    @property
    def STRIPE_API_KEY(self):
        key = os.getenv("STRIPE_LIVE_SECRET_KEY")
        if not key:
            # Without this the key is silently None, stripe.api_key is set to
            # None at import, and the failure surfaces as a 500 on the first
            # add-to-cart -- i.e. on a customer, in production. Refusing to
            # boot turns that into a failed rollout instead, which Kubernetes
            # handles by keeping the previous pods serving.
            raise ImproperlyConfigured(
                "The STRIPE_LIVE_SECRET_KEY environment variable must be set "
                "in Prod.")
        return key

    @property
    def STRIPE_WEBHOOK_SECRET(self):
        secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        if not secret:
            # The webhook is the only thing that marks an order PAID. With no
            # secret it rejects every delivery, so customers would be charged
            # by Stripe while the order sat PENDING and the owner was never
            # told to ship it -- money taken, nothing sent, no error anywhere
            # the owner would see. That is worse than not booting.
            raise ImproperlyConfigured(
                "The STRIPE_WEBHOOK_SECRET environment variable must be set "
                "in Prod; without it no order is ever marked paid.")
        return secret

    @property
    def DATABASES(self):
        # In-cluster Postgres (CloudNativePG); DBHOST points at the
        # operator-created pcfweb-pg-rw Service (see deploy.yaml).
        return {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": os.getenv("DBNAME"),
                "USER": os.getenv("DBUSER"),
                "PASSWORD": os.getenv("DBPASSWORD"),
                "HOST": os.getenv("DBHOST"),
                "ATOMIC_REQUESTS": False,
                # psycopg3 native connection pooling; incompatible with
                # CONN_MAX_AGE so leave that unset. Workers are single-request
                # gunicorn sync workers, so keep the per-process pool tiny --
                # the default min_size of 4 would park ~48 idle connections
                # across the fleet on a 100-connection Postgres.
                #
                # connect_timeout bounds a single connection attempt and pool
                # timeout bounds the wait for one. Without them an unreachable
                # database turns every request into a hang that outlives the
                # gunicorn worker timeout, so the pod dies by worker-kill
                # instead of returning a 500 the health check can see.
                "OPTIONS": {
                    "connect_timeout": 5,
                    "pool": {"min_size": 1, "max_size": 2, "timeout": 10},
                },
            }
        }

    # OUTBOUND MAIL
    # Order notifications, download links, the book-asset audit and Django's
    # 500 reports to ADMINS all leave through this relay. Everything here is
    # env-driven, with defaults mirroring the pcfweb-db-config ConfigMap in
    # deploy.yaml (a drift test ties the two together) -- so changing the
    # relay is a ConfigMap edit plus a pod restart, not a rebuild. Only
    # EMAIL_HOST_PASSWORD is secret; it rides pcfweb-secret, provisioned out
    # of the colo-scripts vault. The whole path, including what the domain's
    # SPF record authorizes, is written down in docs/email.md.
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    # The domain's own mail server, by its MX name. NOT the bare apex: since
    # the site moved behind Cloudflare, pigscanfly.ca resolves to Cloudflare
    # edge IPs, and Cloudflare does not proxy SMTP -- a connection there just
    # burns the ten-second timeout below on every send. mail.pigscanfly.ca
    # is the machine (71.19.157.174) the apex used to point at.
    EMAIL_HOST = os.getenv("EMAIL_HOST", "mail.pigscanfly.ca")
    # The submissions port (RFC 8314): TLS from the first byte, and the
    # meaning the port carries -- an authenticated client handing mail in,
    # which is what this app is. Not 25, the MTA-to-MTA relay port, where
    # outbound traffic is widely blocked or throttled and servers routinely
    # refuse AUTH. If the mail server turns out not to listen on 465, the
    # flip is port 587 with EMAIL_USE_TLS on and EMAIL_USE_SSL off
    # (STARTTLS submission) -- in the ConfigMap, not here.
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "465"))
    # STARTTLS on a plaintext port versus TLS from the first byte (SMTPS,
    # port 465). At most one may be on; pre_setup fails the rollout on the
    # pair rather than letting Django's backend raise at send time -- which
    # would be inside the Stripe webhook, where every caller catches the
    # failure and files it on the order row instead of anywhere a deploy
    # would notice.
    EMAIL_USE_TLS, EMAIL_USE_SSL = _email_encryption_flags()
    # Django's SMTP backend has no timeout by default, so a mail host that
    # drops packets (as distinct from refusing the connection) blocks
    # send_mail for the OS TCP timeout -- minutes. Every send_mail call here
    # runs somewhere that cannot afford that: inside the Stripe webhook,
    # where a hang past GUNICORN_TIMEOUT gets the worker killed mid-request
    # (the same reasoning as the database connect_timeout above and
    # STRIPE_TIMEOUT); in Django's error logger, which mails ADMINS
    # synchronously while handling a 500; and during primary startup
    # (check_book_assets), where it would eat into build.sh's 300s rollout
    # budget. Mail that cannot be sent in ten seconds is mail that is not
    # getting sent; every caller already catches the failure and records it.
    EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "support")
    # Empty disables SMTP AUTH altogether -- Django only authenticates when
    # both user and password are non-empty -- which is the right degradation
    # for a relay that trusts the cluster's source address instead of a
    # login.
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    DEFAULT_FROM_EMAIL = os.getenv(
        "DEFAULT_FROM_EMAIL", "support@pigscanfly.ca")
    # The sender on Django's 500 reports to ADMINS. Its framework default is
    # root@localhost, which any modern relay rejects or spam-folders -- so
    # left unset, the mail about failures is the mail most likely to fail.
    # Ride the same address everything else sends as.
    SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
