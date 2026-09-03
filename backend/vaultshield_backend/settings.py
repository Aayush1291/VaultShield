"""Base Django settings for the VaultShield backend.

Implements Task 14.1 — the project scaffold and base settings for the six
apps in design.md's "Backend apps" layout. Covers Requirement 11.1 only to
the extent of making the apps that hold those models exist and be
installed; the model definitions themselves are Task 14.2 (`Project`,
`Run`, `RunEvent`, `RunDiff`, `Screenshot`, `SecurityFinding`, `Approval`)
and Task 14.6 (`SandboxRecord`).

### Everything configurable reads from the environment

design.md's docker-compose topology puts `postgres` and `redis` on the
compose network while the CLI stays on the host talking to `web` over
`localhost` (Requirement 24.2). That means the same settings module is
imported in two places with different service addresses, so every
environment-specific value below is an `os.environ` lookup with a
development default rather than a literal. The defaults are chosen so a
fresh clone with nothing running can still execute
`python manage.py check` — no database connection is opened by the system
checks, so an unreachable PostgreSQL is not an error until `migrate`,
`runserver`, or a test run actually needs it.

### Access control is a default, not a per-view opt-in

Task 15.2 closed the whole `/api/` surface with
`DEFAULT_AUTHENTICATION_CLASSES` + `DEFAULT_PERMISSION_CLASSES` in
`REST_FRAMEWORK` below (Requirements 27.3-27.5). Requirement 27.5 lists
project creation, run creation, event/diff/screenshot ingest, approve,
reject, and `PATCH /api/runs/:id/` — most of which do not exist yet, so
setting the policy globally is what makes each of them protected the
moment it is written instead of depending on whoever writes it
remembering to say so. `POST /api/auth/login/` is the single exemption
and it declares that itself in `accounts/views.py`.

### What is deliberately not here yet

- `channels`: design.md keeps Django Channels in compose "for future
  live-update support, not used for MVP streaming", so it is a Task 32
  service, not an installed app.
"""

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_flag(name: str, default: str = "0") -> bool:
    """Read a boolean-ish environment variable.

    Accepts the shapes a developer or a compose file actually writes
    (`1`/`true`/`yes`/`on`, any case) rather than requiring an exact
    literal, because `DEBUG=True` and `DEBUG=1` are both natural to type.
    """
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> list[str]:
    """Read a comma-separated environment variable into a list.

    Blank entries are dropped so a trailing comma is harmless.
    """
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _env_int(name: str, default: int) -> int:
    """Read a positive integer environment variable.

    Used by the Celery Beat schedule below, whose four numbers are all
    durations: an interval of zero makes Beat tick continuously and a
    threshold of zero makes a cleanup pass act on rows it was meant to
    spare, so both are refused here — at import, naming the variable —
    rather than at the first tick inside a worker log. A blank value is
    treated as unset, matching `_env_flag`/`_env_list`, so an empty
    `VAULTSHIELD_SANDBOX_TTL_MINUTES=` in a compose file or `.env` is the
    default rather than an error.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from exc

    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive number of units, got {value}")

    return value


def _redis_url(db: int) -> str:
    """Build a Redis URL for logical database `db` from the environment.

    Host and port are read the same way PostgreSQL's are — `localhost` for a
    host-run process talking to a port-forwarded compose service,
    `REDIS_HOST=redis` inside the compose network (design.md's topology names
    the service `redis`). Only the database number varies per caller, so it
    is the argument.
    """
    host = os.environ.get("REDIS_HOST", "localhost")
    port = os.environ.get("REDIS_PORT", "6379")
    return f"redis://{host}:{port}/{db}"


# Security ------------------------------------------------------------------

# Development default only: the deployed value must come from the
# environment. Prefixed `django-insecure-` so Django's own deployment
# checklist (`manage.py check --deploy`) flags it if it ever ships.
SECRET_KEY = os.environ.get(
    "VAULTSHIELD_SECRET_KEY",
    "django-insecure-dev-only-key-replace-via-VAULTSHIELD_SECRET_KEY",
)

DEBUG = _env_flag("VAULTSHIELD_DEBUG", "1")

# `web` is included because design.md's compose topology has the
# `frontend` service reach the API by service name over the compose
# network (Requirement 24.3), which arrives as that `Host` header.
ALLOWED_HOSTS = _env_list("VAULTSHIELD_ALLOWED_HOSTS", "localhost,127.0.0.1,web")


# Applications --------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    # Provides `rest_framework.authtoken.models.Token`, which design.md
    # names as the concrete `Dashboard_Auth_Token` type.
    "rest_framework.authtoken",
]

# design.md "Backend apps": one app per bounded piece of the data model.
LOCAL_APPS = [
    "accounts",
    "projects",
    "runs",
    "security_scan",
    "sandbox_tracking",
    "tasks",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vaultshield_backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vaultshield_backend.wsgi.application"
ASGI_APPLICATION = "vaultshield_backend.asgi.application"


# Database ------------------------------------------------------------------

# PostgreSQL per design.md's technology stack. Host defaults to
# `localhost` so a host-run `manage.py` reaches a port-forwarded compose
# database; inside the compose network `POSTGRES_HOST=postgres` overrides
# it. `manage.py check` opens no connection, so these values do not have
# to resolve for the scaffold to validate.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "vaultshield"),
        "USER": os.environ.get("POSTGRES_USER", "vaultshield"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "vaultshield"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Authentication ------------------------------------------------------------

# design.md: "`User` is Django's built-in
# `django.contrib.auth.models.User` — no custom `User` model is defined
# for the MVP", so there is deliberately no `AUTH_USER_MODEL` override.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Django REST Framework -----------------------------------------------------

REST_FRAMEWORK = {
    # Requirements 27.3-27.5: every `/api/` path except `POST
    # /api/auth/login/` requires a valid `Dashboard_Auth_Token`. Both
    # halves are needed and they fail differently:
    #
    # - `TokenAuthentication` is the only way a request can acquire an
    #   identity, and it is what turns the rejection into an
    #   *authentication* error rather than a bare permission denial: it
    #   supplies a `WWW-Authenticate` header, which is what makes DRF
    #   answer 401 instead of 403. A malformed or unknown token is
    #   rejected here, inside the authenticator, before permissions run.
    # - `IsAuthenticated` is what rejects a request that carried no
    #   credential at all — with no authenticators the request would
    #   simply be anonymous, and anonymous passes DRF's default
    #   `AllowAny`.
    #
    # DRF resolves both before dispatching to the handler (in
    # `APIView.initial()`), so an unauthenticated request never reaches a
    # view body: no Project, Run, RunEvent, RunDiff, Screenshot, or
    # Approval is created and no `PATCH /api/runs/:id/` field is written.
    # That "no side effect" half of Requirement 27.4 is a property of
    # where the check runs, not of any code in the views.
    #
    # Session and basic authentication are deliberately absent: design.md
    # specifies `TokenAuthentication` against Django's built-in `User`,
    # and admitting `SessionAuthentication` would let a browser cookie
    # from the Django admin login act as an `/api/` credential, which is
    # a second, unspecified way in.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # JSON in, JSON out, plus multipart for the screenshot upload endpoint
    # (`POST /api/runs/:id/screenshots/`, Task 16.7). The browsable API
    # renderer is left on only while DEBUG is set: it is useful for
    # hand-checking endpoints during the build and pointless in a
    # deployed stack.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    # Run history lists grow without bound, so the list endpoints paginate
    # by default rather than each viewset opting in.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}


# Internationalization ------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
# Every timestamp in the data model is compared against CLI-produced
# timestamps from another machine, so stored datetimes stay tz-aware.
USE_TZ = True


# Static and uploaded files -------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Screenshots arrive as uploaded files (Requirement 13.3) and are served
# back to the dashboard's Screenshots tab, so they need a media root from
# the start rather than once the ingest endpoint lands.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("VAULTSHIELD_MEDIA_ROOT", BASE_DIR / "media"))


# Celery --------------------------------------------------------------------

# Requirement 18.2: the Security_Scan_Service task executes through *the
# configured* broker and result backend, which is what these two settings
# are. `vaultshield_backend/celery.py` reads them by way of
# `config_from_object("django.conf:settings", namespace="CELERY")`, so the
# `CELERY_` prefix is load-bearing: `CELERY_BROKER_URL` here is Celery's
# `broker_url`, and a setting without the prefix is simply not seen.
#
# Redis for both halves, per design.md's technology stack ("Celery + Redis
# (broker and result backend)") and its compose topology, where `web`,
# `celery-worker`, and `celery-beat` all point at the one `redis` service.
#
# Two logical databases rather than one. Redis keeps queue keys and
# `celery-task-meta-*` result keys in whatever database it is handed, and the
# broker's keyspace is the one that gets inspected and flushed while
# debugging a stuck queue; separating them means doing that cannot discard
# results, and `redis-cli -n 0 keys '*'` shows queues only.
#
# The full URLs are overridable in one piece as well as by host/port,
# because a deployed broker is not always addressed as `host:port/db` — a
# password, TLS (`rediss://`), or a managed endpoint all arrive as a whole
# URL. Nothing about a scan's correctness depends on which form is used.
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", _redis_url(0))
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", _redis_url(1))

# Matches Django's `TIME_ZONE`/`USE_TZ` above rather than defaulting
# independently: every timestamp in the data model is UTC, and the
# `CELERY_BEAT_SCHEDULE` below interprets its intervals in this zone.
CELERY_TIMEZONE = TIME_ZONE

# compose's `depends_on: [redis]` waits for the container, not for Redis to
# accept connections, so a worker started with the stack routinely reaches
# the broker a moment early. Retrying makes that a short pause instead of an
# immediate exit. Celery 5.3+ also warns when this is left unset, since the
# default flips in 6.0 — stating it keeps the behaviour the same across both.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True


# Celery Beat schedule ------------------------------------------------------

# Requirements 25.1 and 25.2 both open with "WHILE Celery Beat is running on
# a configured schedule". `tasks/scheduled.py` holds the two tasks
# (Tasks 33.1-33.2) and this is the schedule half: without an entry here
# neither task ever runs on its own, and `celery -A vaultshield_backend beat`
# (compose's `celery-beat` service) starts with nothing to enqueue.
#
# ### Two numbers per task, because they answer different questions
#
# The `schedule` is how often the pass runs; the `kwargs` value is the age at
# which a row qualifies. They are independent — the reaper looks for
# half-hour-stale heartbeats every quarter hour, so a record is closed out
# within about 15 minutes of becoming reapable rather than up to 30 — and
# each is its own environment variable so a deployment can tune the sweep
# without changing what counts as stale, or the reverse. Every one is a
# duration in the unit its name states.
#
# The interval is deliberately shorter than the age threshold it sweeps for.
# The reverse (a slower sweep than the TTL) is not wrong, only slower to
# notice; both tasks are idempotent single `UPDATE`s that return 0 when there
# is nothing to do, so a pass that finds nothing costs one query.
SANDBOX_CLEANUP_INTERVAL_MINUTES = _env_int("VAULTSHIELD_SANDBOX_CLEANUP_INTERVAL_MINUTES", 15)
STUCK_APPROVAL_SCAN_INTERVAL_MINUTES = _env_int(
    "VAULTSHIELD_STUCK_APPROVAL_SCAN_INTERVAL_MINUTES", 60
)

# The thresholds passed to the tasks. Both restate `tasks/scheduled.py`'s
# `DEFAULT_SANDBOX_TTL_MINUTES` / `DEFAULT_APPROVAL_MAX_AGE_HOURS` (design.md's
# 30 minutes and 24 hours) rather than importing them: that module imports
# `runs.models`, and a settings module cannot touch the app registry while
# Django is still assembling it. `vaultshield_backend/tests.py` asserts the two
# pairs agree, so the restatement cannot drift unnoticed.
SANDBOX_TTL_MINUTES = _env_int("VAULTSHIELD_SANDBOX_TTL_MINUTES", 30)
APPROVAL_MAX_AGE_HOURS = _env_int("VAULTSHIELD_APPROVAL_MAX_AGE_HOURS", 24)

# `namespace="CELERY"` in `celery.py` maps this to Celery's `beat_schedule`.
#
# Tasks are named by string, not imported: importing `tasks.scheduled` here
# would drag `runs.models` in before the app registry exists. The strings are
# the names `autodiscover_tasks()` registers (`tasks/tests.py` pins both), and
# `celery.py`'s second autodiscovery call is what makes the module holding
# them discoverable — an app's task module is `<app>/tasks.py` by convention
# and this one is `tasks/scheduled.py`.
#
# `timedelta` intervals rather than `crontab` entries: neither requirement
# names a wall-clock time, only that the pass recurs, and an interval means
# Beat's ticks fall wherever the process started instead of every deployment
# firing on the hour. `CELERY_TIMEZONE` above is UTC, matching every stored
# timestamp the two queries compare against.
CELERY_BEAT_SCHEDULE = {
    # Requirement 25.1 — the sandbox reaper.
    "cleanup-orphaned-sandboxes": {
        "task": "tasks.scheduled.cleanup_orphaned_sandboxes",
        "schedule": timedelta(minutes=SANDBOX_CLEANUP_INTERVAL_MINUTES),
        "kwargs": {"ttl_minutes": SANDBOX_TTL_MINUTES},
    },
    # Requirement 25.2 — the stuck-approval flag.
    "flag-stuck-approvals": {
        "task": "tasks.scheduled.flag_stuck_approvals",
        "schedule": timedelta(minutes=STUCK_APPROVAL_SCAN_INTERVAL_MINUTES),
        "kwargs": {"max_age_hours": APPROVAL_MAX_AGE_HOURS},
    },
}
