"""The Celery Beat periodic tasks: the sandbox reaper and the stuck-approval flag.

Task 33.1 — design.md's "Celery Beat scheduled tasks (`tasks/scheduled.py`)"
section, which fixes this task's signature, query, and return value:

    @shared_task
    def cleanup_orphaned_sandboxes(ttl_minutes: int = 30) -> int:
        cutoff = timezone.now() - timedelta(minutes=ttl_minutes)
        qs = SandboxRecord.objects.filter(
            status="active",
            last_heartbeat_at__lt=cutoff,
        ).exclude(run__status__in=ACTIVE_RUN_STATUSES)
        count = qs.update(status="torn_down", torn_down_at=timezone.now())
        return count

Covers Requirement 25.1. `flag_stuck_approvals` (Task 33.2, Requirement
25.2) is below it in this same module, and Task 33.4 registers both in
`CELERY_BEAT_SCHEDULE` — the "WHILE Celery Beat is running on a configured
schedule" half of both requirements. Nothing here names a schedule, so this
module stays callable from a test, from `manage.py shell`, and from Beat with
one behaviour.

The two tasks share a shape and touch disjoint tables: each is one
conditional `UPDATE` whose filter is its requirement written out, each
returns a count of rows actually changed, and each is therefore idempotent.
The reaper owns `SandboxRecord.status`; the flag owns `Run.stuck`. Neither
writes the other's table.

### `from celery import shared_task`, for the same reason `security_scan` does

`shared_task` binds to whichever Celery app is current at call time rather
than importing `vaultshield_backend.celery.app` here, which keeps this
module importable with no broker URL and no running Redis — and keeps its
registered name (`tasks.scheduled.cleanup_orphaned_sandboxes`) the one
`autodiscover_tasks()` produces, which is the name Task 33.4's schedule
entry has to spell.

Note the one difference from `security_scan/tasks.py`: `autodiscover_tasks()`
imports `<app>/tasks.py` by convention, and this app's task module is
`scheduled.py`, so the default scan looks for `tasks.tasks` and finds nothing.
`vaultshield_backend/celery.py` therefore runs a second scan with
`related_name="scheduled"` (Task 33.4). Beat's schedule entry alone would not
be enough — a schedule entry carries a name, not an import, so the worker
receiving the message is where an unregistered task fails.

### What "tears down the underlying Sandbox" can mean from inside the backend

design.md's docstring says the task "tears down the underlying Sandbox (if
still present)", and its code is a single `UPDATE`. That is not a gap in the
snippet — it is where the backend's reach ends. Sandboxes are Docker
containers on the *developer's* machine: design.md's compose topology keeps
the CLI off the compose network specifically "so it can reach the host's
Docker socket", and a `celery-beat` container has no route to that socket.
`Sandbox.teardown` (`cli/vaultshield/sandbox.py`) is the only code that
removes a container, reached either by the `vaultshield run` that created it
or by `vaultshield teardown <run-id>` looking it up by name (Requirement
23.1, which is unconditional and needs no help from here).

So what this task owns is the record: a `SandboxRecord` with
`status="active"` is a *claim* that a container may still be alive, and the
reaper's job is to close out claims that can no longer be true — a Run that
has stopped executing and a heartbeat that stopped arriving. Writing a
Docker call into this module would be inventing a connection the deployment
does not have, and swallowing its inevitable failure would make the row's
`status` mean less than it does now.

### Both conditions are required, and each rules out a different false positive

Requirement 25.1 selects on the Run's state *and* on heartbeat age, so
neither alone reaps a row:

- **Age alone** would reap the record of a container that is legitimately in
  use. `last_heartbeat_at` is `auto_now_add` and nothing yet bumps it (there
  is no heartbeat endpoint in the MVP), so in practice every record older
  than the TTL passes the age test — which makes the status check the load-
  bearing guard rather than a formality.
- **Run state alone** would reap a record the moment its Run left `running`,
  including the seconds between a Run being marked up for review and its
  `vaultshield run` finishing its own teardown.

`ACTIVE_RUN_STATUSES` is the design's `(i.e. the associated Run is no longer
running)` written out: a Sandbox is expected to exist only while the agent is
executing in it. After that, no CLI command needs the container —
`vaultshield submit` works from the host checkout and its
Repository_Host_Provider credential, `vaultshield teardown` removes a
container by name whatever the Run's status, and the dashboard reads
uploaded rows — so `awaiting_review`, `approved`, `rejected`,
`submitted`, and `failed` are all states in which a still-`active` record
is stale bookkeeping.

The consequence worth naming: a Run whose CLI was killed mid-flight keeps
`status="running"` forever, so this task will never close its record out.
That follows from Requirement 25.1 rather than from a choice made here, and
it fails in the safe direction — the alternative marks a long-running Run's
live container as torn down. `vaultshield teardown <run-id>` is the
unconditional, host-side answer for that case, and it is the only one that
can actually remove the container. An error the CLI survives, by contrast,
now ends in `failed` (a best-effort `PATCH status=failed`), which this task
does reap.
"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from runs.models import Run
from sandbox_tracking.models import SandboxRecord
from vaultshield_backend.mongodb import to_mongo_precision

# The Run_Status values during which a Sandbox is *supposed* to be alive, and
# therefore the ones whose `SandboxRecord` rows the reaper leaves alone. A
# module-level frozenset rather than an inline literal: it is the definition
# of "active" that Requirement 25.1 refers to and that Task 33.3's property
# test has to share, so there is one place it is written down.
ACTIVE_RUN_STATUSES = frozenset({"running"})

# design.md's default (`ttl_minutes: int = 30`), named so Task 33.4's
# schedule entry and any test can refer to the same number instead of
# repeating it. Thirty minutes is long enough that a slow agent run's
# container is not disowned while it works, and short enough that a stranded
# record does not sit in the admin's `active` list for a working day.
DEFAULT_SANDBOX_TTL_MINUTES = 30

# design.md's default for the other task (`max_age_hours: int = 24`), named
# for the same reason. A day is the threshold that makes `stuck` mean "this
# was approved and then nobody ran `vaultshield submit`" rather than "this
# was approved during a long meeting": approval and submission are two
# separate human actions in Requirement 22's flow, and the gap between them
# is legitimately hours.
DEFAULT_APPROVAL_MAX_AGE_HOURS = 24


@shared_task
def cleanup_orphaned_sandboxes(ttl_minutes: int = DEFAULT_SANDBOX_TTL_MINUTES) -> int:
    """Close out `SandboxRecord` rows whose Sandbox can no longer be alive.

    Requirement 25.1. Selects records still marked `active` whose Run has
    stopped executing (its status is not in `ACTIVE_RUN_STATUSES`) and whose
    `last_heartbeat_at` is older than `ttl_minutes`, and marks each
    `status="torn_down"` with `torn_down_at` set.

    Args:
        ttl_minutes: how stale a heartbeat has to be before its record is
            reapable. Beat passes the configured value (Task 33.4); the
            default is design.md's 30.

    Returns:
        The number of records torn down, as design.md specifies. It is a real
        count of rows changed, not of rows matched, which is what makes it
        usable as the worker log's answer to "did this pass do anything".

    ### One `now`, used for both the cutoff and the stamp

    design.md calls `timezone.now()` twice; this reads the clock once. The
    two uses are the same instant by intent — the cutoff is "TTL ago" and
    `torn_down_at` is "the moment this pass ran" — and a single value keeps
    `torn_down_at` from drifting past the cutoff it was selected against.

    ### Two queries on MongoDB, and the guard is still in the write

    Under PostgreSQL this was one statement: `.exclude(run__status__in=...)`
    joined to the runs table and Django resolved the join into a pk subquery
    inside the `UPDATE`. MongoDB cannot join inside an update at all, and
    `django-mongodb-backend` refuses it outright rather than silently doing
    something else:

        NotSupportedError: Cannot use QuerySet.update() when querying across
        multiple collections on MongoDB.

    So the join is lifted out. The active Runs' primary keys are read first,
    then the update filters on `run_id` — the foreign-key *column on
    SandboxRecord itself*, so the second query touches one collection and is a
    legal MongoDB update. `exclude(run_id__in=...)` is not a join; `run_id` is
    a local field.

    What that preserves: `status="active"` is still evaluated *by the write*,
    and MongoDB applies a multi-document update atomically per document. Two
    overlapping passes therefore still cannot both count the same record, and
    neither can overwrite a `torn_down_at` an earlier pass already wrote. Run
    twice with nothing new to reap and the second call still returns 0 rather
    than restamping yesterday's records. Idempotency is intact, and it is
    intact for the same reason as before — the guard is in the `UPDATE`, not
    in a preceding read.

    What it does not preserve: the *set of excluded Runs* is now a snapshot
    taken microseconds before the write instead of being evaluated inside it.
    A Run that moved into `running` in that window would have its record
    reaped anyway. This is the one guarantee the migration genuinely narrows,
    so it is stated plainly rather than glossed:

    - It is not reachable through VaultShield's own lifecycle. `Run.status`
      advances `running -> awaiting_review | failed -> approved/rejected -> submitted`;
      nothing moves a Run *back* to `running`, and only a hand-edit or a
      `PATCH /api/runs/:id/` could. A record also has to be `ttl_minutes`
      stale to be a candidate at all, so a Run actively heartbeating is not
      in the candidate set regardless.
    - The consequence is bounded: a `SandboxRecord` is marked `torn_down`
      early. No Run is modified and no data is deleted, so the damage is one
      row's bookkeeping, recoverable by the CLI re-registering the sandbox.
    - `vaultshield_backend/test_mongodb.py::TestSandboxCleanup` covers the behaviour that is
      preserved (idempotency, no collateral writes, the exclusion itself) and
      documents this window explicitly.

    Nothing outside the matched rows is touched — no Run is modified, and no
    record is deleted — which is the half of Property 34 (Task 33.3) that
    this task is responsible for.
    """
    # Truncated to MongoDB's millisecond storage precision so `torn_down_at`
    # reads back exactly as written. Without it, a caller comparing this pass's
    # wall clock against the stored stamp sees the stamp as up to 999µs
    # *earlier* than the instant the pass began. See
    # `vaultshield_backend/mongodb.py`.
    now = to_mongo_precision(timezone.now())
    cutoff = now - timedelta(minutes=ttl_minutes)

    # Step 1: the Runs whose Sandboxes are supposed to still be alive. Read as
    # a list of pks so step 2 filters on SandboxRecord's own `run_id` column
    # rather than joining. `values_list` keeps this to one field over the wire.
    active_run_pks = list(
        Run.objects.filter(status__in=ACTIVE_RUN_STATUSES).values_list("pk", flat=True)
    )

    # Step 2: one single-collection conditional update. `status="active"` is
    # what makes it idempotent and safe against an overlapping pass; the
    # `exclude` is on the local FK column, so nothing here spans collections.
    reapable = SandboxRecord.objects.filter(
        status="active",
        last_heartbeat_at__lt=cutoff,
    ).exclude(run_id__in=active_run_pks)

    return reapable.update(status="torn_down", torn_down_at=now)


@shared_task
def flag_stuck_approvals(max_age_hours: int = DEFAULT_APPROVAL_MAX_AGE_HOURS) -> int:
    """Mark approved Runs that were never submitted as `stuck`.

    Requirement 25.2. Selects Runs with `status="approved"` and no `pr_url`
    whose `updated_at` is older than `max_age_hours`, and sets `stuck=True`
    on each.

    Args:
        max_age_hours: how long a Run may sit approved-and-unsubmitted
            before it is flagged. Beat passes the configured value
            (Task 33.4); the default is design.md's 24.

    Returns:
        The number of Runs newly flagged. Rows already carrying `stuck=True`
        are excluded from the query, so this counts what *changed* on this
        pass rather than how many stuck Runs exist — which is what makes it
        useful in a worker log and what makes the task idempotent: a second
        pass over the same backlog returns 0.

    ### `updated_at` is the clock, and this write deliberately does not touch it

    A queryset `UPDATE` does not run `auto_now`, so `stuck` is set without
    `Run.updated_at` moving. That is load-bearing rather than incidental: the
    column is this query's own input, and bumping it would reset the age of
    every row the task just flagged — erasing how long the Run has actually
    been waiting, which is the only thing that makes the flag actionable to
    whoever reads it in the dashboard or admin.

    It is also why `apply_decision` (`runs/views.py`) passes `updated_at`
    explicitly when it writes `status="approved"`: the age measured here is
    time since the decision, not time since the Run was created, so a Run is
    never flagged for hours it spent in `awaiting_review`.

    ### All three conditions are required, and each excludes a real Run

    - **`status="approved"`** — Requirement 25.2's wording, and the only
      status where an absent `pr_url` is a problem. A `running` or
      `awaiting_review` Run has nothing to submit yet, a `rejected` one never
      will, and a `submitted` Run with an empty `pr_url` is a different
      defect: the list endpoint's `?submitted=false` filter reports it
      (`runs/views.py`), and flagging it here would file it under
      "nobody submitted this" when in fact somebody did and the URL never
      came back.
    - **`pr_url=""`** — the empty string, not NULL, because `Run.pr_url` is
      non-nullable with an empty default precisely so that "unsubmitted" has
      one representation. This is the same predicate the list endpoint's
      `submitted` filter uses, so the two cannot disagree about which Runs
      have a PR.
    - **`updated_at__lt=cutoff`** — strictly older than the threshold, so
      raising `max_age_hours` can only ever flag fewer Runs.

    Nothing outside `stuck` is written: no status is advanced, no Run is
    deleted, and no Run is ever *un*-flagged. Clearing the flag is not this
    task's job — a Run that gets submitted after being flagged leaves the
    query by both of the first two conditions, and no requirement asks for
    `stuck` to be reset. That, together with leaving non-qualifying Runs
    untouched, is the `Run` half of Property 34 (Task 33.3).
    """
    cutoff = timezone.now() - timedelta(hours=max_age_hours)

    # `stuck=False` in the filter, not just for the count: it keeps the
    # statement a no-op for rows already flagged, so two overlapping passes
    # (a slow pass overtaken by Beat's next tick, or a manual call alongside
    # a scheduled one) cannot both claim the same Run.
    stuck_approvals = Run.objects.filter(
        status="approved",
        pr_url="",
        updated_at__lt=cutoff,
        stuck=False,
    )

    return stuck_approvals.update(stuck=True)
