"""
Django settings for pigscanfly project.

Settings are organized as django-configurations classes: Base, Dev, and Prod.
Select one with the DJANGO_CONFIGURATION environment variable (see manage.py).

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.2/ref/settings/
"""
import logging
import os

from typing import *

from pathlib import Path

from configurations import Configuration
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


class Base(Configuration):
    COOKIE_CONSENT_ENABLED = True
    COOKIE_CONSENT_LOG_ENABLED = True
    LOGIN_URL = 'login'
    LOGIN_REDIRECT_URL = '/'
    THUMBNAIL_DEBUG = True

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

    GOOGLE_CLIENT_SECRETS_FILE = os.getenv(
        "GOOGLE_CLIENT_SECRETS_FILE",
        "client_secret.json")

    if not os.path.exists(GOOGLE_CLIENT_SECRETS_FILE):
        GOOGLE_CLIENT_SECRETS_FILE = "../cal-sync-magic/client_secret.json"

    if not os.path.exists(GOOGLE_CLIENT_SECRETS_FILE):
        GOOGLE_CLIENT_SECRETS_FILE = "client_secret/client_secret.json"

    # If we don't have a secret file but we have the text make it.
    if not os.path.exists(GOOGLE_CLIENT_SECRETS_FILE):
        secret = os.getenv("GOOGLE_CLIENT_SECRETS_TEXT")
        if secret is not None:
            secrets_dir = os.path.dirname(GOOGLE_CLIENT_SECRETS_FILE)
            if secrets_dir:
                os.makedirs(secrets_dir, exist_ok=True)
            # Owner-only permissions; this holds an OAuth client secret.
            fd = os.open(GOOGLE_CLIENT_SECRETS_FILE,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secret)
        else:
            # Calendar sync features will fail at use time without this file,
            # but everything else (manage.py, tests, the store) should work.
            logger.warning(
                "No Google client secret file or GOOGLE_CLIENT_SECRETS_TEXT "
                "env var found; calendar sync will be unavailable.")

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

    # cal-sync-magic lives in a private repo; the Docker image installs it
    # from a sibling checkout, but CI and fresh local clones may not have
    # it. The calendar app (and its urls) simply turn off when absent.
    try:
        import cal_sync_magic  # noqa: F401
        INSTALLED_APPS.append("cal_sync_magic")
    except ImportError:
        logger.warning(
            "cal-sync-magic is not installed; calendar sync is disabled.")

    STATICFILES_FINDERS = (
        "django.contrib.staticfiles.finders.FileSystemFinder",
        "django.contrib.staticfiles.finders.AppDirectoriesFinder",
    )

    MIDDLEWARE = [
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


class Dev(Base):
    DEBUG = True

    ALLOWED_HOSTS: List[str] = ['*']

    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = os.path.join(BASE_DIR, "sent_emails")

    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


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
        return os.getenv("STRIPE_LIVE_SECRET_KEY")

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
                # CONN_MAX_AGE so leave that unset.
                "OPTIONS": {"pool": True},
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
