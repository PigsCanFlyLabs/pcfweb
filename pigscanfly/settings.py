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


def parse_shipping_rates(raw: str) -> List[str]:
    """Split a comma-separated STRIPE_SHIPPING_RATES value into ids.

    Each id is stripped, not just tested for being non-blank. Stripe matches
    the id exactly, so " shr_two" from a conventional "a, b" list -- or
    "shr_two\\n" from a Secret created out of a file -- is not the rate, it is
    a resource_missing that fails every physical checkout.
    """
    return [rate.strip() for rate in raw.split(",") if rate.strip()]


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


class Prod(Base):

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

    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = os.getenv("EMAIL_HOST", "pigscanfly.ca")
    EMAIL_USE_TLS = True
    EMAIL_PORT = 25
    EMAIL_USE_SSL = False
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "support")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    DEFAULT_FROM_EMAIL = "support@pigscanfly.ca"
