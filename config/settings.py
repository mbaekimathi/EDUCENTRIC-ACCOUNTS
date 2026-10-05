"""
Django settings for the Educentric ACCOUNTS (finance / bursar) system.

Shares MySQL with ADMINISTRATION and CLIENTS.
- Owns: accounts_* staff + billing tables (managed migrations)
- Reads only: admissions_* and school profile (unmanaged mirrors)

Works on cPanel (Passenger) and later on a VPS (Gunicorn + Nginx).
"""

import os
import sys
import warnings
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env(
    DEBUG=(bool, True),
    LOCAL=(bool, True),
)
environ.Env.read_env(BASE_DIR / ".env")

_LOCAL_DEV_HOSTS = {"localhost", "127.0.0.1", "testserver"}


def _env_key_set(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


def _infer_is_local() -> bool:
    legacy_hosts = env.list("ALLOWED_HOSTS", default=[])
    if legacy_hosts and not set(legacy_hosts) <= _LOCAL_DEV_HOSTS:
        return False
    if _env_key_set("HOSTED_DB_NAME") or _env_key_set("HOSTED_ALLOWED_HOSTS"):
        return False
    return True


_IS_LOCAL = env.bool("LOCAL") if "LOCAL" in os.environ else _infer_is_local()
_ENV_PROFILE = "LOCAL_" if _IS_LOCAL else "HOSTED_"


def _env_str(name: str, default=""):
    prefixed = f"{_ENV_PROFILE}{name}"
    if _env_key_set(prefixed):
        return env(prefixed, default=default)
    if _env_key_set(name):
        return env(name, default=default)
    return default


def _env_bool(name: str, default=False):
    prefixed = f"{_ENV_PROFILE}{name}"
    if prefixed in os.environ:
        return env.bool(prefixed, default=default)
    if name in os.environ:
        return env.bool(name, default=default)
    return default


def _env_list(name: str, *, local_default=None):
    prefixed = f"{_ENV_PROFILE}{name}"
    if _env_key_set(prefixed):
        return env.list(prefixed)
    if _env_key_set(name):
        return env.list(name)
    if _IS_LOCAL and local_default is not None:
        return list(local_default)
    return []


def _merge_unique(*groups):
    seen = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.append(item)
    return seen


def _clean_hosts(values):
    cleaned = []
    for raw in values or []:
        host = str(raw).strip().strip("'\"")
        if host and host not in cleaned:
            cleaned.append(host)
    return cleaned


def _clean_origins(values):
    cleaned = []
    for raw in values or []:
        origin = str(raw).strip().strip("'\"")
        if origin and origin not in cleaned:
            cleaned.append(origin)
    return cleaned


DEBUG = _env_bool("DEBUG", default=_IS_LOCAL)
SECRET_KEY = env("SECRET_KEY", default="")
if isinstance(SECRET_KEY, str):
    SECRET_KEY = SECRET_KEY.strip().strip("'\"")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "unsafe-development-key-change-before-production"
    else:
        raise ImproperlyConfigured("Set SECRET_KEY in .env when DEBUG=False")

ALLOWED_HOSTS = _merge_unique(
    _clean_hosts(
        _env_list(
            "ALLOWED_HOSTS",
            local_default=["localhost", "127.0.0.1", "testserver"],
        )
    ),
    _clean_hosts(env.list("HOSTED_ALLOWED_HOSTS", default=[])),
    _clean_hosts(env.list("LOCAL_ALLOWED_HOSTS", default=[])),
    _clean_hosts(env.list("ALLOWED_HOSTS", default=[])),
)
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "Set HOSTED_ALLOWED_HOSTS (or ALLOWED_HOSTS) in .env when deploying to production."
    )

CSRF_TRUSTED_ORIGINS = _merge_unique(
    _clean_origins(_env_list("CSRF_TRUSTED_ORIGINS", local_default=[])),
    _clean_origins(env.list("HOSTED_CSRF_TRUSTED_ORIGINS", default=[])),
    _clean_origins(env.list("LOCAL_CSRF_TRUSTED_ORIGINS", default=[])),
    _clean_origins(env.list("CSRF_TRUSTED_ORIGINS", default=[])),
)

if DEBUG and not _IS_LOCAL:
    warnings.warn(
        "DEBUG=True with LOCAL=False uses development-only behavior and weak cache/sessions. "
        "Set HOSTED_DEBUG=False on cPanel.",
        RuntimeWarning,
        stacklevel=1,
    )

if DEBUG:
    for host in (
        ".ngrok-free.app",
        ".ngrok-free.dev",
        ".ngrok.io",
        ".trycloudflare.com",
    ):
        if host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(host)

# cPanel / Cloudflare / Nginx terminate SSL in front of the app.
if _env_bool("USE_PROXY_SSL_HEADER", default=not DEBUG):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.staff",
    "apps.directory",
    "apps.billing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.directory.context_processors.school_branding",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

_DB_CONN_MAX_AGE = env.int("DB_CONN_MAX_AGE", default=0)

_db_name = _env_str("DB_NAME", default="").strip()
if _db_name:
    DATABASES = {
        "default": {
            "ENGINE": "config.mysql_backend",
            "NAME": _db_name,
            "USER": _env_str("DB_USER", default="root"),
            "PASSWORD": _env_str("DB_PASSWORD", default=""),
            "HOST": _env_str("DB_HOST", default="127.0.0.1"),
            "PORT": env("DB_PORT", default="3306"),
            "CONN_MAX_AGE": _DB_CONN_MAX_AGE,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "staff.AccountsUser"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 6},
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_MAX_AGE = 60 if DEBUG else 31536000
MEDIA_URL = env("MEDIA_URL", default="/media/")
_default_media = BASE_DIR / "media"
_shared_admin_media = BASE_DIR.parent / "ADMINISTRATION" / "media"
_media_root = _env_str("MEDIA_ROOT", default="")
if _media_root:
    MEDIA_ROOT = Path(_media_root)
elif _shared_admin_media.is_dir():
    MEDIA_ROOT = _shared_admin_media
else:
    MEDIA_ROOT = _default_media

SERVE_MEDIA = _env_bool("SERVE_MEDIA", default=True)

LOGIN_URL = "staff:login"
LOGIN_REDIRECT_URL = "billing:dashboard"
LOGOUT_REDIRECT_URL = "staff:login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

_FILE_CACHE_DIR = BASE_DIR / "tmp" / "django_cache"
if env("REDIS_URL", default=""):
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": env("REDIS_URL"),
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "TIMEOUT": 300,
            "KEY_PREFIX": "edu_accounts",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    SESSION_CACHE_ALIAS = "default"
elif _IS_LOCAL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "edu-accounts-development-cache",
            "TIMEOUT": 300,
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"
else:
    try:
        _FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": str(_FILE_CACHE_DIR),
            "TIMEOUT": 300,
            "OPTIONS": {"MAX_ENTRIES": 20000},
            "KEY_PREFIX": "edu_accounts",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"

SESSION_COOKIE_NAME = "edu_accounts_sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 10
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", default=not DEBUG)
if "runserver" in sys.argv:
    SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "error_file": {
            "class": "logging.FileHandler",
            "filename": str(BASE_DIR / "logs" / "errors.log"),
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["console", "error_file"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
try:
    (BASE_DIR / "logs").mkdir(exist_ok=True)
except OSError:
    LOGGING["loggers"]["django.request"]["handlers"] = ["console"]
