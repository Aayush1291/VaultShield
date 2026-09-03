"""Tests for the `tasks` app.

Task 33.1's unit tests for `cleanup_orphaned_sandboxes` come first, then
Task 33.2's for `flag_stuck_approvals`. Task 33.3 adds Property 34 to this
file — the property asserting scheduled cleanup affects exactly the
qualifying `SandboxRecord` and `Run` rows and nothing else.

### These touch the database, because the tasks are queries

Each task is a single conditional `UPDATE` whose whole behaviour is *which
rows it matched*. Substituting the queryset would leave nothing under test,
so every database-touching class here is `pytest.mark.django_db` and reads
the rows back. `vaultshield_backend/settings_test.py` runs them on in-memory
SQLite, so no PostgreSQL and no Redis are needed — and no Celery app either,
since a `shared_task` is called directly here rather than enqueued.

Row ages are set with a queryset `.update()` rather than by assigning to the
instance: `SandboxRecord.last_heartbeat_at` is `auto_now_add` and
`Run.updated_at` is `auto_now`, so an assignment would be overwritten on
`save()` by the very field behaviour these tests are trying to work around.
Going through the column directly is also the route production takes —
`apply_decision` writes `updated_at` in its own `UPDATE` — so no field
default gets to decide a value a test is pinning.
"""

import itertools
from datetime import timedelta

import pytest
from django.utils import timezone

from projects.models import Project
from runs.models import Run
from sandbox_tracking.models import SandboxRecord
from tasks.scheduled import (
    ACTIVE_RUN_STATUSES,
    DEFAULT_APPROVAL_MAX_AGE_HOURS,
    DEFAULT_SANDBOX_TTL_MINUTES,
    cleanup_orphaned_sandboxes,
    flag_stuck_approvals,
)

# Every Run_Status from Requirement 11.2, split by whether a Sandbox is still
# expected to be alive. Derived from the model's own choices so a new status
# cannot be added without one of these lists growing to match.
ALL_RUN_STATUSES = [value for value, _label in Run.STATUS_CHOICES]
FINISHED_RUN_STATUSES = [s for s in ALL_RUN_STATUSES if s not in ACTIVE_RUN_STATUSES]
# Requirement 25.2 names one status; the rest are what `flag_stuck_approvals`
# has to leave alone. Derived the same way, for the same reason.
UNFLAGGABLE_RUN_STATUSES = [s for s in ALL_RUN_STATUSES if s != "approved"]

TTL_MINUTES = 30
MAX_AGE_HOURS = 24


def _saved_run(status: str, run_id: str = "20240117-093000-0123456789abcdef") -> Run:
    """A persisted `Run` (with its `Project`) in `status`."""
    project = Project.objects.create(name="demo", repo_url="https://example.com/x.git")

    return Run.objects.create(project=project, run_id=run_id, agent="mock", status=status)


def _sandbox_record(run: Run, *, heartbeat_age_minutes: float, status: str = "active"):
    """A `SandboxRecord` for `run` whose heartbeat is that many minutes old."""
    record = SandboxRecord.objects.create(run=run, status=status)
    SandboxRecord.objects.filter(pk=record.pk).update(
        last_heartbeat_at=timezone.now() - timedelta(minutes=heartbeat_age_minutes)
    )

    return record


def _reloaded(record: SandboxRecord) -> SandboxRecord:
    return SandboxRecord.objects.get(pk=record.pk)


class TestCleanupOrphanedSandboxesIsACeleryTask:
    """The "WHILE Celery Beat is running" half of Requirement 25.1 needs a
    task Beat can name and enqueue. Database-free, and importable with no
    Celery app constructed — `shared_task` resolves one lazily.
    """

    def test_it_is_registered_under_the_name_beats_schedule_must_spell(self):
        """Task 33.4's `CELERY_BEAT_SCHEDULE` entry references the task by
        this string, so the module path is part of the contract."""
        assert cleanup_orphaned_sandboxes.name == "tasks.scheduled.cleanup_orphaned_sandboxes"

    def test_it_can_be_enqueued_rather_than_only_called(self):
        assert callable(getattr(cleanup_orphaned_sandboxes, "delay", None))

    def test_the_default_ttl_is_the_documented_thirty_minutes(self):
        assert DEFAULT_SANDBOX_TTL_MINUTES == 30


