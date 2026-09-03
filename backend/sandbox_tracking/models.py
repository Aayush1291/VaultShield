"""Sandbox lifecycle tracking.

design.md's "Backend apps" layout gives this app the `SandboxRecord`
model: a `run` foreign key, a `status` field, a `last_heartbeat_at`
timestamp, and a nullable `torn_down_at` (Requirement 11.5). It is the
eighth of Requirement 11.1's persisted models and the last one registered
in the admin (Requirement 11.3).

### The model exists for the reaper, not for the CLI

Nothing in the review pipeline reads a `SandboxRecord`. Its only consumer
is `cleanup_orphaned_sandboxes` (Task 33.1, Requirement 25.1), the Celery
Beat task that finds Sandboxes whose Run has finished but whose container
was never torn down — a `vaultshield run` killed mid-flight, a crashed
CLI, a developer who closed the laptop. That task's query is what fixes
every field's shape here:

    SandboxRecord.objects.filter(
        status="active", last_heartbeat_at__lt=cutoff
    ).exclude(run__status__in=ACTIVE_RUN_STATUSES)

`status` is the filter, `last_heartbeat_at` is the age test, `run` is the
join, and `torn_down_at` is what the update writes. A row is a claim that
a container may still be alive; the reaper's job is to disprove it.

This app is separate from `runs/` for the same reason `security_scan/` is:
`runs/` owns what the CLI uploaded for review, while a Sandbox is
infrastructure state with its own lifecycle that outlives — and can be
stranded by — the Run it was created for.
"""

from django.db import models

from runs.models import Run


class SandboxRecord(models.Model):
    """One tracked Sandbox container lifecycle for a Run.

    Created when the Sandbox_Module starts a container and closed out
    either by `vaultshield teardown` or, when that never happens, by
    `cleanup_orphaned_sandboxes` (Requirement 25.1).
    """

    # Deliberately narrower than `Run.STATUS_CHOICES`: a Sandbox is either
    # believed to be alive or known to be gone. As with `Run`, the display
    # half repeats the stored value — these are the literal strings
    # `cleanup_orphaned_sandboxes` filters and writes, so a prettified
    # label here would be a second source of truth for the same state.
    STATUS_CHOICES = [
        ("active", "active"),
        ("torn_down", "torn_down"),
    ]

    # `ForeignKey`, not `OneToOneField`, and the reverse accessor is plural:
    # a single Run can go through more than one container (a retried or
    # rebuilt Sandbox), and each attempt is separately reapable. CASCADE
    # matches every other relation to `Run` — deleting a Run leaves no
    # orphaned tracking rows behind.
    run = models.ForeignKey(
        Run, on_delete=models.CASCADE, related_name="sandbox_records"
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="active")
    # `auto_now_add`, emphatically not `auto_now`. This is the TTL clock the
    # reaper selects on, so it must only advance on a real heartbeat: with
    # `auto_now` any unrelated `save()` would refresh it and a stranded
    # Sandbox could stay just-seen forever. Set at creation, then bumped by
    # explicit assignment when a heartbeat arrives.
    last_heartbeat_at = models.DateTimeField(auto_now_add=True)
    # NULL for as long as the Sandbox is believed to be alive — the audit
    # companion to `status="torn_down"`, recording when teardown happened
    # rather than merely that it did. `blank=True` so the admin form
    # (Requirement 11.3) accepts an active record with the field empty.
    torn_down_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"sandbox for {self.run.run_id} ({self.status})"
