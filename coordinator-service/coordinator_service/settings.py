from pathlib import Path

import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
# Determines the absolute folder path and stores in inside the variable

DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")
IS_TESTING = 'test' in sys.argv

# Pattern B: Safe fallback in Development/Testing, Fail-Fast in Production
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or IS_TESTING:
        SECRET_KEY = "django-insecure-#)8x**j4@!pjsa4qr%1##8k=4$6wa-p1a@+o^h6(18b_tx&k1d"
    else:
        raise RuntimeError(
            "CRITICAL SECURITY ERROR: SECRET_KEY environment variable is not set! "
            "A secure, unique SECRET_KEY must be provided in production (DEBUG=False)."
        )
elif not (DEBUG or IS_TESTING) and SECRET_KEY.startswith("django-insecure"):
    raise RuntimeError(
        "CRITICAL SECURITY ERROR: Insecure default SECRET_KEY detected in production (DEBUG=False)! "
        "You must configure a strong, random SECRET_KEY for production."
    )

# Force sync if running tests OR if FORCE_SYNC env var is set to true
TESTING_SYNC_MODE = IS_TESTING or os.getenv("FORCE_SYNC", "false").lower() in ("true", "1", "t")

raw_allowed_hosts = os.getenv("ALLOWED_HOSTS")
if raw_allowed_hosts:
    ALLOWED_HOSTS = [host.strip() for host in raw_allowed_hosts.split(",") if host.strip()]
elif DEBUG or IS_TESTING:
    ALLOWED_HOSTS = ["*"]
else:
    raise RuntimeError(
        "CRITICAL SECURITY ERROR: ALLOWED_HOSTS environment variable is not set in production (DEBUG=False)!"
    )

INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY", "dtc-internal-coordinator-secret-key-2026")

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    "rest_framework",
    "coordinator"
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'coordinator.middleware.CorrelationIdMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True

_cors_env = os.getenv("CORS_ALLOWED_ORIGINS")
if _cors_env:
    CORS_ALLOWED_ORIGINS = [origin.strip() for origin in _cors_env.split(",") if origin.strip()]
else:
    CORS_ALLOWED_ORIGINS = [
        "null",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8001",
        "http://127.0.0.1:8001",
        "http://localhost:8002",
        "http://127.0.0.1:8002",
    ]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^http://localhost(:\d+)?$",
    r"^http://127\.0\.0\.1(:\d+)?$",
    r"^https://.*\.railway\.app$",
    r"^https://.*\.up\.railway\.app$",
    r"^https://.*\.vercel\.app$",
    r"^https://.*\.netlify\.app$",
    r"^https://.*\.pages\.dev$",
    r"^https://.*\.github\.io$",
]
CORS_EXPOSE_HEADERS = [
    "x-total-count",
    "x-overall-total",
    "x-page-limit",
    "x-page-offset",
    "x-correlation-id",
    "x-actor-id",
    "x-user-id",
    "x-user-email",
    "content-type",
]
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "x-correlation-id",
    "x-actor-id",
    "x-user-id",
    "x-user-email",
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        "https://*.railway.app,https://*.up.railway.app,https://*.vercel.app,https://*.netlify.app,https://*.pages.dev,https://*.github.io,http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if origin.strip()
]
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

ROOT_URLCONF = 'coordinator_service.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'coordinator_service.wsgi.application'

from urllib.parse import urlparse

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")

if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    db_name = url.path.lstrip("/") or os.getenv("DB_NAME", "coordinator_db")
    db_password = url.password or os.getenv("DB_PASSWORD", os.getenv("PGPASSWORD"))
    if not db_password:
        if DEBUG or IS_TESTING:
            db_password = "postgres"
        else:
            raise RuntimeError(
                "CRITICAL SECURITY ERROR: Database password is not configured in production (DEBUG=False)!"
            )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": db_name,
            "USER": url.username or os.getenv("DB_USER", os.getenv("PGUSER", "postgres")),
            "PASSWORD": db_password,
            "HOST": url.hostname or os.getenv("DB_HOST", os.getenv("PGHOST", "localhost")),
            "PORT": str(url.port or os.getenv("DB_PORT", os.getenv("PGPORT", "5432"))),
        }
    }
else:
    db_password = os.getenv("DB_PASSWORD", os.getenv("PGPASSWORD"))
    if not db_password:
        if DEBUG or IS_TESTING:
            db_password = "postgres"
        else:
            raise RuntimeError(
                "CRITICAL SECURITY ERROR: DB_PASSWORD / PGPASSWORD environment variable is not set in production (DEBUG=False)!"
            )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", os.getenv("PGDATABASE", "coordinator_db")),
            "USER": os.getenv("DB_USER", os.getenv("PGUSER", "postgres")),
            "PASSWORD": db_password,
            "HOST": os.getenv("DB_HOST", os.getenv("PGHOST", "localhost")),
            "PORT": str(os.getenv("DB_PORT", os.getenv("PGPORT", "5432"))),
        }
    }


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


LANGUAGE_CODE = 'en-us'

TIME_ZONE = "Asia/Kolkata"

USE_I18N = True

USE_TZ = True


STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
os.makedirs(STATIC_ROOT, exist_ok=True)
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}


# Email Configuration (Gmail SMTP with console fallback for local development)
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
# Automatically sanitize internal spaces from Google App Passwords (e.g. 'xxxx xxxx xxxx xxxx' -> 'xxxxxxxxxxxxxxxx')
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip().replace(" ", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "noreply@dtc.local")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", 10))

if not (DEBUG or IS_TESTING) and EMAIL_HOST_USER and not EMAIL_HOST_PASSWORD:
    raise RuntimeError(
        "CRITICAL SECURITY ERROR: EMAIL_HOST_USER is configured but EMAIL_HOST_PASSWORD is missing in production (DEBUG=False)!"
    )

if EMAIL_PORT == 465:
    EMAIL_USE_SSL = True
    EMAIL_USE_TLS = False
else:
    EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("true", "1", "t")
    EMAIL_USE_SSL = False

if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

CELERY_BROKER_URL = os.getenv(
    "REDIS_URL",
    os.getenv(
        "REDIS_PRIVATE_URL",
        os.getenv(
            "REDIS_PUBLIC_URL",
            os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
        )
    )
)
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

CELERY_TIMEZONE = "Asia/Kolkata"

CELERY_TASK_TRACK_STARTED = True

CELERY_BEAT_SCHEDULE = {
    "recover_stuck_transactions_every_90_seconds": {
        "task": "coordinator.tasks.recover_stuck_transactions",
        "schedule": 90.0,
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {
            "()": "coordinator.middleware.CorrelationIdFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] [correlation_id=%(correlation_id)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["correlation_id"],
            "formatter": "standard",
        },
    },
    "loggers": {
        "": {
            "handlers": ["console"],
            "level": "INFO",
        },
        "coordinator": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}