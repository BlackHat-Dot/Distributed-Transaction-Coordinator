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

raw_allowed_hosts = os.getenv("ALLOWED_HOSTS")
if raw_allowed_hosts:
    ALLOWED_HOSTS = [host.strip() for host in raw_allowed_hosts.split(",") if host.strip()]
elif DEBUG or IS_TESTING:
    ALLOWED_HOSTS = ["*"]
else:
    raise RuntimeError(
        "CRITICAL SECURITY ERROR: ALLOWED_HOSTS environment variable is not set in production (DEBUG=False)!"
    )

INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY")
if not INTERNAL_SERVICE_KEY:
    if DEBUG or IS_TESTING:
        INTERNAL_SERVICE_KEY = "dtc-internal-coordinator-secret-key-2026"
    else:
        raise RuntimeError(
            "CRITICAL SECURITY ERROR: INTERNAL_SERVICE_KEY environment variable is not set in production (DEBUG=False)!"
        )
elif not (DEBUG or IS_TESTING) and INTERNAL_SERVICE_KEY == "dtc-internal-coordinator-secret-key-2026":
    raise RuntimeError(
        "CRITICAL SECURITY ERROR: Insecure default INTERNAL_SERVICE_KEY detected in production (DEBUG=False)! "
        "You must configure a strong, unique INTERNAL_SERVICE_KEY for production."
    )

INSTALLED_APPS = [
    'django.contrib.admin', # standard
    'django.contrib.auth', # standard
    'django.contrib.contenttypes', # standard
    'django.contrib.sessions', # standard
    'django.contrib.messages', # standard
    'django.contrib.staticfiles', # standard
    'corsheaders', # added
    "rest_framework", # added
    "coordinator" # added
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware', # added
    'coordinator.middleware.CorrelationIdMiddleware', # added
    'whitenoise.middleware.WhiteNoiseMiddleware', # added
    'django.middleware.security.SecurityMiddleware', # standard
    'django.contrib.sessions.middleware.SessionMiddleware', # standard
    'django.middleware.common.CommonMiddleware', # standard
    'django.middleware.csrf.CsrfViewMiddleware', # standard
    'django.contrib.auth.middleware.AuthenticationMiddleware', # standard
    'django.contrib.messages.middleware.MessageMiddleware', # standard
    'django.middleware.clickjacking.XFrameOptionsMiddleware', # standard
]

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True

CORS_ALLOWED_ORIGINS = []

if DEBUG or IS_TESTING:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        r"^http://localhost(:\d+)?$",
        r"^http://127\.0\.0\.1(:\d+)?$",
    ]
else:
    CORS_ALLOWED_ORIGIN_REGEXES = []

    _cors_env = os.getenv("CORS_ALLOWED_ORIGINS")
    if _cors_env:
        CORS_ALLOWED_ORIGINS.extend(
            [origin.strip().rstrip("/") for origin in _cors_env.split(",") if origin.strip()]
        )


CORS_ALLOWED_ORIGINS = list(dict.fromkeys(CORS_ALLOWED_ORIGINS))
# removes duplicate origins

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
    "x-user-id",
    "x-user-email",
]
# allows frontend to send this headers

CORS_EXPOSE_HEADERS = [
    "x-total-count",
    "x-overall-total",
    "x-page-limit",
    "x-page-offset",
    "x-correlation-id",
    "x-user-id",
    "x-user-email",
    "content-type",
]
# allows frontend to read these listed from response headers


_csrf_env = os.getenv("CSRF_TRUSTED_ORIGINS")
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = [origin.strip().rstrip("/") for origin in _csrf_env.split(",") if origin.strip()]
else:
    CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# Railway/Reverse-proxy terminates SSL (HTTPS) and forwards internal traffic to Django over HTTP.
# This setting tells Django to trust the 'X-Forwarded-Proto: https' header so it knows the 
# original user connection was secure (preventing CSRF mismatches and redirect loops).

ROOT_URLCONF = 'coordinator_service.urls'
# tells django "Whenever an HTTP request arrives, open the file at coordinator_service/urls.py to find out where to send it."

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


AUTH_PASSWORD_VALIDATORS = []


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
CELERY_RESULT_BACKEND = None

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

CELERY_TASK_IGNORE_RESULT = True

CELERY_TIMEZONE = "Asia/Kolkata"

CELERY_TASK_DEFAULT_QUEUE = "coordinator_queue"

CELERY_BEAT_SCHEDULE = {
    "recover_stuck_transactions_every_90_seconds": {
        "task": "coordinator.tasks.recover_stuck_transactions",
        "schedule": 90.0,
        "options": {"queue": "coordinator_queue"},
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