@pytest.mark.django_db
class TestCleanupOrphanedSandboxesReapsQualifyingRecords:
    """Requirement 25.1: a finished Run plus a stale heartbeat is reapable."""

    def test_a_stale_record_of_a_finished_run_is_torn_down(self):
        run = _saved_run("awaiting_review")
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 1

        reaped = _reloaded(record)
        assert reaped.status == "torn_down"
        assert reaped.torn_down_at is not None

    @pytest.mark.parametrize("run_status", FINISHED_RUN_STATUSES)
    def test_every_non_active_run_status_qualifies(self, run_status):
        """No CLI command needs the container once the agent has stopped, so
        each of these is a state in which an `active` record is stale."""
        run = _saved_run(run_status)
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 1
        assert _reloaded(record).status == "torn_down"

    def test_torn_down_at_records_when_teardown_happened(self):
        """The audit companion to the status, per Requirement 11.5 — it has to
        be the time of this pass, not the record's creation or heartbeat."""
        run = _saved_run("rejected")
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)

        before = timezone.now()
        cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES)
        after = timezone.now()

        assert before <= _reloaded(record).torn_down_at <= after

    def test_several_qualifying_records_are_all_reaped_and_counted(self):
        """A Run can go through more than one container (`SandboxRecord.run`
        is a plural `ForeignKey`), and a pass reaps whatever qualifies."""
        run = _saved_run("submitted")
        records = [
            _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + minutes)
            for minutes in (1, 60, 60 * 24)
        ]

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == len(records)
        assert {_reloaded(record).status for record in records} == {"torn_down"}


@pytest.mark.django_db
class TestCleanupOrphanedSandboxesLeavesEverythingElseAlone:
    """The other half of Requirement 25.1: both conditions have to hold.

    Property 34 (Task 33.3) generalises these over generated ages and
    statuses; the cases here are the individual reasons a record is spared.
    """

    @pytest.mark.parametrize("run_status", sorted(ACTIVE_RUN_STATUSES))
    def test_a_record_of_a_still_active_run_is_spared(self, run_status):
        """However stale the heartbeat: the agent may still be working in
        that container, and nothing bumps `last_heartbeat_at` yet."""
        run = _saved_run(run_status)
        record = _sandbox_record(run, heartbeat_age_minutes=60 * 24)

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 0
        assert _reloaded(record).status == "active"
        assert _reloaded(record).torn_down_at is None

    def test_a_fresh_heartbeat_spares_a_finished_runs_record(self):
        run = _saved_run("awaiting_review")
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES - 1)

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 0
        assert _reloaded(record).status == "active"

    def test_an_already_torn_down_record_is_not_restamped(self):
        """`status="active"` is in the filter, so a second pass is a no-op
        rather than a rewrite of an earlier pass's `torn_down_at`."""
        run = _saved_run("rejected")
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)
        cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES)
        first_pass_stamp = _reloaded(record).torn_down_at

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 0
        assert _reloaded(record).torn_down_at == first_pass_stamp

    def test_no_record_is_deleted(self):
        """Reaping closes a record out; the row is the audit trail."""
        reapable = _sandbox_record(
            _saved_run("submitted", "20240118-101500-fedcba9876543210"),
            heartbeat_age_minutes=TTL_MINUTES + 1,
        )
        spared = _sandbox_record(
            _saved_run("running", "20240119-111500-abcdef0123456789"),
            heartbeat_age_minutes=TTL_MINUTES + 1,
        )

        cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES)

        assert set(SandboxRecord.objects.values_list("pk", flat=True)) == {
            reapable.pk,
            spared.pk,
        }

    def test_the_associated_run_is_not_modified(self):
        """This task owns `SandboxRecord` rows only — a Run's `status` and
        `stuck` flag belong to review and to `flag_stuck_approvals`."""
        run = _saved_run("approved")
        _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)

        cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES)

        run.refresh_from_db()
        assert run.status == "approved"
        assert run.stuck is False

    def test_a_larger_ttl_reaps_no_more_than_a_smaller_one(self):
        """The TTL is an age threshold, so raising it can only spare rows."""
        run = _saved_run("rejected")
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES * 10) == 0
        assert _reloaded(record).status == "active"

        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 1

    def test_nothing_to_reap_returns_zero(self):
        assert cleanup_orphaned_sandboxes(ttl_minutes=TTL_MINUTES) == 0


# Run_Directory identifiers are unique per Run (`Run.run_id`), and several of
# the tests below need more than one Run, so ids come from a counter rather
# than from a literal each caller has to remember to vary.
_run_id_numbers = itertools.count()


def _aged_run(
    *,
    age_hours: float,
    status: str = "approved",
    pr_url: str = "",
    stuck: bool = False,
) -> Run:
    """A persisted Run whose `updated_at` is `age_hours` old.

    Defaults describe a Run that qualifies for flagging, so each test varies
    only the one attribute it is about.
    """
    project = Project.objects.create(name="demo", repo_url="https://example.com/x.git")
    run = Run.objects.create(
        project=project,
        run_id=f"20240117-093000-{next(_run_id_numbers):016d}",
        agent="mock",
        status=status,
        pr_url=pr_url,
        stuck=stuck,
    )
    Run.objects.filter(pk=run.pk).update(
        updated_at=timezone.now() - timedelta(hours=age_hours)
    )

    return run


def _is_stuck(run: Run) -> bool:
    return Run.objects.get(pk=run.pk).stuck


