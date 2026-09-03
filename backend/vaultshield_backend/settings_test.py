"""Settings the test suite runs under: the real settings, on SQLite.

Everything written before Task 22.3 tested pure functions — rule matching,
serializer validation, argv construction — so the suite needed no database
at all. `run_security_scan` is the first piece whose whole job is a write:
it hands a `RunDiff` to `scan_diff` and persists what comes back, and a test
that never reads those rows back is not testing the task.

`settings.DATABASES` points at PostgreSQL (design.md's technology stack),
which is reachable inside the compose network and from a developer who has
started it — but not on a fresh clone with nothing running. `settings.py`
already keeps `manage.py check` working in that state; this keeps `pytest`
working in it too. `pyproject.toml`'s `DJANGO_SETTINGS_MODULE` points here,
so plain `pytest` picks it up:

    pytest                                        # SQLite (default)
    pytest --ds=vaultshield_backend.settings      # the configured PostgreSQL

Overriding the settings *module* rather than patching `DATABASES` from a
fixture is deliberate: by the time any fixture runs, Django has already
built and cached a connection for the `default` alias, so a late
`DATABASES` swap leaves that stale wrapper in place and the run still
dials PostgreSQL. Choosing the module up front has no such ordering
question.

**The divergence this accepts**, stated so a later test task recognises it
rather than debugging it: SQLite is not PostgreSQL, and the closest thing to
a trap in this schema is `select_for_update()` in the event-ingest view —
Django raises `NotSupportedError` for it on SQLite. A test covering that
locking behaviour has to run under `--ds=vaultshield_backend.settings`
against a real PostgreSQL. Nothing is silently skipped; the failure is loud
and names the reason.

Only the test run is affected. `runserver`, `migrate`, and the Celery
workers import `settings` and still talk to PostgreSQL.
"""

from vaultshield_backend.settings import *  # noqa: F403

# In-memory rather than a file: a test database that needs no service and
# leaves nothing behind. Migrations still run against it, so the schema
# under test is the one the migrations describe.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:"},
    }
}
