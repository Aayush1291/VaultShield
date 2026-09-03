"""The Celery task that turns a Run's uploaded diff into stored findings.

Task 22.3 — design.md's "Security_Scan_Service (`security_scan/`)"
section, which fixes the entry point:

    # security_scan/tasks.py
    @shared_task
    def run_security_scan(run_id: str) -> None:
        diff = RunDiff.objects.get(run__run_id=run_id)
        findings = scan_diff(diff)
        SecurityFinding.objects.bulk_create(findings)

Covers Requirement 17.1 (the rule-based checks run "when a Run_Diff is
uploaded") and Requirement 17.2 (each match becomes a stored
`Security_Finding`). `rules.py` decides *what* counts as a finding; this
module decides *when* a scan happens and *that* its results are persisted.
Requirement 17.3 is inherited: the only thing done with diff content here
is handing it to `scan_diff`.

The two halves of "automatically" split across tasks: Requirement 18.1's
enqueue call lives in the diff-ingest view (Task 23.2) and Requirement
18.2's broker/result-backend configuration in `vaultshield_backend/celery.py`
(Task 23.1). Neither exists yet, which shapes one decision below.

### `from celery import shared_task`, not a project app instance

`shared_task` registers the task against whichever Celery app is current,
resolving lazily on first use rather than at import. That is what keeps
this module importable — and testable — before Task 23.1 constructs the
app: `import security_scan.tasks` needs no broker URL, no result backend,
and no running Redis. Importing a concrete app from
`vaultshield_backend.celery` instead would make every `manage.py`
invocation that touches this app depend on configuration that has not been
written yet.

It is also the right long-term shape: `shared_task` is how a reusable
Django app declares a task, and Task 23.1's `autodiscover_tasks()` picks up
`<app>/tasks.py` by convention, so no registration edit is needed here when
the app lands.

### Addressed by `run_id`, not by primary key

The argument is the CLI-generated Run_Directory identifier
(Requirement 7.1), matching design.md's `RunDiff.objects.get(run__run_id=...)`
and the reason `Run.run_id` is `unique=True`: the CLI, the ingest views, and
this task all name a Run by the id the developer's machine minted, so
nothing has to hold a second identifier for the same Run. It also keeps the
enqueue site (Task 23.2) honest — it passes along the value it already has
from the URL.

### A missing diff is a no-op, not a failure

`RunDiff.DoesNotExist` is caught and the task returns. The normal enqueue
path cannot hit it (23.2 enqueues immediately after the diff is saved), so
this covers the cases where the world moved on: a Run torn down or deleted
between enqueue and execution, or a queued task replayed against a Run
whose diff is gone. There is nobody to return an error to — no request, no
reviewer waiting — and an uncaught `DoesNotExist` would be retried and fail
identically every time, so raising would trade a silent no-op for a noisy
permanent failure that fixes nothing. This mirrors how `scan_diff` skips
malformed file entries rather than raising for the same reason.

Note what is *not* swallowed: a database error, a programming error in the
rules, or any other exception still propagates and is recorded as a failed
task, because those are real faults a worker log should show.

### A scan replaces this Run's findings rather than adding to them

`bulk_create` alone would be wrong the second time a Run is scanned, and a
Run can genuinely be scanned twice: `RunDiff.run` is a `OneToOneField`, so
a repeat `POST /api/runs/:id/diff/` *replaces* the stored change set (the
view answers 200 rather than 201 to say so), and the Uploader's retry from
local artifacts (Requirement 14.4) makes that a documented flow rather than
a hypothetical. Each such POST enqueues another scan.

Appending would then leave a Run holding the union of both scans: exact
duplicate rows for anything still present, plus findings for lines the
replacement no longer contains. The dashboard's Security tab
(Requirement 20.5) would be listing findings against a diff that does not
contain them, next to the Diff tab that shows it does not — the worst
failure mode for a review aid, since the reviewer cannot tell which half is
real. Deleting this Run's findings first makes the stored set always
describe the change set currently stored for that Run, which is what
Requirement 17.2 is for.

The delete is scoped to `run=` and nothing else, so it can only ever remove
rows this task would have produced. It is in the same transaction as the
`bulk_create` so a Run is never left with its old findings dropped and no
new ones written.

The remaining race is benign and worth naming rather than hiding: two scans
of the same Run overlapping can finish in either order, so the findings
that survive are the last scan to *commit*, not necessarily the newest
diff. Closing that would mean locking the Run row for the whole scan, which
buys a review aid nothing — the ordinary path is sequential, since a client
only re-POSTs a diff after the previous response.
"""

from __future__ import annotations

from celery import shared_task
from django.db import transaction

from runs.models import RunDiff
from security_scan.models import SecurityFinding
from security_scan.rules import scan_diff


@shared_task
def run_security_scan(run_id: str) -> None:
    """Scan the stored diff of the Run named `run_id` and persist findings.

    Requirements 17.1 and 17.2. Applies every rule in the registry to the
    Run's change set and replaces that Run's `SecurityFinding` rows with the
    result, so the stored findings always describe the diff currently stored
    for the Run.

    Args:
        run_id: the CLI-generated `Run.run_id`, not a database pk.

    Returns:
        Nothing. Findings are the output, and they are read back off the Run
        (`run.security_findings`) by the run detail endpoint and the
        dashboard's Security tab; a return value would only be readable
        through the result backend, which nothing consumes.
    """
    try:
        # `select_related("run")`: `scan_diff` reads `run_diff.run` to stamp
        # every finding, and each returned finding is constructed with it,
        # so fetching the Run in this query is what keeps the whole scan at
        # one read.
        run_diff = RunDiff.objects.select_related("run").get(run__run_id=run_id)
    except RunDiff.DoesNotExist:
        # Nothing uploaded (or no longer uploaded) for this Run: no findings
        # to report, and nothing here can fix that by retrying.
        return

    # Outside the transaction on purpose: this is pure in-memory pattern
    # matching over already-fetched data, with no database work to keep a
    # transaction open for.
    findings = scan_diff(run_diff)

    with transaction.atomic():
        SecurityFinding.objects.filter(run=run_diff.run).delete()
        # Preserves the order `scan_diff` returned — file, then line, then
        # registry order — which is therefore also the order the Security
        # tab reads them back in.
        SecurityFinding.objects.bulk_create(findings)