class TestFlagStuckApprovalsIsACeleryTask:
    """The "WHILE Celery Beat is running" half of Requirement 25.2, same as
    its sibling: a task Beat can name and enqueue, importable with no Celery
    app constructed.
    """

    def test_it_is_registered_under_the_name_beats_schedule_must_spell(self):
        assert flag_stuck_approvals.name == "tasks.scheduled.flag_stuck_approvals"

    def test_it_can_be_enqueued_rather_than_only_called(self):
        assert callable(getattr(flag_stuck_approvals, "delay", None))

    def test_the_default_max_age_is_the_documented_twenty_four_hours(self):
        assert DEFAULT_APPROVAL_MAX_AGE_HOURS == 24


@pytest.mark.django_db
class TestFlagStuckApprovalsFlagsQualifyingRuns:
    """Requirement 25.2: approved, no `pr_url`, and older than the threshold."""

    def test_an_old_approved_run_with_no_pr_is_flagged(self):
        run = _aged_run(age_hours=MAX_AGE_HOURS + 1)

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 1
        assert _is_stuck(run) is True

    def test_several_qualifying_runs_are_all_flagged_and_counted(self):
        runs = [
            _aged_run(age_hours=MAX_AGE_HOURS + hours) for hours in (1, 24, 24 * 30)
        ]

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == len(runs)
        assert all(_is_stuck(run) for run in runs)

    def test_flagging_does_not_move_the_updated_at_it_selects_on(self):
        """`updated_at` is this query's own input, and it measures how long
        the Run has been waiting. A queryset `UPDATE` skips `auto_now`, which
        is what keeps that age from being reset by the act of flagging."""
        run = _aged_run(age_hours=MAX_AGE_HOURS + 1)
        approved_at = Run.objects.get(pk=run.pk).updated_at

        flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS)

        assert Run.objects.get(pk=run.pk).updated_at == approved_at


@pytest.mark.django_db
class TestFlagStuckApprovalsLeavesEverythingElseAlone:
    """All three conditions have to hold. Property 34 (Task 33.3) generalises
    these over generated ages and statuses; each case here is one reason a
    Run is spared.
    """

    @pytest.mark.parametrize("run_status", UNFLAGGABLE_RUN_STATUSES)
    def test_a_run_in_any_other_status_is_spared(self, run_status):
        """However old: a Run that was never approved has nothing to submit,
        and one already marked `submitted` is a different discrepancy — the
        list endpoint's `?submitted=false` filter is what reports that."""
        run = _aged_run(age_hours=MAX_AGE_HOURS * 100, status=run_status)

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 0
        assert _is_stuck(run) is False

    def test_an_approved_run_that_has_a_pr_is_spared(self):
        run = _aged_run(
            age_hours=MAX_AGE_HOURS + 1, pr_url="https://example.com/org/repo/pull/7"
        )

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 0
        assert _is_stuck(run) is False

    def test_a_recently_approved_run_is_spared(self):
        run = _aged_run(age_hours=MAX_AGE_HOURS - 1)

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 0
        assert _is_stuck(run) is False

    def test_an_already_flagged_run_is_not_counted_again(self):
        """`stuck=False` is in the filter, so a second pass over the same
        backlog reports 0 rather than re-reporting yesterday's Runs."""
        run = _aged_run(age_hours=MAX_AGE_HOURS + 1)
        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 1

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 0
        assert _is_stuck(run) is True

    def test_only_the_stuck_field_of_a_flagged_run_changes(self):
        """This task raises a flag; it does not advance the lifecycle. Status
        and `pr_url` belong to review and to `vaultshield submit`."""
        run = _aged_run(age_hours=MAX_AGE_HOURS + 1)

        flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS)

        flagged = Run.objects.get(pk=run.pk)
        assert flagged.status == "approved"
        assert flagged.pr_url == ""
        assert flagged.created_at == run.created_at

    def test_no_run_is_deleted(self):
        flaggable = _aged_run(age_hours=MAX_AGE_HOURS + 1)
        spared = _aged_run(age_hours=MAX_AGE_HOURS + 1, status="rejected")

        flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS)

        assert set(Run.objects.values_list("pk", flat=True)) == {
            flaggable.pk,
            spared.pk,
        }

    def test_a_larger_max_age_flags_no_more_than_a_smaller_one(self):
        """The threshold is an age, so raising it can only spare Runs."""
        run = _aged_run(age_hours=MAX_AGE_HOURS + 1)

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS * 10) == 0
        assert _is_stuck(run) is False

        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 1

    def test_nothing_to_flag_returns_zero(self):
        assert flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS) == 0

    def test_no_sandbox_record_is_touched(self):
        """The two scheduled tasks own disjoint tables."""
        run = _aged_run(age_hours=MAX_AGE_HOURS + 1)
        record = _sandbox_record(run, heartbeat_age_minutes=TTL_MINUTES + 1)

        flag_stuck_approvals(max_age_hours=MAX_AGE_HOURS)

        assert _reloaded(record).status == "active"
        assert _reloaded(record).torn_down_at is None
