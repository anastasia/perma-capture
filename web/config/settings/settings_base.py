"""
Django settings for config project.

Generated by 'django-admin startproject' using Django 3.0.8.

For more information on this file, see
https://docs.djangoproject.com/en/3.0/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.0/ref/settings/
"""

import os
from copy import deepcopy

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'whitenoise.runserver_nostatic',

    # apps
    'main',

    # third party
    'django_extensions',
    'rest_framework',
    'rest_framework.authtoken',

    # built-in
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'config.context_processors.settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/3.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'postgres',
        'USER': 'postgres',
        'PASSWORD': 'password',
        'HOST': 'db',
        'PORT': 5432,
    }
}


AUTH_USER_MODEL = 'main.User'
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'index'
CSRF_FAILURE_VIEW = 'main.views.csrf_failure'

# Password validation
# https://docs.djangoproject.com/en/3.0/ref/settings/#auth-password-validators

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

AUTHENTICATION_BACKENDS = ('main.auth.ConfirmedUserSessionBackend',)

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'main.auth.ConfirmedUserTokenBackend',
        'rest_framework.authentication.SessionAuthentication',  # authenticate with Django login
    ),
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': (
        'djangorestframework_camel_case.render.CamelCaseJSONRenderer',
        'djangorestframework_camel_case.render.CamelCaseBrowsableAPIRenderer',
    ),
    'DEFAULT_PARSER_CLASSES': (
        'djangorestframework_camel_case.parser.CamelCaseFormParser',
        'djangorestframework_camel_case.parser.CamelCaseMultiPartParser',
        'djangorestframework_camel_case.parser.CamelCaseJSONParser',
    ),
    'NON_FIELD_ERRORS_KEY': 'error',
    'TEST_REQUEST_DEFAULT_FORMAT': 'json'
}

ALLOW_SIGNUPS = False
PASSWORD_RESET_TIMEOUT_DAYS = 1

VERIFY_WEBHOOK_SIGNATURE = False
CAPTURE_SERVICE_WEBHOOK_SIGNING_KEY = ''
CAPTURE_SERVICE_WEBHOOK_SIGNING_KEY_ALGORITHM = None

SEND_WEBHOOK_DATA_TO_CAPTURE_SERVICE = False
EXPOSE_WEBHOOK_TEST_ROUTE = False

# Internationalization
# https://docs.djangoproject.com/en/3.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.0/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static')


# avoid the need for collectstatic in production (see http://whitenoise.evans.io/en/stable/django.html#WHITENOISE_USE_FINDERS )
WHITENOISE_USE_FINDERS = True

from django.utils.log import DEFAULT_LOGGING
LOGGING = deepcopy(DEFAULT_LOGGING)
LOGGING['handlers'] = {
    **LOGGING['handlers'],
    # log everything to console on both dev and prod
    'console': {
        'level': 'DEBUG',
        'class': 'logging.StreamHandler',
        'formatter': 'standard',
    },
    # custom error email template
    'mail_admins': {
        'level': 'ERROR',
        'filters': ['require_debug_false'],
        'class': 'main.reporter.CustomAdminEmailHandler'
    },
    # log to file
    'file': {
        'level':'INFO',
        'class':'logging.handlers.RotatingFileHandler',
        'filename': '/tmp/django.log',
        'maxBytes': 1024*1024*5, # 5 MB
        'backupCount': 5,
        'formatter':'standard',
    },
}
LOGGING['loggers'] = {
    **LOGGING['loggers'],
    # only show warnings for third-party apps
    '': {
        'level': 'WARNING',
        'handlers': ['console', 'mail_admins', 'file'],
    },
    # disable django's built-in handlers to avoid double emails
    'django': {
        'level': 'WARNING'
    },
    'django.request': {
        'level': 'ERROR'
    },
    'celery.django': {
        'level': 'INFO',
        'handlers': ['console', 'mail_admins', 'file'],
    },
    # show info for our first-party apps
    **{
        app_name: {'level': 'INFO'}
        for app_name in ('main',)
    },
}
LOGGING['formatters'] = {
    **LOGGING['formatters'],
    'standard': {
        'format': '%(asctime)s [%(levelname)s] %(filename)s %(lineno)d: %(message)s'
    },
}


