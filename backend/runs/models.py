"""Run and run-artifact persistence.

design.md's "Backend apps" layout puts five models here: `Run` plus the
four artifact tables the CLI uploads into it — `RunEvent`, `RunDiff`,
`Screenshot`, and `Approval` (Requirement 11.1). `SecurityFinding` lives
in `security_scan/models.py` and `SandboxRecord` in
`sandbox_tracking/models.py`, but both point back at `Run`, so `Run` is the
hub every other table in the schema hangs off.

### Cardinality is not uniform, and that is the point

`RunEvent`, `Screenshot`, and `SecurityFinding` are `ForeignKey` (many per
Run — a Run emits many events, screenshots one per configured route,
findings one per rule match). `RunDiff` and `Approval` are
`OneToOneField`: a Run has exactly one change set and at most one
approve/reject decision. The one-to-one on `Approval` is what makes
Requirement 21's state machine enforceable at the schema level — a second
decision on the same Run cannot be recorded, so the `awaiting_review` gate
in `apply_decision` (Task 28.1) cannot be bypassed by racing requests.

Every relation is `on_delete=CASCADE`: artifacts have no meaning without
their Run, and `vaultshield teardown` deleting a Run should not leave
orphaned events or screenshot rows behind.

### No repository-hosting token, anywhere

Requirement 22.5 and design.md's Security Considerations require that the
backend have "no field, model, or code path capable of persisting a
Repository_Host_Provider access token". `Run.pr_url` — a public URL — is
the only thing `vaultshield submit` ever writes back. The token that
opened that PR is resolved and used entirely on the developer's host
machine. Property 32 (Task 29.5) asserts this over the whole schema.
"""

from django.db import models

from projects.models import Project


class Run(models.Model):
    """One supervised agent execution against a Project.

    Created by the CLI through `POST /api/runs/` (Requirement 12.2), filled
    in by the artifact ingest endpoints (Requirement 13), moved through
    review by approve/reject (Requirement 21), and finished by
    `vaultshield submit` writing `pr_url` (Requirement 22.4), or marked
    `failed` by the CLI on any error after registration.
    """

    # Requirement 11.2: Run_Status is exactly these six values. The
    # display half of each pair intentionally repeats the stored value —
    # design.md specifies these as the literal Run_Status strings, and the
    # dashboard renders its own human-facing copy (Requirement 21.4's
    # "approved — awaiting submission"), so a prettified label here would
    # be a second, competing source of truth.
    #
    # `failed` is set by the CLI when a Run errors after it was registered
    # (`PATCH status=failed`, only from `running`; see
    # `runs.views.GATED_PATCH_TRANSITIONS`). It is terminal for the CLI: the
    # completion PATCH (`awaiting_review`, also gated to `running`) refuses a
    # `failed` Run. Only a hand-made, unrestricted `PATCH status=running`
    # could revive one.
    STATUS_CHOICES = [
        ("running", "running"),
        ("awaiting_review", "awaiting_review"),
        ("approved", "approved"),
        ("rejected", "rejected"),
        ("submitted", "submitted"),
        ("failed", "failed"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="runs")
    # The CLI-generated Run_Directory identifier (Requirement 7.1), not the
    # database primary key. `unique=True` because the CLI addresses runs by
    # it across process boundaries: `vaultshield submit <run-id>`,
    # `vaultshield teardown <run-id>`, and `run_security_scan(run_id)` all
    # look a Run up by this value rather than by pk.
    run_id = models.CharField(max_length=64, unique=True)
    # Which adapter produced this Run ("mock", "codex", "kiro",
    # "antigravity"). Left unconstrained by choices: the adapter registry
    # is a CLI-side concern, and pinning the set here would mean a schema
    # migration for every new adapter.
    agent = models.CharField(max_length=32)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default="running")
    # Requirement 12.6's `submitted` filter is derived from this field
    # being empty or non-empty, so "not yet submitted" must be the empty
    # string rather than NULL — otherwise `pr_url=""` and `pr_url=NULL`
    # would both mean unsubmitted and the filter would miss half the rows.
    pr_url = models.URLField(blank=True, default="")
    # Requirement 11.4. Set by `flag_stuck_approvals` (Task 33.2) on Runs
    # approved long ago that never got submitted; never set by the CLI.
    stuck = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    # `auto_now` matters beyond bookkeeping: `flag_stuck_approvals` selects
    # on `updated_at` age, so this field is the input to that query.
    updated_at = models.DateTimeField(auto_now=True)
    # The high-water mark for `RunEvent.sequence` on this Run, and the whole
    # of the MongoDB-native replacement for the `SELECT ... FOR UPDATE` +
    # `MAX(sequence)` read-modify-write that assigned sequences under
    # PostgreSQL (MongoDB has no row lock; see
    # `runs/sequences.allocate_event_sequences`).
    #
    # Never read or written through the ORM in the ingest path. It is advanced
    # by a single `find_one_and_update` with `$inc`, which MongoDB guarantees
    # is atomic on one document — so two concurrent batches for this Run get
    # disjoint blocks of numbers without either one locking anything.
    #
    # Deliberately absent from every serializer's `fields`: it is an internal
    # allocation cursor, not part of the API surface, and the value is
    # meaningless to a client. Bypassing the ORM also means advancing it does
    # not touch `updated_at`, which matches the PostgreSQL behaviour it
    # replaces — event ingest never bumped a Run's `updated_at`, and
    # `flag_stuck_approvals` selects on that column.
    next_event_sequence = models.PositiveIntegerField(default=0)
    # NOTE: intentionally no field of any kind stores a repo-hosting access
    # token. Only pr_url (a public URL) is ever written back by `submit`.

    class Meta:
        # Newest first for the dashboard's run history (Requirement 19.2)
        # and to give the paginated list endpoint a stable page split.
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.run_id} ({self.status})"


