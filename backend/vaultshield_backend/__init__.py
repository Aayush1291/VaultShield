"""VaultShield backend project package.

Re-exports the Celery app (Task 23.1) so it is constructed whenever Django
is, which is what `@shared_task` depends on: `security_scan.tasks` and
`tasks.scheduled` declare their tasks against the *current* app rather than
importing a concrete one, and that current app only exists once something
has instantiated it. Importing it here — the one module guaranteed to load
before any app module — is what makes `run_security_scan.delay(...)` from a
Django view (Task 23.2) reach the configured broker instead of a default
app with no configuration.

It is also how `celery -A vaultshield_backend` finds the app: the `-A`
argument is a package, and Celery looks for a `celery_app` attribute on it.
"""

from vaultshield_backend.celery import app as celery_app

__all__ = ("celery_app",)
