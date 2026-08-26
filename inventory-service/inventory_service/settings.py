from pathlib import Path

import os
from dotenv import load_dotenv

load_dotenv()


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-h!a*%*sb)2=dg@n=kj3mui#i1+$u6y&0i!c4f0o0d1var6^&cc")

DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "t")

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",") if os.getenv("ALLOWED_HOSTS") else ["*"]




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
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = [
    "x-total-count",
    "x-overall-total",
    "x-page-limit",
    "x-page-offset",
    "x-correlation-id",
    "content-type",
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        "https://*.railway.app,https://*.up.railway.app,http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if origin.strip()
]
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
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": url.path.lstrip("/") or os.getenv("DB_NAME", os.getenv("PGDATABASE", "inventory_db")),
            "USER": url.username or os.getenv("DB_USER", os.getenv("PGUSER", "postgres")),
            "PASSWORD": url.password or os.getenv("DB_PASSWORD", os.getenv("PGPASSWORD", "postgres")),
            "HOST": url.hostname or os.getenv("DB_HOST", os.getenv("PGHOST", "localhost")),
            "PORT": str(url.port or os.getenv("DB_PORT", os.getenv("PGPORT", "5432"))),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", os.getenv("PGDATABASE", "inventory_db")),
            "USER": os.getenv("DB_USER", os.getenv("PGUSER", "postgres")),
            "PASSWORD": os.getenv("DB_PASSWORD", os.getenv("PGPASSWORD", "postgres")),
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


MAILERS = {
    'default': {
        'BACKEND': 'django.core.mail.backends.console.EmailBackend',
    },
}

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
