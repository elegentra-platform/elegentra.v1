import os
from dotenv import load_dotenv
from importlib.util import find_spec

# Load .env file
load_dotenv()

from pathlib import Path

try:
    import dj_database_url
except ImportError:  # pragma: no cover - local fallback until requirements are installed
    dj_database_url = None

BASE_DIR = Path(__file__).resolve().parent.parent
SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")

# ==================================================
# CORE
# ==================================================
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-local-development-only")
CROSS_PLATFORM_CONNECT_SECRET = os.getenv("CROSS_PLATFORM_CONNECT_SECRET", "local-elegentra-myfestivo-experiment")
MYFESTIVO_CONNECT_URL = os.getenv("MYFESTIVO_CONNECT_URL", "http://localhost:8000/elegentra/connect/").strip()
DEBUG = os.getenv("DJANGO_DEBUG", os.getenv("DEBUG", "True")).lower() == "true"

default_hosts = {"localhost", "127.0.0.1", ".localhost", ".trycloudflare.com", ".up.railway.app"}
ELEGENTRA_ROOT_DOMAIN = os.getenv("ELEGENTRA_ROOT_DOMAIN", "elegentra.com").strip() or "elegentra.com"
default_hosts.add(ELEGENTRA_ROOT_DOMAIN)
default_hosts.add(f".{ELEGENTRA_ROOT_DOMAIN}")
railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
if railway_domain:
    default_hosts.add(railway_domain)
    default_hosts.add(f".{railway_domain}")

extra_hosts = {
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "").split(",")
    if host.strip()
}
ALLOWED_HOSTS = sorted(default_hosts | extra_hosts)

csrf_trusted_origins = {
    "https://*.trycloudflare.com",
    "https://*.up.railway.app",
    f"https://{ELEGENTRA_ROOT_DOMAIN}",
    f"https://*.{ELEGENTRA_ROOT_DOMAIN}",
}
if railway_domain:
    csrf_trusted_origins.add(f"https://{railway_domain}")
csrf_trusted_origins |= {
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
}
CSRF_TRUSTED_ORIGINS = sorted(csrf_trusted_origins)


# ==================================================
# APPLICATIONS
# ==================================================
INSTALLED_APPS = [
    # Django core
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',

    # Third-party
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'rest_framework',

    # Local apps
    'public',
    'accounts',
    'saloons',
    'services',
    'plans',
    'analytics',
    'staffpanel',
]

SITE_ID = 1

# ==================================================
# MIDDLEWARE
# ==================================================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    # allauth
    'allauth.account.middleware.AccountMiddleware',
]

if find_spec("whitenoise"):
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')

# ==================================================
# URLS / WSGI
# ==================================================
ROOT_URLCONF = 'core.urls'
WSGI_APPLICATION = 'core.wsgi.application'

# ==================================================
# TEMPLATES
# ==================================================
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',  # REQUIRED
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.google_oauth_enabled',
                'accounts.context_processors.user_notifications',
            ],
        },
    },
]

# ==================================================
# DATABASE
# ==================================================
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

database_url = os.getenv("DATABASE_URL", "").strip()
if database_url and dj_database_url:
    DATABASES["default"] = dj_database_url.parse(database_url, conn_max_age=600, ssl_require=not DEBUG)

# ==================================================
# AUTHENTICATION
# ==================================================
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'
ACCOUNT_DEFAULT_HTTP_PROTOCOL = os.getenv(
    "ACCOUNT_DEFAULT_HTTP_PROTOCOL",
    "https" if not DEBUG else "http",
).strip().lower()

# ==================================================
# DJANGO-ALLAUTH
# ==================================================
ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False

ACCOUNT_EMAIL_VERIFICATION = 'mandatory'
ACCOUNT_CONFIRM_EMAIL_ON_GET = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True

ACCOUNT_LOGOUT_ON_GET = True
ACCOUNT_PREVENT_ENUMERATION = False

SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_AUTO_SIGNUP = True

SOCIALACCOUNT_ADAPTER = 'accounts.adapters.CustomSocialAccountAdapter'

google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
        "OAUTH_PKCE_ENABLED": True,
        "FETCH_USERINFO": True,
    }
}

if google_client_id and google_client_secret:
    SOCIALACCOUNT_PROVIDERS["google"]["APPS"] = [
        {
            "client_id": google_client_id,
            "secret": google_client_secret,
            "key": "",
        }
    ]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# ==================================================
# PASSWORD VALIDATION
# ==================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ==================================================
# INTERNATIONALIZATION
# ==================================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# ==================================================
# STATIC FILES
# ==================================================
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

if find_spec("whitenoise"):
    STORAGES["staticfiles"] = {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    }

MEDIA_URL = os.getenv("MEDIA_URL", "/media/")
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media"))).resolve()

USE_R2 = os.getenv("USE_R2", "False").lower() == "true"
if USE_R2:
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME", "").strip()
    AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL", "").strip()
    AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "auto").strip() or "auto"
    AWS_S3_ADDRESSING_STYLE = os.getenv("AWS_S3_ADDRESSING_STYLE", "path").strip() or "path"
    AWS_QUERYSTRING_AUTH = os.getenv("AWS_QUERYSTRING_AUTH", "True").lower() == "true"
    AWS_DEFAULT_ACL = None
    AWS_S3_FILE_OVERWRITE = False
    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": "max-age=86400",
    }

    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    }

# ==================================================
# DEFAULT PK
# ==================================================
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ==================================================
# EMAIL (GMAIL SMTP)
# ==================================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True

EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", 'zevralabs@gmail.com')
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", f'Elegentra <{EMAIL_HOST_USER}>')


if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

