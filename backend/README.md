# VaultShield Backend

The Django + Django REST Framework service behind the VaultShield
dashboard: it persists Projects, Runs, and every artifact the CLI captures
(events, diffs, screenshots, security findings, approvals), and runs the
Celery workers that scan diffs and clean up orphaned sandboxes.

**Datastore: MongoDB.** PostgreSQL was removed entirely — there is no
`postgres` service, no psycopg dependency, and no second database alias. The
one alias uses [`django-mongodb-backend`](https://pypi.org/project/django-mongodb-backend/),
MongoDB's official Django backend. Redis remains, as Celery's broker and result
backend.

## Layout

```
backend/
  manage.py
  vaultshield_backend/     # project package: settings, urls, asgi/wsgi, celery
    apps.py                # AppConfigs forcing ObjectId primary keys onto contrib apps
    mongodb.py             # datetime precision + transient-transaction retry
    drf_mongodb.py         # DRF serializer fields for ObjectId
    contrib_migrations/    # MongoDB-compatible migrations for admin/auth/contenttypes
    test_mongodb.py        # the MongoDB integration + concurrency suite
  accounts/                # dashboard authentication (login view, auth tokens)
  projects/                # Project
  runs/                    # Run, RunEvent, RunDiff, Screenshot, Approval + REST API
    sequences.py           # atomic event-sequence allocation
  security_scan/           # SecurityFinding, static rule registry, scan task
  sandbox_tracking/        # SandboxRecord
  tasks/                   # Celery Beat periodic tasks
```

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Settings read from the environment. Every value has a development default
except the database URL, which has none: MongoDB is the only system of record,
so `settings.py` raises `ImproperlyConfigured` at import rather than falling
back to anything.

| Variable | Default | Purpose |
|---|---|---|
| `VAULTSHIELD_MONGODB_URL` | **required** | MongoDB connection string; the whole `DATABASES["default"]["HOST"]` |
| `VAULTSHIELD_MONGODB_NAME` | `vaultshield` | database inside the cluster, used only when the URL's path names none |
| `VAULTSHIELD_SECRET_KEY` | insecure dev key | Django `SECRET_KEY` |
| `VAULTSHIELD_DEBUG` | `1` | Django `DEBUG` |
| `VAULTSHIELD_ALLOWED_HOSTS` | `localhost,127.0.0.1,web` | comma-separated `ALLOWED_HOSTS` |
| `VAULTSHIELD_MEDIA_ROOT` | `backend/media` | where uploaded screenshots are stored |
| `REDIS_HOST` | `localhost` | `redis` inside the compose network |
| `REDIS_PORT` | `6379` | Redis port |
| `CELERY_BROKER_URL` | `redis://$REDIS_HOST:$REDIS_PORT/0` | overrides the whole broker URL (password, TLS, managed endpoint) |
| `CELERY_RESULT_BACKEND` | `redis://$REDIS_HOST:$REDIS_PORT/1` | overrides the whole result backend URL |
| `VAULTSHIELD_TEST_MONGODB_URL` | unset | sends the test suite at a different cluster |
| `VAULTSHIELD_SANDBOX_CLEANUP_INTERVAL_MINUTES` | `15` | Beat: how often the sandbox reaper runs |
| `VAULTSHIELD_SANDBOX_TTL_MINUTES` | `30` | Beat: heartbeat age that makes a record reapable |
| `VAULTSHIELD_STUCK_APPROVAL_SCAN_INTERVAL_MINUTES` | `60` | Beat: how often the stuck-approval sweep runs |
| `VAULTSHIELD_APPROVAL_MAX_AGE_HOURS` | `24` | Beat: age at which an unsubmitted approval is flagged |

Django does not load `.env` files (there is no `python-dotenv` dependency), so
`backend/.env` is a convenience file you load yourself. The connection string
lives in the repo-root `.env` because that is where `docker compose` reads it,
so a host-run backend wants both:

```bash
set -a; . ./.env; . ./backend/.env; set +a
```

The broker and the result backend use two logical Redis databases on the one
server, so inspecting or flushing the broker while debugging a queue cannot
discard stored task results.

### The deployment must be a replica set or a sharded cluster

MongoDB permits multi-document transactions only on a replica set or a sharded
cluster, and three code paths depend on real transactions: the approve/reject
state machine, the all-or-nothing event batch, and the security scan's
delete-then-insert. Atlas is always a replica set. A standalone `mongod` starts
the stack fine and then fails those writes, so for local work start it with
`--replSet rs0` and initiate the set.

## Running

```bash
python manage.py check          # no database connection required
python manage.py migrate        # requires a reachable MongoDB
python manage.py runserver
celery -A vaultshield_backend worker   # requires a reachable Redis
```

`migrate` creates the collections and their indexes, including the unique
compound index on `(run_id, sequence)` that backs event ordering. It is
idempotent, and `docker-compose.yml` runs it from the `web` service on start.

## Running tests

```bash
pytest                          # against VAULTSHIELD_MONGODB_URL, in a test_ database
VAULTSHIELD_TEST_MONGODB_URL=mongodb://localhost:27017/vaultshield pytest
```

Tests use `vaultshield_backend/settings_test.py`: the real settings, with the
database URL optionally redirected. Unlike the PostgreSQL era there is no
in-memory substitute — MongoDB has no `sqlite3 :memory:` equivalent — so the
suite needs a reachable deployment, and it must be a replica set because the
transaction tests exercise real transactions. Django creates and drops a
`test_`-prefixed database around the run, so it never touches development data.

`vaultshield_backend/test_mongodb.py` holds the MongoDB-specific suite: real
threaded concurrency against the sequence allocator and the approve/reject state
machine, and injected mid-operation failures at every boundary that used to rely
on a PostgreSQL transaction.

## MongoDB-specific implementation notes

Four PostgreSQL mechanisms had no direct MongoDB equivalent and were replaced.
Each replacement is documented at its call site; this is the map.

| Concern | Was | Now |
|---|---|---|
| Event `sequence` numbering | `SELECT ... FOR UPDATE` + `MAX(sequence)` | atomic `$inc` on the Run document (`runs/sequences.py`) plus a unique `(run, sequence)` index |
| Atomicity | `django.db.transaction.atomic` | `django_mongodb_backend.transaction.atomic` |
| Scan-after-diff ordering | Django's `on_commit` | the backend's `on_commit` |
| Sandbox reaper | one join-filtered `UPDATE` | two single-collection queries (`tasks/scheduled.py`) |

Three things to know when editing this codebase:

- **Never import `django.db.transaction`.** `django-mongodb-backend` reports
  `supports_transactions = False`, so Django silently turns
  `transaction.atomic()` into a no-op and fires `on_commit` callbacks
  immediately. Nothing raises; the atomicity just disappears. Use
  `django_mongodb_backend.transaction` instead.
- **`select_for_update()` is silently ignored**, not rejected. It returns rows
  and takes no lock, so code that relies on it appears to work.
- **Primary keys are `ObjectId`**, so `id` is a 24-character hex string in every
  API response rather than an integer. `drf_mongodb.py` is what makes DRF
  serialize it; without that registration DRF maps the field to `IntegerField`
  and every endpoint raises `TypeError`.

Timestamps are stored with millisecond precision (BSON's date type is integer
milliseconds), so values generated in Python are truncated on the way in. Code
that writes a timestamp and then echoes it back passes it through
`mongodb.to_mongo_precision()` first, so the response and a later re-read agree.
