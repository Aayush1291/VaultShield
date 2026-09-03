"""Celery Beat periodic tasks.

design.md's "Backend apps" layout gives this app a single module,
`scheduled.py`, holding the two periodic tasks from Requirement 25:
`cleanup_orphaned_sandboxes` (Task 33.1) and `flag_stuck_approvals`
(Task 33.2). Task 33.4 registers both in `CELERY_BEAT_SCHEDULE`.

The app carries no models and no HTTP surface, so it has no `models.py`,
`views.py`, or `admin.py` — it is an installed app purely so its task
module is importable under the Django app registry and its schedule is
discoverable.
"""