### CELERY settings ###

if os.environ.get('DOCKERIZED'):
    CELERY_BROKER_URL = 'redis://redis:6379/1'
else:
    CELERY_BROKER_URL = 'redis://guest:guest@localhost::6379/1'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'

# if celery is launched with --autoscale=1000, celery will autoscale to 1000 but limited by system resources:
CELERY_WORKER_AUTOSCALER = 'celery_resource_autoscaler:ResourceAutoscaler'
CELERY_RESOURCE_LIMITS = [
    {
        'class': 'celery_resource_autoscaler:MemoryLimit',
        'kwargs': {'max_memory': 0.8},
    },
    {
        'class': 'celery_resource_autoscaler:CPULimit',
        'kwargs': {'max_load': 0.8},
    },
]

# These default time limits are useful in Perma, where capturing tasks
# sometimes hang, for still-undiagnosed reasons, and where all tasks
# are expected to be short-lived.
#
# It remains to be determined whether they will prove appropriate here.
#
# If a task is running longer than five minutes, ask it to shut down
CELERY_TASK_SOFT_TIME_LIMIT=300
# If a task is running longer than seven minutes, kill it
CELERY_TASK_TIME_LIMIT = 420

# Control whether Celery tasks should be run asynchronously in a background worker
# process or synchronously in the main thread of the calling script / Django request.
# This should normally be False, but it's handy to not require the broker and a
# celery worker daemon to be running sometimes... for instance, if you want to drop into pdb.
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_EAGER_PROPAGATES = True  # propagate exceptions when CELERY_TASK_ALWAYS_EAGER=True

# Lets you route particular tasks to particular queues.
# Mentioning a new queue creates it.
CELERY_TASK_ROUTES = {}

# from celery.schedules import crontab
CELERY_BEAT_SCHEDULE = {}

### \END CELERY settings ###

# Make these settings available for use in Django's templates.
# e.g. <a href="mailto:{{ CONTACT_EMAIL }}">Contact Us</a>
TEMPLATE_VISIBLE_SETTINGS = (
    'APP_NAME',
    'CONTACT_EMAIL',
    'USE_ANALYTICS',
    'ACCESSIBILITY_POLICY_URL',
    'PASSWORD_RESET_TIMEOUT_DAYS',
)

DEFAULT_FROM_EMAIL = 'info@perma.cc'
CONTACT_EMAIL = 'info@perma.cc'

ACCESSIBILITY_POLICY_URL = 'https://accessibility.huit.harvard.edu/digital-accessibility-policy'

# LIL's analytics JS
USE_ANALYTICS = False

# Since we don't know yet...
APP_NAME = 'capture.perma.cc'

TESTING = False

API_PREFIX = 'api'

# Capture Service
BACKEND_API = "http://capture-service"
# Right now, the capture service is sending incorrect access URLs locally.
# Until that is fixed, use this setting to correct the netloc returned by the API.
# Example:
# {'internal': 'host.docker.internal:9000', 'external': 'localhost:9000'}
OVERRIDE_ACCESS_URL_NETLOC = None
# We have discussed the possibility that the capture service might communicate
# with this application via a private network connection, rather than over
# the public internet or using the service's public name. If that's the case,
# use this setting to specify the netloc to which the capture service should POST
# it's webhook callback, on completing an archive (since build_absolute_uri, which
# we use otherwise, will return the public-facing address).
# This can also be used in development, to direct the traffic to host.docker.internal
# instead of localhost.
# Example:
# "http://host.docker.internal:8000"
CALLBACK_PREFIX = None

# Playback
RWP_BASE_URL = "https://cdn.jsdelivr.net/npm/replaywebpage@1.1.2"
