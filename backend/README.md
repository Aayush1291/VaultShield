# VaultShield Backend

The Django + Django REST Framework service behind the VaultShield
dashboard: it persists Projects, Runs, and every artifact the CLI captures
(events, diffs, screenshots, security findings, approvals), and runs the
Celery workers that scan diffs and clean up orphaned sandboxes.

## Layout

```
backend/
  manage.py
  vaultshield_backend/     # project package: settings, urls, asgi/wsgi, celery
  accounts/                # dashboard authentication (login view, auth tokens)
  projects/                # Project
  runs/                    # Run, RunEvent, RunDiff, Screenshot, Approval + REST API
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

Settings read from the environment, with development defaults so
`python manage.py check` works on a fresh clone with no services running:

| Variable | Default | Purpose |
|---|---|---|
| `VAULTSHIELD_SECRET_KEY` | insecure dev key | Django `SECRET_KEY` |
| `VAULTSHIELD_DEBUG` | `1` | Django `DEBUG` |
| `VAULTSHIELD_ALLOWED_HOSTS` | `localhost,127.0.0.1,web` | comma-separated `ALLOWED_HOSTS` |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `vaultshield` | PostgreSQL credentials |
| `POSTGRES_HOST` | `localhost` | `postgres` inside the compose network |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `VAULTSHIELD_MEDIA_ROOT` | `backend/media` | where uploaded screenshots are stored |
| `REDIS_HOST` | `localhost` | `redis` inside the compose network |
| `REDIS_PORT` | `6379` | Redis port |
| `CELERY_BROKER_URL` | `redis://$REDIS_HOST:$REDIS_PORT/0` | overrides the whole broker URL (password, TLS, managed endpoint) |
| `CELERY_RESULT_BACKEND` | `redis://$REDIS_HOST:$REDIS_PORT/1` | overrides the whole result backend URL |

The broker and the result backend use two logical Redis databases on the one
server, so inspecting or flushing the broker while debugging a queue cannot
discard stored task results.

## Running

```bash
python manage.py check          # no database connection required
python manage.py migrate        # requires a reachable PostgreSQL
python manage.py runserver
celery -A vaultshield_backend worker   # requires a reachable Redis
```

## Running tests

```bash
pytest                                    # SQLite test database, no services needed
pytest --ds=vaultshield_backend.settings  # against the configured PostgreSQL
```

Tests default to the settings in `vaultshield_backend/settings_test.py`: the
real settings with the *test* database on in-memory SQLite, so the suite runs
on a fresh clone with nothing started. Migrations still run against it.
PostgreSQL-specific behaviour (row locking in the event-ingest view) needs
the second form and a reachable database.
