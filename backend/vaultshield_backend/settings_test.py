"""Settings the test suite runs under: the real settings, on a scratch MongoDB.

### Why this module still exists after the MongoDB migration

Before the migration it existed to swap PostgreSQL for in-memory SQLite, so
`pytest` needed no running service. MongoDB has no in-process equivalent —
there is no `sqlite3 :memory:` for it — so that trick is gone and the suite
genuinely needs a reachable MongoDB deployment.

What this module does now is narrower but still worth having: it lets the tests
point at a *different* deployment from the one `runserver` uses, through
`VAULTSHIELD_TEST_MONGODB_URL`. That matters because Django's test runner
creates and **drops** a database around the run. Pointing tests at the same
cluster as development is fine (they land in `test_<name>`, not `<name>`), but
being able to send them somewhere else means CI can use a local throwaway
`mongod` container instead of reaching a shared Atlas cluster over the network:

    # against whatever VAULTSHIELD_MONGODB_URL names (the default)
    pytest

    # against a local replica set, no network, much faster
    VAULTSHIELD_TEST_MONGODB_URL=mongodb://localhost:27017/vaultshield pytest

`pyproject.toml`'s `DJANGO_SETTINGS_MODULE` points here, so plain `pytest`
picks it up.

### The deployment must be a replica set or a sharded cluster

Not a preference — MongoDB only permits multi-document transactions on one of
those, and `runs/views.py` / `security_scan/tasks.py` depend on real
transactions for the approve/reject state machine, all-or-nothing event
batches, and the delete-then-insert in the security scan. A standalone `mongod`
runs the suite right up to the transaction tests and then fails them, which is
the correct outcome: those tests exist to prove the atomicity is real, so
silently skipping them would defeat the point.

MongoDB Atlas is always a replica set, and Atlas is what `docker-compose.yml`
points the stack at — there is no `mongo` service in the compose topology,
because the database is external to it. A local single-node `mongod` used for
tests has to be started with `--replSet` and have the set initiated, or the
transaction tests will fail.

### What is deliberately NOT overridden

`DATABASES["default"]["ENGINE"]`, the models, the migrations, and every
`django_mongodb_backend.transaction` call site are all the real thing. The only
divergence from a production run is which cluster the URL points at and the
`test_` database prefix Django adds. There is no second database backend
anywhere in the project, so there is no PostgreSQL/SQLite behaviour left for a
test to accidentally depend on — which was the standing hazard the old version
of this file documented.
"""

import os

from vaultshield_backend.settings import *  # noqa: F403

# `VAULTSHIELD_TEST_MONGODB_URL` when set, otherwise whatever the real settings
# already resolved. Reusing the resolved value rather than re-reading
# `VAULTSHIELD_MONGODB_URL` keeps the "no URL configured" error in one place
# (`settings._mongodb_settings`) instead of raising a second, different one here.
_TEST_MONGODB_URL = os.environ.get("VAULTSHIELD_TEST_MONGODB_URL", "").strip()

if _TEST_MONGODB_URL:
    # Same shape as the real settings build: the connection string in HOST,
    # with NAME used only when the URI's path does not name a database.
    DATABASES = {  # noqa: F405
        "default": {
            "ENGINE": "django_mongodb_backend",
            "HOST": _TEST_MONGODB_URL,
            "NAME": os.environ.get("VAULTSHIELD_MONGODB_NAME", "").strip() or "vaultshield",
        }
    }

# Speeds the suite up materially and changes nothing it asserts: several tests
# create Users, and the default PBKDF2 hasher is deliberately slow.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
