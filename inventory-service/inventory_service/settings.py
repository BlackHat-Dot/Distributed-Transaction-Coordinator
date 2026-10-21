from pathlib import Path

import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")

# How 'test' in sys.argv works :
# When you run commands in your terminal. Python puts the words into sys.argv
# Eg., python manage.py runserver ; sys.argv = ['manage.py', 'runserver']
# python manage.py test ; sys.argv = ['manage.py', 'test']
IS_TESTING = "test" in sys.argv[1:2]

# Pattern B: Safe fallback in Development/Testing, Fail-Fast in Production
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or IS_TESTING:
        SECRET_KEY = "django-insecure-h!a*%*sb)2=dg@n=kj3mui#i1+$u6y&0i!c4f0o0d1var6^&cc"
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

MAX_USER_HOLDS = int(os.getenv("MAX_USER_HOLDS", "10"))
HOLD_DURATION_MINUTES = int(os.getenv("HOLD_DURATION_MINUTES", "10"))
HOLD_PRESENCE_GRACE_SECONDS = int(os.getenv("HOLD_PRESENCE_GRACE_SECONDS", "120"))
ALLOW_UNVERIFIED_IDENTITY_HEADERS = IS_TESTING or (os.getenv("ALLOW_UNVERIFIED_IDENTITY_HEADERS", "False").lower() in ("true", "1"))
ALLOW_UNAUTHENTICATED_RESET = DEBUG or IS_TESTING or (os.getenv("ALLOW_UNAUTHENTICATED_RESET", "False").lower() in ("true", "1"))

STAFF_API_KEY = os.getenv("STAFF_API_KEY")
if not STAFF_API_KEY and IS_TESTING:
    STAFF_API_KEY = "dtc-staff-secret-key-2026"
elif not (DEBUG or IS_TESTING) and STAFF_API_KEY == "dtc-staff-secret-key-2026":
    raise RuntimeError(
        "CRITICAL SECURITY ERROR: Insecure default STAFF_API_KEY detected in production (DEBUG=False)!"
    )
JWT_SIGNING_KEY = os.getenv("JWT_SIGNING_KEY")

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
    'corsheaders',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'inventory',
    'rest_framework',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'inventory.middleware.CorrelationIdMiddleware',
    'django.middleware.security.SecurityMiddleware',
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

CORS_PREFLIGHT_MAX_AGE = 86400
# Cache CORS preflight (OPTIONS) requests in the browser for 24 hours to eliminate redundant network round-trips



_csrf_env = os.getenv("CSRF_TRUSTED_ORIGINS")
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = [origin.strip().rstrip("/") for origin in _csrf_env.split(",") if origin.strip()]
else:
    CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

ROOT_URLCONF = 'inventory_service.urls'

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

WSGI_APPLICATION = 'inventory_service.wsgi.application'

from urllib.parse import urlparse

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")

if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    db_name = url.path.lstrip("/") or os.getenv("DB_NAME", "inventory_db")
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
            "NAME": os.getenv("DB_NAME", os.getenv("PGDATABASE", "inventory_db")),
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

CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True

CELERY_TASK_DEFAULT_QUEUE = "inventory_queue"

# ── Option A: Background Celery Beat Sweeper ───────────────────────────────────
# Runs every 30 seconds to update expired HELD seats → AVAILABLE in PostgreSQL.
# Guarantees DB accuracy even during idle periods with no traffic.
CELERY_BEAT_SCHEDULE = {
    "sweep_expired_holds_every_30_seconds": {
        "task": "inventory.tasks.sweep_expired_holds_task",
        "schedule": 30.0,
        "options": {"queue": "inventory_queue"},
    },
}
# ── End Option A ───────────────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = []



LANGUAGE_CODE = 'en-us'

TIME_ZONE = "Asia/Kolkata"

USE_I18N = True

USE_TZ = True



STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
os.makedirs(STATIC_ROOT, exist_ok=True)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {
            "()": "inventory.middleware.CorrelationIdFilter",
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
        "inventory": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

REDIS_URL = CELERY_BROKER_URL

CACHES = {
    "default": {
        "BACKEND": os.getenv(
            "CACHE_BACKEND",
            "django.core.cache.backends.locmem.LocMemCache" if IS_TESTING else "django.core.cache.backends.redis.RedisCache",
        ),
        "LOCATION": REDIS_URL,
    }
}

REST_FRAMEWORK = {
    # NUM_PROXIES determines how many reverse proxy hops to trust when resolving client IP from X-Forwarded-For.
    # Set to 1 for standard reverse-proxy (e.g. AWS ALB, Nginx, Cloudflare), or higher for nested proxies.
    "NUM_PROXIES": int(os.getenv("NUM_PROXIES", "1")),
    "DEFAULT_THROTTLE_RATES": {
        # Fallback rates
        "inventory_user": os.getenv("INVENTORY_USER_THROTTLE_RATE", "60/min"),
        "inventory_anon": os.getenv("INVENTORY_ANON_THROTTLE_RATE", "300/min"),
        # Catalog browsing: generous IP rate for university/office/cellular NAT gateways during on-sale
        "inventory_seat_list": os.getenv("INVENTORY_SEAT_LIST_THROTTLE_RATE", "300/min"),
        # Transaction check: generous IP rate for polling checkout status behind NAT
        "inventory_check": os.getenv("INVENTORY_CHECK_THROTTLE_RATE", "120/min"),
        # Authenticated user actions: strictly scoped per user_id, independent of client IP/NAT
        "inventory_reserve": os.getenv("INVENTORY_RESERVE_THROTTLE_RATE", "5/min"),
        "inventory_release": os.getenv("INVENTORY_RELEASE_THROTTLE_RATE", "10/min"),
        # Unauthenticated / bot protection on mutating routes: scoped per client IP with NAT in mind
        "inventory_reserve_anon": os.getenv(
            "INVENTORY_RESERVE_ANON_THROTTLE_RATE",
            "1000/min" if IS_TESTING else "300/min",
        ),
        "inventory_release_anon": os.getenv(
            "INVENTORY_RELEASE_ANON_THROTTLE_RATE",
            "1000/min" if IS_TESTING else "300/min",
        ),
    },
    "EXCEPTION_HANDLER": "inventory.throttling.inventory_exception_handler",
}



