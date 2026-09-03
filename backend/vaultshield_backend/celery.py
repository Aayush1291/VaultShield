"""The VaultShield Celery application.

Task 23.1 — Requirement 18.2: the Security_Scan_Service task executes
through a configured broker and result backend. This module is the "app"
half of that; `settings.py`'s `CELERY_*` block is the "configured" half.

    celery -A vaultshield_backend worker
    celery -A vaultshield_backend beat

Both compose services from design.md's topology (`celery-worker`,
`celery-beat`) name this module through `-A vaultshield_backend`, which
finds `celery_app` re-exported from the package `__init__.py`.

### Why every line here is the stock Django recipe

`config_from_object("django.conf:settings", namespace="CELERY")` means the
broker URL, result backend, and (from Task 33.4) the beat schedule live in
`settings.py` with everything else that reads from the environment, instead
of a second configuration surface here that would have to be kept in step
with it. The `CELERY_` prefix is what keeps those settings distinguishable
from Django's own in a single flat namespace.

Passing the settings module *by name* rather than importing it matters for
ordering: the string is resolved lazily, on first access to `app.conf`, so
importing this module — which `__init__.py` does on every `manage.py`
invocation — does not force Django's settings to load.

`autodiscover_tasks()` with no arguments imports `<app>/tasks.py` for every
entry in `INSTALLED_APPS`, which is how `security_scan.tasks.run_security_scan`
(Task 22.3) gets registered without the module being named here. It is also
deferred: with `force=False` it subscribes to Celery's `import_modules` signal
rather than scanning immediately, so the app registry is not walked while
Django is still building it. The worker fires that signal at startup; a test
that wants the same effect calls `app.loader.import_default_modules()`.

### Why there are two autodiscovery calls

`related_name` is the *module* autodiscovery looks for inside each installed
app, and it defaults to `tasks`. design.md's "Backend apps" layout gives the
`tasks` app one module named `scheduled.py`, so the default scan looks for
`tasks.tasks` — which does not exist — and would leave
`cleanup_orphaned_sandboxes` and `flag_stuck_approvals` (Tasks 33.1-33.2)
unregistered in the worker. Beat would still enqueue them from
`CELERY_BEAT_SCHEDULE`, since a schedule entry only needs the name string,
and every message would come back `NotRegistered` on the other side.

The second call fixes that without naming a module: it repeats the scan for
`<app>/scheduled.py` across `INSTALLED_APPS`, an app with no such module is
skipped, and the tasks keep the `tasks.scheduled.*` names the schedule spells.
Both calls are deferred onto the same signal, so ordering is Beat-time, not
import-time, and `CELERY_IMPORTS`/`CELERY_INCLUDE` stay empty — nothing in
configuration has to be edited when a task module is added to an app that
follows either convention.

### The `DJANGO_SETTINGS_MODULE` default is what makes the fixup engage

`celery -A vaultshield_backend` starts in a bare Python process with no
Django environment, so the `setdefault` below is the only thing that points
it at the settings module — the same line `manage.py`, `wsgi.py`, and
`asgi.py` each carry. It also has to run *before* `Celery(...)`: Celery
decides whether to install its Django fixup by looking for
`DJANGO_SETTINGS_MODULE` in the environment at app-construction time, and
that fixup is what supplies `autodiscover_tasks()`'s package list from
`INSTALLED_APPS` and calls `django.setup()` before task modules are
imported.

`setdefault` and not an assignment: `pytest` (via `pyproject.toml`'s
`DJANGO_SETTINGS_MODULE = "vaultshield_backend.settings_test"`) and any
deployment that exports its own value must win over the development
default.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vaultshield_backend.settings")

# Named for the project package so the worker's banner, the default queue,
# and `-A vaultshield_backend` all say the same thing.
app = Celery("vaultshield_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
# `<app>/tasks.py` — `security_scan.tasks`.
app.autodiscover_tasks()
# `<app>/scheduled.py` — `tasks.scheduled`, the two Celery Beat tasks.
app.autodiscover_tasks(related_name="scheduled")
