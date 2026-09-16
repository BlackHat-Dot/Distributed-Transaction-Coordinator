from pathlib import Path

import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")
IS_TESTING = 'test' in sys.argv

# Pattern B: Safe fallback in Development/Testing, Fail-Fast in Production
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or IS_TESTING:
        SECRET_KEY = "django-insecure-bpllbni(s#$6elvc&luq-xmvhm5k##=6o7d3q^*91f)0n$1t#z"
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


INSTALLED_APPS = [
    'corsheaders',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'payments'
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'payments.middleware.CorrelationIdMiddleware',
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

CORS_ALLOWED_ORIGINS = []

if DEBUG or IS_TESTING:
    CORS_ALLOWED_ORIGINS.extend([
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8001",
        "http://127.0.0.1:8001",
        "http://localhost:8002",
        "http://127.0.0.1:8002",
    ])
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

_csrf_env = os.getenv("CSRF_TRUSTED_ORIGINS")
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = [origin.strip().rstrip("/") for origin in _csrf_env.split(",") if origin.strip()]
else:
    CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

ROOT_URLCONF = 'payment_service.urls'

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

WSGI_APPLICATION = 'payment_service.wsgi.application'

from urllib.parse import urlparse

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")

if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    db_name = url.path.lstrip("/") or os.getenv("DB_NAME", "payment_db")
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
            "NAME": os.getenv("DB_NAME", os.getenv("PGDATABASE", "payment_db")),
            "USER": os.getenv("DB_USER", os.getenv("PGUSER", "postgres")),
            "PASSWORD": db_password,
            "HOST": os.getenv("DB_HOST", os.getenv("PGHOST", "localhost")),
            "PORT": str(os.getenv("DB_PORT", os.getenv("PGPORT", "5432"))),
        }
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


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {
            "()": "payments.middleware.CorrelationIdFilter",
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
        "payments": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