class RunEvent(models.Model):
    """One entry from a Run's Event_Log: terminal output or a file change.

    Uploaded in batches by the Uploader (Requirement 14.2) and rendered by
    the dashboard's Timeline tab (Requirement 20.3).
    """

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=32)  # "output" | "file_change"
    # No `auto_now_add`: this is the timestamp the Monitor recorded on the
    # developer's machine, which is what Requirement 6.3's timestamp-sorted
    # merge ordered the Event_Log by. An ingest-time timestamp would
    # reorder the log by upload batching instead.
    timestamp = models.DateTimeField()
    payload = models.TextField()
    sequence = models.PositiveIntegerField()  # preserves original batch order

    class Meta:
        # Requirement 13.1 / Property 22: bulk event ingest preserves the
        # submitted order. `sequence` is what carries that order, so it is
        # also what reads must come back in — two events sharing a
        # timestamp would otherwise come back in arbitrary order.
        ordering = ["sequence"]
        constraints = [
            # The database-level half of Requirement 13.1's ordering
            # guarantee, and new with the MongoDB migration.
            #
            # Under PostgreSQL, `sequence` uniqueness per Run was a
            # *consequence* of how it was assigned: `SELECT ... FOR UPDATE`
            # held a row lock across the read-modify-write, so no two batches
            # could compute the same starting point. MongoDB has no row lock,
            # so the allocator is an atomic `$inc` instead
            # (`runs/sequences.py`) — and this constraint is what makes the
            # guarantee independent of it.
            #
            # Stated as a real unique compound index rather than trusted to
            # the allocator, because "no duplicate sequence numbers for a
            # Run" is the property the API contract rests on: if the
            # allocator ever regressed, this turns a silently interleaved
            # Event_Log into a loud `IntegrityError` at ingest.
            models.UniqueConstraint(
                fields=["run", "sequence"], name="unique_run_event_sequence"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run.run_id} #{self.sequence} {self.event_type}"


class RunDiff(models.Model):
    """The structured per-file change set a Run produced.

    One row per Run, ingested at `POST /api/runs/:id/diff/`
    (Requirement 13.2), which also enqueues the security scan
    (Requirement 18.1).
    """

    run = models.OneToOneField(Run, on_delete=models.CASCADE, related_name="diff")
    # The CLI's `diff.json` shape, stored as-is rather than normalized into
    # a per-file table: nothing queries individual file rows. The scanner
    # (Task 22.1) and the dashboard's Diff tab both read the whole change
    # set at once, so a JSON column keeps the round trip exact.
    # [{"path", "additions", "deletions", "patch"?, "patch_truncated"?}, ...]
    files = models.JSONField()

    def __str__(self) -> str:
        return f"diff for {self.run.run_id}"


class Screenshot(models.Model):
    """A captured page image for one configured route of a Run.

    Uploaded at `POST /api/runs/:id/screenshots/` (Requirement 13.3) and
    rendered by the dashboard's Screenshots tab (Requirement 20.4).
    """

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="screenshots")
    route = models.CharField(max_length=255)
    # Files land under `MEDIA_ROOT/screenshots/`. `ImageField` over
    # `FileField` so Django validates that an uploaded artifact is actually
    # a decodable image — these come in over HTTP from the CLI, and a
    # corrupt upload should be rejected at ingest, not discovered when the
    # dashboard tries to render it. This is why Pillow is a backend
    # dependency.
    image = models.ImageField(upload_to="screenshots/")

    def __str__(self) -> str:
        return f"{self.run.run_id} {self.route}"


class Approval(models.Model):
    """The recorded approve/reject decision for a Run.

    Written by the approve/reject actions once a Run's status is
    `awaiting_review` (Requirements 21.1-21.3). `OneToOneField` makes a
    second decision on the same Run a database-level impossibility.
    """

    run = models.OneToOneField(Run, on_delete=models.CASCADE, related_name="approval")
    # A narrower set than Run.STATUS_CHOICES on purpose: a decision is only
    # ever "approved" or "rejected". `running`/`awaiting_review`/`submitted`
    # are Run lifecycle states, not decisions a reviewer can record.
    decision = models.CharField(
        max_length=16,
        choices=[("approved", "approved"), ("rejected", "rejected")],
    )
    decided_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.run.run_id} {self.decision}"
