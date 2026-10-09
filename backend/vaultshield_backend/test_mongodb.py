"""MongoDB integration tests: the guarantees the PostgreSQL migration replaced.

### What this file is for, and why it is separate

The rest of the suite tests VaultShield's behaviour and is largely
datastore-agnostic — serializer field surfaces, URL resolution, rule matching.
This file tests the *datastore*, and specifically the four correctness
mechanisms that could not survive the move from PostgreSQL unchanged:

| Mechanism                | PostgreSQL                        | MongoDB now                      |
|--------------------------|-----------------------------------|----------------------------------|
| Event sequence numbering | `SELECT ... FOR UPDATE` + `MAX()` | atomic `$inc` + unique index     |
| Atomicity                | `django.db.transaction.atomic`    | `django_mongodb_backend` atomic  |
| Scan-after-diff ordering | Django `on_commit`                | backend `on_commit`              |
| Sandbox reaper           | one join-filtered `UPDATE`        | two single-collection queries    |

Each of those is a place where the *old* code would still import, still run,
and still pass every pre-existing test while silently doing nothing —
`select_for_update()` is ignored on this backend rather than raising, and
`django.db.transaction.atomic()` is a no-op because the backend reports
`supports_transactions = False`. Tests that only assert happy-path behaviour
cannot tell the difference. So the tests here deliberately do two things the
rest of the suite does not: they run real threads, and they inject failures
mid-operation.

### Why failure injection rather than trusting the transaction

"Wrapped in `atomic()`" is not evidence of atomicity when the wrong `atomic()`
is a silent no-op. Every former transaction boundary therefore has a test that
raises an exception partway through and asserts the database is unchanged.
`TestTransactionPrimitives` additionally pins the *negative* result — that
Django's native API leaks — so that if a future edit reverts an import, a test
fails rather than the guarantee quietly disappearing.

### Requirements covered

- 13.1 / Property 22: bulk event ingest preserves submitted order
- 17.1, 17.2: security findings describe the diff currently stored
- 18.1: the scan is triggered once the diff has landed
- 21.1-21.3: approve/reject state machine, including concurrent decisions
- 12.2, 12.4, 12.5: the Run lifecycle's gated PATCH transitions
  (`awaiting_review`/`failed` only from `running`, including under
  contention), and `run_id` format and duplicate handling on create
- 25.1: the sandbox reaper and its idempotency
- 27.3-27.5: every `/api/` path requires a Dashboard_Auth_Token
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from bson import ObjectId
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, connections
from django.db.utils import DatabaseError, NotSupportedError
from pymongo.errors import OperationFailure
from django.utils import timezone
from django_mongodb_backend import transaction as mongo_transaction
from django.db import transaction as django_transaction
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework.validators import UniqueValidator

from projects.models import Project
from runs.models import Approval, Run, RunDiff, RunEvent
from runs.sequences import (
    SequenceAllocationError,
    allocate_event_sequences,
    highest_stored_sequence,
)
from runs.serializers import (
    RUN_ID_UNIQUE_MESSAGE,
    RunEventIngestSerializer,
    RunSerializer,
    RunUpdateSerializer,
)
from sandbox_tracking.models import SandboxRecord
from security_scan.models import SecurityFinding
from security_scan.tasks import run_security_scan
from tasks.scheduled import cleanup_orphaned_sandboxes, flag_stuck_approvals
from vaultshield_backend.mongodb import (
    TRANSIENT_TRANSACTION_ERROR_LABEL,
    is_transient_transaction_error,
    retry_on_transient_transaction_error,
    to_mongo_precision,
)

# --- helpers ---------------------------------------------------------------


def _project(name: str = "vaultshield") -> Project:
    return Project.objects.create(name=name, repo_url="https://example.invalid/repo.git")


def _run(project: Project | None = None, /, **overrides) -> Run:
    """A Run with every required field filled, `run_id` unique per call."""
    attributes = {
        "project": project or _project(),
        "run_id": f"20240101-000000-{ObjectId()}",
        "agent": "mock",
        "status": "running",
    }
    attributes.update(overrides)
    return Run.objects.create(**attributes)


def _event_payloads(count: int, *, prefix: str = "line") -> list[dict]:
    """`count` well-formed ingest payloads, in submitted order."""
    now = timezone.now()
    return [
        {
            "event_type": "output",
            "timestamp": (now + timedelta(milliseconds=index)).isoformat(),
            "payload": f"{prefix}-{index}",
        }
        for index in range(count)
    ]


def _ingest(run: Run, payloads: list[dict]) -> list[RunEvent]:
    """Drive the real ingest path: validate then save, as the view does."""
    serializer = RunEventIngestSerializer(data=payloads, many=True)
    serializer.is_valid(raise_exception=True)
    return serializer.save(run=run)


def _authenticated_client() -> tuple[APIClient, User, Token]:
    """An `APIClient` carrying a valid Dashboard_Auth_Token."""
    user = User.objects.create_user(username="reviewer", password="pw-not-a-secret")
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client, user, token


class _Injected(RuntimeError):
    """Marker for a deliberately injected mid-operation failure."""


# A `RunDiff.files` entry the scanner actually finds something in.
#
# `scan_diff` reads each entry's `patch` and matches its *added* lines, so a
# `{"path", "additions", "deletions"}` entry — the shape the ingest serializer
# validates — carries nothing to scan and yields no findings. Tests that assert
# findings exist therefore have to supply a patch, exactly as
# `security_scan/tests.py` does.
_RISKY_PATCH = "@@ -0,0 +1,1 @@\n+rm -rf /var/lib/data\n"


def _risky_files(path: str = "deploy.sh") -> list[dict]:
    return [{"path": path, "patch": _RISKY_PATCH}]


# --- 1. connection and configuration ---------------------------------------


@pytest.mark.django_db
class TestMongoDBConnection:
    """The suite is talking to MongoDB, and to nothing else."""

    def test_engine_is_the_mongodb_backend(self):
        assert connection.settings_dict["ENGINE"] == "django_mongodb_backend"

    def test_no_postgresql_alias_is_configured(self):
        """MongoDB is the sole system of record: one alias, and it is Mongo."""
        assert list(connections) == ["default"]
        for alias in connections:
            engine = connections[alias].settings_dict["ENGINE"]
            assert "postgres" not in engine, f"{alias} still points at PostgreSQL"
            assert "sqlite" not in engine, f"{alias} still points at SQLite"

    def test_server_is_reachable_and_reports_a_version(self):
        info = connection.database.client.admin.command("buildInfo")
        assert int(info["version"].split(".")[0]) >= 7, "MongoDB 7.0+ is required"

    def test_deployment_is_a_replica_set_or_sharded_cluster(self):
        """Multi-document transactions require one or the other.

        Asserted rather than assumed because a standalone `mongod` fails only
        the transaction tests, and this names the reason once instead of
        letting five unrelated failures imply it.
        """
        hello = connection.database.client.admin.command("hello")
        assert hello.get("setName") or hello.get("msg") == "isdbgrid", (
            "The configured deployment is standalone; MongoDB permits "
            "transactions only on a replica set or sharded cluster."
        )

    def test_collections_exist_for_every_vaultshield_model(self):
        present = set(connection.database.list_collection_names())
        for model in (Project, Run, RunEvent, RunDiff, Approval, SandboxRecord, SecurityFinding):
            assert model._meta.db_table in present, f"missing {model._meta.db_table}"


@pytest.mark.django_db
class TestObjectIdPrimaryKeys:
    """Every model uses MongoDB's ObjectId, not an integer auto key."""

    ALL_MODELS = (Project, Run, RunEvent, RunDiff, Approval, SandboxRecord, SecurityFinding)

    def test_no_model_uses_an_integer_auto_field(self):
        for model in self.ALL_MODELS + (User, Permission, ContentType):
            pk_type = type(model._meta.pk).__name__
            assert pk_type != "AutoField", f"{model.__name__} uses AutoField"
            assert pk_type != "BigAutoField", f"{model.__name__} uses BigAutoField"

    def test_saved_primary_keys_are_objectids(self):
        run = _run()
        assert isinstance(run.pk, ObjectId)
        assert isinstance(run.project.pk, ObjectId)

    def test_primary_key_round_trips_through_the_database(self):
        run = _run()
        assert Run.objects.get(pk=run.pk).pk == run.pk


# --- 2. CRUD and relationships ---------------------------------------------


@pytest.mark.django_db
class TestCrudOperations:
    """Create, read, update, delete against MongoDB for the core models."""

    def test_create_and_read_back_every_field(self):
        project = _project("crud")
        run = _run(project, agent="codex", status="awaiting_review")

        stored = Run.objects.get(pk=run.pk)

        assert stored.project_id == project.pk
        assert stored.agent == "codex"
        assert stored.status == "awaiting_review"
        assert stored.pr_url == ""
        assert stored.stuck is False

    def test_update_persists(self):
        run = _run()
        run.status = "approved"
        run.pr_url = "https://example.invalid/pr/1"
        run.save()

        stored = Run.objects.get(pk=run.pk)
        assert stored.status == "approved"
        assert stored.pr_url == "https://example.invalid/pr/1"

    def test_queryset_update_persists(self):
        """The bulk path, which the state machine and both Beat tasks use."""
        run = _run()
        changed = Run.objects.filter(pk=run.pk).update(status="submitted")

        assert changed == 1
        assert Run.objects.get(pk=run.pk).status == "submitted"

    def test_delete_removes_the_document(self):
        run = _run()
        Run.objects.filter(pk=run.pk).delete()

        assert not Run.objects.filter(pk=run.pk).exists()

    def test_jsonfield_round_trips_exactly(self):
        """`RunDiff.files` carries the CLI's `diff.json` verbatim."""
        run = _run()
        files = [
            {"path": "a/b.py", "additions": 12, "deletions": 3},
            {"path": "c.md", "additions": 0, "deletions": 7},
        ]
        RunDiff.objects.create(run=run, files=files)

        assert RunDiff.objects.get(run=run).files == files

    def test_bulk_create_returns_instances_with_primary_keys_in_order(self):
        """Both properties are relied on: the event response body echoes the
        returned instances, in order."""
        run = _run()
        created = RunEvent.objects.bulk_create(
            [
                RunEvent(
                    run=run,
                    event_type="output",
                    timestamp=timezone.now(),
                    payload=f"p{index}",
                    sequence=index,
                )
                for index in range(5)
            ]
        )

        assert [event.sequence for event in created] == [0, 1, 2, 3, 4]
        assert all(isinstance(event.pk, ObjectId) for event in created)


@pytest.mark.django_db
class TestRelationships:
    """Foreign keys, one-to-ones, reverse traversal, and cascade deletes."""

    def test_forward_foreign_key_traversal(self):
        project = _project("fk")
        run = _run(project)

        assert Run.objects.get(pk=run.pk).project.name == "fk"

    def test_reverse_foreign_key_traversal(self):
        project = _project("reverse")
        _run(project)
        _run(project)

        assert project.runs.count() == 2

    def test_reverse_one_to_one_traversal(self):
        run = _run(status="awaiting_review")
        Approval.objects.create(run=run, decision="approved")

        assert Run.objects.get(pk=run.pk).approval.decision == "approved"

    def test_missing_reverse_one_to_one_raises_does_not_exist(self):
        """What `RunDetailSerializer` relies on to render `"diff": null`."""
        run = _run()

        with pytest.raises(RunDiff.DoesNotExist):
            _ = Run.objects.get(pk=run.pk).diff

    def test_filtering_across_a_foreign_key(self):
        """`run__run_id` is how `run_security_scan` resolves a diff."""
        run = _run()
        RunDiff.objects.create(run=run, files=[])

        assert RunDiff.objects.get(run__run_id=run.run_id).run_id == run.pk

    def test_cascade_delete_removes_every_dependent_document(self):
        run = _run()
        RunEvent.objects.create(
            run=run, event_type="output", timestamp=timezone.now(), payload="x", sequence=0
        )
        RunDiff.objects.create(run=run, files=[])
        Approval.objects.create(run=run, decision="approved")
        SandboxRecord.objects.create(run=run)
        SecurityFinding.objects.create(
            run=run, rule_id="r", file_path="f", line_number=1, description="d"
        )

        run.delete()

        assert RunEvent.objects.filter(run_id=run.pk).count() == 0
        assert RunDiff.objects.filter(run_id=run.pk).count() == 0
        assert Approval.objects.filter(run_id=run.pk).count() == 0
        assert SandboxRecord.objects.filter(run_id=run.pk).count() == 0
        assert SecurityFinding.objects.filter(run_id=run.pk).count() == 0


@pytest.mark.django_db
class TestUniqueConstraints:
    """Uniqueness is enforced by real MongoDB unique indexes, not by Python."""

    def test_run_id_is_unique(self):
        run = _run()

        with pytest.raises(IntegrityError):
            Run.objects.create(
                project=run.project, run_id=run.run_id, agent="mock", status="running"
            )

    def test_one_diff_per_run(self):
        """`RunDiff.run` is a `OneToOneField`; the upsert in ingest depends on
        a second insert being refused."""
        run = _run()
        RunDiff.objects.create(run=run, files=[])

        with pytest.raises(IntegrityError):
            RunDiff.objects.create(run=run, files=[])

    def test_one_approval_per_run(self):
        """The schema-level statement that a Run is decided at most once —
        what `apply_decision`'s 409 path exists to convert into an error a
        reviewer can read."""
        run = _run(status="awaiting_review")
        Approval.objects.create(run=run, decision="approved")

        with pytest.raises(IntegrityError):
            Approval.objects.create(run=run, decision="rejected")

    def test_event_sequence_is_unique_per_run(self):
        """The database-level half of Requirement 13.1, new with MongoDB.

        Under PostgreSQL this was implied by the row lock. Here it is an
        explicit unique compound index, so a duplicate is refused even if the
        allocator regressed.
        """
        run = _run()
        RunEvent.objects.create(
            run=run, event_type="output", timestamp=timezone.now(), payload="a", sequence=0
        )

        with pytest.raises(IntegrityError):
            RunEvent.objects.create(
                run=run, event_type="output", timestamp=timezone.now(), payload="b", sequence=0
            )

    def test_the_same_sequence_is_allowed_on_a_different_run(self):
        """The constraint is per Run, so two Runs both start at 0."""
        first, second = _run(), _run()
        for run in (first, second):
            RunEvent.objects.create(
                run=run, event_type="output", timestamp=timezone.now(), payload="x", sequence=0
            )

        assert RunEvent.objects.filter(sequence=0).count() == 2

    def test_the_unique_index_exists_in_mongodb(self):
        """Asserted against MongoDB itself, not against Django's model state:
        a constraint declared in `Meta` but never migrated would still pass a
        model-level check.
        """
        indexes = connection.database[RunEvent._meta.db_table].index_information()
        compound = [
            spec
            for spec in indexes.values()
            if spec.get("unique") and [key for key, _ in spec["key"]] == ["run_id", "sequence"]
        ]

        assert compound, f"no unique (run_id, sequence) index; have {list(indexes)}"


# --- 3. transaction primitives ---------------------------------------------


@pytest.mark.django_db
class TestTransactionPrimitives:
    """The backend's transaction API is real, and Django's native one is not.

    These are the assumptions every atomicity test below rests on, pinned
    directly so that a regression is reported here — as "the primitive
    changed" — rather than as a puzzling failure in the state machine tests.
    """

    def test_backend_reports_no_support_for_djangos_native_transactions(self):
        """The fact that makes `django.db.transaction` unusable here."""
        assert connection.features.supports_transactions is False

    def test_backend_reports_no_support_for_select_for_update(self):
        """And therefore why the sequence allocator cannot use a row lock."""
        assert connection.features.has_select_for_update is False

    def test_select_for_update_is_silently_ignored_rather_than_raising(self):
        """The reason the old ingest code was dangerous rather than broken.

        A backend that raised would have failed loudly on the first request.
        This one returns the row and takes no lock, so the read-modify-write
        it used to guard would have been left unprotected with every existing
        test still green.
        """
        run = _run()

        with mongo_transaction.atomic():
            assert Run.objects.select_for_update().get(pk=run.pk).pk == run.pk

    def test_mongodb_atomic_rolls_back_on_exception(self):
        run = _run()

        with pytest.raises(_Injected):
            with mongo_transaction.atomic():
                RunDiff.objects.create(run=run, files=[{"path": "a", "additions": 1, "deletions": 0}])
                raise _Injected

        assert not RunDiff.objects.filter(run=run).exists()

    def test_mongodb_atomic_commits_on_success(self):
        run = _run()

        with mongo_transaction.atomic():
            RunDiff.objects.create(run=run, files=[])

        assert RunDiff.objects.filter(run=run).exists()

    def test_djangos_native_atomic_does_not_roll_back_on_this_backend(self):
        """The negative control, and the whole reason for the custom import.

        If this ever starts rolling back, `django-mongodb-backend` has gained
        native transaction support and the explicit imports in `runs/views.py`
        and `security_scan/tasks.py` could be reconsidered. Until then this
        test documents why they are not a stylistic choice.
        """
        run = _run()

        with pytest.raises(_Injected):
            with django_transaction.atomic():
                RunDiff.objects.create(run=run, files=[])
                raise _Injected

        assert RunDiff.objects.filter(run=run).exists(), (
            "django.db.transaction.atomic() rolled back, which contradicts "
            "supports_transactions=False"
        )

    def test_on_commit_does_not_fire_before_the_block_exits(self):
        run = _run()
        fired: list[str] = []

        with mongo_transaction.atomic():
            RunDiff.objects.create(run=run, files=[])
            mongo_transaction.on_commit(lambda: fired.append("called"))
            assert fired == [], "callback fired before commit"

        assert fired == ["called"]

    def test_on_commit_is_discarded_when_the_block_rolls_back(self):
        run = _run()
        fired: list[str] = []

        with pytest.raises(_Injected):
            with mongo_transaction.atomic():
                RunDiff.objects.create(run=run, files=[])
                mongo_transaction.on_commit(lambda: fired.append("called"))
                raise _Injected

        assert fired == []


# --- 4. cross-collection operations ----------------------------------------


@pytest.mark.django_db
class TestCrossCollectionLimits:
    """The limit that forced the sandbox reaper to be rewritten.

    Pinned as a test because the rewrite reads like a stylistic choice
    otherwise, and because a future backend release lifting the restriction is
    worth noticing.
    """

    def test_update_spanning_collections_is_refused(self):
        run = _run()
        SandboxRecord.objects.create(run=run)

        with pytest.raises(NotSupportedError):
            SandboxRecord.objects.filter(status="active").exclude(
                run__status__in=["running"]
            ).update(status="torn_down")

    def test_filtering_on_the_local_foreign_key_column_is_allowed(self):
        """What the rewritten reaper uses instead: `run_id` is a local field,
        so no join is involved and the update stays in one collection."""
        run = _run(status="approved")
        SandboxRecord.objects.create(run=run)

        changed = (
            SandboxRecord.objects.filter(status="active")
            .exclude(run_id__in=[ObjectId()])
            .update(status="torn_down")
        )

        assert changed == 1


# --- 5. event sequence allocation (Requirement A / 13.1) -------------------


@pytest.mark.django_db
class TestSequenceAllocation:
    """The atomic `$inc` allocator's single-threaded contract."""

    def test_a_fresh_run_starts_at_zero(self):
        """Zero-based so a single-batch Run's sequences line up with the line
        indices of the `events.jsonl` they came from."""
        assert list(allocate_event_sequences(_run(), 3)) == [0, 1, 2]

    def test_blocks_are_contiguous_and_continue_across_calls(self):
        run = _run()

        assert list(allocate_event_sequences(run, 3)) == [0, 1, 2]
        assert list(allocate_event_sequences(run, 2)) == [3, 4]
        assert list(allocate_event_sequences(run, 1)) == [5]

    def test_allocating_zero_reserves_nothing_and_writes_nothing(self):
        """An empty batch is legitimate and must not advance the cursor, or a
        retried empty upload would leave a gap for no reason."""
        run = _run()

        assert list(allocate_event_sequences(run, 0)) == []
        assert list(allocate_event_sequences(run, 1)) == [0]

    def test_a_negative_count_is_rejected(self):
        with pytest.raises(ValueError):
            allocate_event_sequences(_run(), -1)

    def test_allocation_for_a_deleted_run_raises(self):
        run = _run()
        Run.objects.filter(pk=run.pk).delete()

        with pytest.raises(SequenceAllocationError):
            allocate_event_sequences(run, 1)

    def test_allocation_does_not_touch_updated_at(self):
        """`Run.updated_at` is `flag_stuck_approvals`' input, so event ingest
        must leave it alone — which the raw `$inc` does by bypassing
        `auto_now`."""
        run = _run()
        before = Run.objects.get(pk=run.pk).updated_at

        allocate_event_sequences(run, 5)

        assert Run.objects.get(pk=run.pk).updated_at == before

    def test_allocation_does_not_depend_on_stored_rows(self):
        """The cursor is authoritative, not `MAX(sequence)`.

        This is the behavioural difference from the PostgreSQL implementation:
        deleting stored events does not make their numbers available again, so
        a replayed batch can never collide with one that was already ingested.
        """
        run = _run()
        _ingest(run, _event_payloads(3))
        RunEvent.objects.filter(run=run).delete()

        assert list(allocate_event_sequences(run, 1)) == [3]

    def test_highest_stored_sequence_reports_rows_not_the_cursor(self):
        run = _run()
        assert highest_stored_sequence(run) is None

        _ingest(run, _event_payloads(4))
        assert highest_stored_sequence(run) == 3


@pytest.mark.django_db(transaction=True)
class TestConcurrentSequenceAllocation:
    """Requirement 9: real concurrent ingestion, proven unique and ordered.

    The test the PostgreSQL suite never had — `settings_test.py` used to note
    that the `select_for_update()` path could not run on SQLite and that the
    locking test was therefore unwritten. That gap is exactly how the silent
    loss of the lock would have gone unnoticed, so the replacement mechanism
    ships with the test the original lacked.

    `threading.Barrier` is what makes this a real contention test rather than
    an incidental one: every worker blocks until all of them are ready, so the
    allocations are issued as close to simultaneously as the interpreter
    allows. Without it the pool would likely run them one after another and
    the test would pass regardless of whether the mechanism was atomic.
    """

    WORKERS = 8
    PER_WORKER = 25

    @staticmethod
    def _run_workers(work, count):
        """Run `work(index)` in `count` threads released simultaneously."""
        barrier = threading.Barrier(count)

        def wrapped(index):
            barrier.wait()
            try:
                return work(index)
            finally:
                # Each thread opens its own Django connection; leaving them
                # open leaks MongoClients across the run.
                connections.close_all()

        with ThreadPoolExecutor(max_workers=count) as pool:
            return [future.result() for future in [pool.submit(wrapped, i) for i in range(count)]]

    def test_concurrent_allocations_are_disjoint_and_exhaustive(self):
        """No number is handed out twice, and none is skipped.

        Exhaustiveness is the stronger half: "no duplicates" alone would also
        hold if the allocator handed out wildly scattered numbers. Asserting
        the union is exactly `range(total)` proves each `$inc` advanced the
        cursor by precisely its own block and no allocation was lost.
        """
        run = _run()

        blocks = self._run_workers(
            lambda _: list(allocate_event_sequences(run, self.PER_WORKER)), self.WORKERS
        )

        allocated = [number for block in blocks for number in block]
        total = self.WORKERS * self.PER_WORKER

        assert len(allocated) == total
        assert len(set(allocated)) == total, "a sequence number was allocated twice"
        assert sorted(allocated) == list(range(total)), "the allocated range has gaps"

    def test_each_concurrent_block_is_internally_contiguous(self):
        """A batch's own events must stay together, or its submitted order
        would be interleaved with another batch's in the stored ordering."""
        run = _run()

        blocks = self._run_workers(
            lambda _: list(allocate_event_sequences(run, self.PER_WORKER)), self.WORKERS
        )

        for block in blocks:
            assert block == list(range(block[0], block[0] + self.PER_WORKER))

    def test_concurrent_ingest_preserves_each_batch_submitted_order(self):
        """Requirement 13.1 end to end, through the real serializer path.

        Each worker tags its payloads with its own prefix, so after the dust
        settles every worker's events can be pulled out of the stored ordering
        and checked to still read in the order that worker submitted them.
        """
        run = _run()

        self._run_workers(
            lambda index: _ingest(run, _event_payloads(self.PER_WORKER, prefix=f"w{index}")),
            self.WORKERS,
        )

        stored = list(
            RunEvent.objects.filter(run=run)
            .order_by("sequence")
            .values_list("sequence", "payload")
        )
        sequences = [sequence for sequence, _ in stored]
        total = self.WORKERS * self.PER_WORKER

        assert len(stored) == total, "events were lost"
        assert len(set(sequences)) == total, "a sequence number was stored twice"
        assert sequences == sorted(sequences)

        for index in range(self.WORKERS):
            prefix = f"w{index}-"
            own = [payload for _, payload in stored if payload.startswith(prefix)]
            assert own == [f"{prefix}{position}" for position in range(self.PER_WORKER)], (
                f"worker {index}'s batch did not keep its submitted order"
            )

    def test_concurrent_decisions_on_one_run_yield_exactly_one_approval(self):
        """Requirement 21.3 under contention.

        The conditional `UPDATE` is the guard, and MongoDB applies an update to
        a single document atomically, so exactly one of many simultaneous
        decisions may win. The losers must fail cleanly rather than raise an
        `IntegrityError` from the `Approval` one-to-one.
        """
        from runs.views import StateTransitionError, apply_decision

        run = _run(status="awaiting_review")

        def decide(index):
            try:
                apply_decision(Run.objects.get(pk=run.pk), "approved" if index % 2 else "rejected")
                return "won"
            except StateTransitionError:
                return "refused"
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                # Named rather than propagated so a failure says *which* error
                # leaked. Before `retry_on_transient_transaction_error` was
                # added, this branch caught MongoDB write conflicts
                # (`DatabaseError`) for most losers — a 500 where Requirement
                # 21.3 specifies a 409.
                return f"{type(exc).__name__}"
            finally:
                connections.close_all()

        outcomes = self._run_workers(decide, 6)

        assert outcomes.count("won") == 1, f"expected exactly one winner, got {outcomes}"
        assert set(outcomes) <= {"won", "refused"}, (
            f"a loser failed with something other than a clean refusal: {outcomes}"
        )
        assert Approval.objects.filter(run=run).count() == 1
        assert Run.objects.get(pk=run.pk).status in {"approved", "rejected"}

    def test_repeated_contention_never_produces_two_approvals(self):
        """Run the race several times: a write conflict is probabilistic, so a
        single trial can pass by luck."""
        from runs.views import StateTransitionError, apply_decision

        project = _project()

        for trial in range(4):
            run = _run(project, status="awaiting_review")

            def decide(_index, target=run):
                try:
                    apply_decision(Run.objects.get(pk=target.pk), "approved")
                    return "won"
                except StateTransitionError:
                    return "refused"
                finally:
                    connections.close_all()

            outcomes = self._run_workers(decide, 5)

            assert outcomes.count("won") == 1, f"trial {trial}: {outcomes}"
            assert Approval.objects.filter(run=run).count() == 1


@pytest.mark.django_db(transaction=True)
class TestConcurrentStatusTransitions:
    """Run lifecycle: the CLI's completion and failure PATCHes racing on one
    `running` Run.

    Both are gated to `running`, so exactly one may win; the other must get
    a clean `StateTransitionError` (the 409), never a write conflict leaking
    out as a 500. Same worker shape as `TestConcurrentSequenceAllocation`:
    threads released together by a barrier, each with its own connection.
    """

    ITERATIONS = 20
    TARGETS = ("failed", "awaiting_review")

    def test_failed_and_awaiting_review_race_has_exactly_one_winner(self):
        from runs.views import StateTransitionError, apply_status_transition

        project = _project()

        for iteration in range(self.ITERATIONS):
            run = _run(project, status="running")

            def transition(index, target_run=run):
                target = self.TARGETS[index]
                try:
                    apply_status_transition(Run.objects.get(pk=target_run.pk), target, {})
                    return ("won", target)
                except StateTransitionError:
                    return ("refused", target)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    return (type(exc).__name__, target)
                finally:
                    connections.close_all()

            outcomes = TestConcurrentSequenceAllocation._run_workers(
                transition, len(self.TARGETS)
            )

            results = [result for result, _target in outcomes]
            assert sorted(results) == ["refused", "won"], f"iteration {iteration}: {outcomes}"
            winner = next(target for result, target in outcomes if result == "won")
            assert Run.objects.get(pk=run.pk).status == winner


@pytest.mark.django_db
class TestDuplicateEventIngestAndRetry:
    """Retry safety: a replayed batch never reuses sequence numbers."""

    def test_a_retried_batch_gets_fresh_numbers(self):
        """Requirement 14.4's retry is from the same local artifacts, so the
        same payloads arrive twice. Both copies are stored — events have no
        natural key, and two identical output chunks a second apart are two
        real events — but never on top of each other."""
        run = _run()
        payloads = _event_payloads(3)

        first = [event.sequence for event in _ingest(run, payloads)]
        second = [event.sequence for event in _ingest(run, payloads)]

        assert first == [0, 1, 2]
        assert second == [3, 4, 5]
        assert not set(first) & set(second)

    def test_an_abandoned_allocation_leaves_a_gap_and_no_collision(self):
        """A batch that allocated and then failed burns its block.

        The gap is the deliberate trade: reusing the numbers is what would risk
        a duplicate, and `RunEvent.Meta.ordering` is unaffected by gaps because
        ordering is relative.
        """
        run = _run()
        abandoned = list(allocate_event_sequences(run, 5))

        stored = [event.sequence for event in _ingest(run, _event_payloads(2))]

        assert abandoned == [0, 1, 2, 3, 4]
        assert stored == [5, 6]
        assert list(RunEvent.objects.filter(run=run).values_list("sequence", flat=True)) == [5, 6]

    def test_ingesting_the_same_payloads_twice_never_trips_the_unique_index(self):
        run = _run()
        payloads = _event_payloads(10)

        for _ in range(5):
            _ingest(run, payloads)

        sequences = list(RunEvent.objects.filter(run=run).values_list("sequence", flat=True))
        assert len(sequences) == 50
        assert len(set(sequences)) == 50


# --- 6. failure injection at every former transaction boundary -------------


@pytest.mark.django_db
class TestEventBatchAtomicity:
    """Requirement 10 for the event-ingest boundary.

    A failed ingest must store none of its events, so the Uploader's retry
    starts from a clean slate rather than duplicating a half-written batch.
    """

    def test_a_failure_mid_batch_stores_no_events(self):
        run = _run()
        _ingest(run, _event_payloads(2))
        before = set(RunEvent.objects.filter(run=run).values_list("sequence", flat=True))

        with pytest.raises(_Injected):
            with mongo_transaction.atomic():
                _ingest(run, _event_payloads(3))
                raise _Injected

        after = set(RunEvent.objects.filter(run=run).values_list("sequence", flat=True))
        assert after == before, "a rolled-back batch left events behind"

    def test_a_failure_leaves_no_partially_written_batch(self):
        """Injected *between* two writes inside one transaction, which is the
        shape a real mid-batch failure takes."""
        run = _run()

        with pytest.raises(_Injected):
            with mongo_transaction.atomic():
                _ingest(run, _event_payloads(2, prefix="first"))
                _ingest(run, _event_payloads(2, prefix="second"))
                raise _Injected

        assert RunEvent.objects.filter(run=run).count() == 0


@pytest.mark.django_db
class TestApprovalAtomicity:
    """Requirement 10 for the approve/reject boundary (Requirements 21.1-21.3).

    `apply_decision` writes two things — the Run's status and the `Approval`
    row — and the invariant is that a Run never carries a decided status with
    no `Approval` behind it. Under PostgreSQL the transaction was what held
    those together. These tests prove the MongoDB transaction does the same by
    breaking the sequence in the middle.
    """

    def test_a_failure_after_the_status_update_rolls_the_status_back(self):
        from runs.views import apply_decision

        run = _run(status="awaiting_review")

        with patch(
            "runs.views.Approval.objects.create", side_effect=_Injected
        ), pytest.raises(_Injected):
            apply_decision(run, "approved")

        stored = Run.objects.get(pk=run.pk)
        assert stored.status == "awaiting_review", "status advanced without an Approval"
        assert not Approval.objects.filter(run=run).exists()

    def test_no_decided_run_ever_lacks_an_approval(self):
        """The invariant stated directly, after a failed and then a successful
        decision on the same Run."""
        from runs.views import apply_decision

        run = _run(status="awaiting_review")

        with patch(
            "runs.views.Approval.objects.create", side_effect=_Injected
        ), pytest.raises(_Injected):
            apply_decision(run, "approved")

        apply_decision(Run.objects.get(pk=run.pk), "approved")

        stored = Run.objects.get(pk=run.pk)
        assert stored.status == "approved"
        assert Approval.objects.filter(run=run).count() == 1

    def test_a_second_decision_is_refused_without_an_integrity_error(self):
        from runs.views import StateTransitionError, apply_decision

        run = _run(status="awaiting_review")
        apply_decision(run, "approved")

        with pytest.raises(StateTransitionError):
            apply_decision(Run.objects.get(pk=run.pk), "rejected")

        assert Approval.objects.filter(run=run).count() == 1
        assert Run.objects.get(pk=run.pk).status == "approved"

    def test_updated_at_is_stored_exactly_as_returned(self):
        """MongoDB stores milliseconds, so `apply_decision` truncates before
        writing; otherwise the decision response and every later GET would
        disagree about an unchanged row. See `vaultshield_backend/mongodb.py`.
        """
        from runs.views import apply_decision

        run = _run(status="awaiting_review")
        returned = apply_decision(run, "approved")

        assert Run.objects.get(pk=run.pk).updated_at == returned.updated_at


@pytest.mark.django_db
class TestRunStatusTransitions:
    """Run lifecycle through `PATCH /api/runs/:id/` (Requirement 12.4).

    `awaiting_review` (the CLI's completion signal) and `failed` (its error
    signal) are accepted only from `running`; from any other status the
    PATCH is a 409 naming the stored status and the row is not touched.
    approve/reject keep their own gate (`awaiting_review` only).
    """

    GATED_TARGETS = ("failed", "awaiting_review")
    NON_RUNNING = ("awaiting_review", "approved", "rejected", "submitted", "failed")
    ORIGINAL_PR_URL = "https://example.invalid/pr/original"

    @staticmethod
    def _aged(run: Run) -> Run:
        """Push `updated_at` an hour back so a bump is observable."""
        Run.objects.filter(pk=run.pk).update(
            updated_at=to_mongo_precision(timezone.now() - timedelta(hours=1))
        )
        return Run.objects.get(pk=run.pk)

    @pytest.mark.parametrize("target", GATED_TARGETS)
    def test_a_gated_status_is_accepted_from_running(self, target):
        client, _, _ = _authenticated_client()
        run = self._aged(_run(status="running"))

        response = client.patch(f"/api/runs/{run.run_id}/", {"status": target}, format="json")

        assert response.status_code == 200, response.data
        assert response.data["status"] == target
        stored = Run.objects.get(pk=run.pk)
        assert stored.status == target
        assert stored.updated_at > run.updated_at, "updated_at was not bumped"
        # Stored exactly as returned, as `TestApprovalAtomicity` checks for
        # decisions: the response must not report microseconds MongoDB drops.
        assert RunSerializer(stored).data["updated_at"] == response.data["updated_at"]

    def test_a_gated_patch_writes_pr_url_in_the_same_update(self):
        client, _, _ = _authenticated_client()
        run = _run(status="running")

        response = client.patch(
            f"/api/runs/{run.run_id}/",
            {"status": "failed", "pr_url": "https://example.invalid/pr/3"},
            format="json",
        )

        assert response.status_code == 200, response.data
        stored = Run.objects.get(pk=run.pk)
        assert (stored.status, stored.pr_url) == ("failed", "https://example.invalid/pr/3")
        assert response.data["pr_url"] == "https://example.invalid/pr/3"

    @pytest.mark.parametrize("source", NON_RUNNING)
    @pytest.mark.parametrize("target", GATED_TARGETS)
    def test_a_gated_status_is_refused_from_anything_but_running(self, target, source):
        client, _, _ = _authenticated_client()
        run = self._aged(_run(status=source, pr_url=self.ORIGINAL_PR_URL))

        response = client.patch(
            f"/api/runs/{run.run_id}/",
            {"status": target, "pr_url": "https://example.invalid/pr/new"},
            format="json",
        )

        assert response.status_code == 409, response.data
        body = response.json()
        stored = Run.objects.get(pk=run.pk)
        assert body["current_status"] == stored.status == source
        assert body["detail"] == (
            f"A Run can only move to `{target}` from `running`; this Run is `{source}`."
        )
        assert (stored.status, stored.pr_url, stored.updated_at) == (
            run.status,
            run.pr_url,
            run.updated_at,
        ), "a refused transition changed the row"

    @pytest.mark.parametrize("decision", ("approve", "reject"))
    @pytest.mark.parametrize("source", ("running", "failed"))
    def test_approve_and_reject_are_refused_outside_awaiting_review(self, decision, source):
        client, _, _ = _authenticated_client()
        run = _run(status=source)

        response = client.post(f"/api/runs/{run.run_id}/{decision}/")

        assert response.status_code == 409, response.data
        assert response.json()["current_status"] == source
        assert Run.objects.get(pk=run.pk).status == source
        assert not Approval.objects.filter(run=run).exists()

    @pytest.mark.parametrize(
        ("decision", "decided"), (("approve", "approved"), ("reject", "rejected"))
    )
    def test_approve_and_reject_still_work_from_awaiting_review(self, decision, decided):
        client, _, _ = _authenticated_client()
        run = _run(status="awaiting_review")

        response = client.post(f"/api/runs/{run.run_id}/{decision}/")

        assert response.status_code == 200, response.data
        assert response.data["status"] == decided
        assert Run.objects.get(pk=run.pk).status == decided
        assert Approval.objects.get(run=run).decision == decided

    def test_a_pr_url_patch_does_not_write_back_a_stale_status(self):
        """A non-gated PATCH saves only the fields it names, so the status
        loaded with the instance cannot overwrite one stored since."""
        run = _run(status="running")
        instance = Run.objects.get(pk=run.pk)
        Run.objects.filter(pk=run.pk).update(status="failed")

        serializer = RunUpdateSerializer(
            instance, data={"pr_url": "https://example.invalid/pr/7"}, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        stored = Run.objects.get(pk=run.pk)
        assert stored.status == "failed"
        assert stored.pr_url == "https://example.invalid/pr/7"


@pytest.mark.django_db
class TestSecurityScanAtomicity:
    """Requirement 10 for the scan boundary (Requirements 17.1, 17.2).

    `run_security_scan` deletes a Run's findings and then inserts the new set.
    Without a real transaction, a failure between the two leaves the Run with
    *zero* findings — which the dashboard renders as "no findings", i.e. "this
    diff is clean", rather than "the scan did not finish".
    """

    def _run_with_diff(self) -> Run:
        run = _run()
        RunDiff.objects.create(run=run, files=_risky_files())
        return run

    def test_a_failure_between_delete_and_insert_preserves_previous_findings(self):
        run = self._run_with_diff()
        SecurityFinding.objects.create(
            run=run, rule_id="previous", file_path="old.py", line_number=1, description="stale"
        )

        with patch(
            "security_scan.tasks.SecurityFinding.objects.bulk_create", side_effect=_Injected
        ), pytest.raises(_Injected):
            run_security_scan(run.run_id)

        surviving = list(SecurityFinding.objects.filter(run=run).values_list("rule_id", flat=True))
        assert surviving == ["previous"], (
            "the previous scan's findings were destroyed by a failed rescan"
        )

    def test_a_run_is_never_left_with_zero_findings_by_a_failed_rescan(self):
        run = self._run_with_diff()
        run_security_scan(run.run_id)
        first_pass = SecurityFinding.objects.filter(run=run).count()
        assert first_pass > 0, "fixture diff produced no findings; test is vacuous"

        with patch(
            "security_scan.tasks.SecurityFinding.objects.bulk_create", side_effect=_Injected
        ), pytest.raises(_Injected):
            run_security_scan(run.run_id)

        assert SecurityFinding.objects.filter(run=run).count() == first_pass


@pytest.mark.django_db
class TestSecurityScanOrdering:
    """Requirement 18.1 / Requirement C: the scan cannot start before the diff.

    The scan is enqueued from `transaction.on_commit`, and the import it comes
    from is the whole guarantee. Django's native `on_commit` fires immediately
    on this backend, which would publish the task while the `RunDiff` was still
    uncommitted — the worker would find no diff, take the `DoesNotExist` no-op
    branch, and leave the Security tab silently empty.
    """

    def test_the_diff_is_committed_and_visible_when_the_task_is_enqueued(self):
        """Asserted from inside the callback, which is the only place the
        ordering is observable."""
        client, _, _ = _authenticated_client()
        run = _run()
        observed: dict[str, bool] = {}

        def record(run_id):
            observed["diff_visible"] = RunDiff.objects.filter(run__run_id=run_id).exists()

        with patch("runs.views.run_security_scan.delay", side_effect=record):
            response = client.post(
                f"/api/runs/{run.run_id}/diff/",
                {"files": [{"path": "a.py", "additions": 1, "deletions": 0}]},
                format="json",
            )

        assert response.status_code == 201
        assert observed.get("diff_visible") is True, (
            "the scan was enqueued before the RunDiff was visible"
        )

    def test_no_scan_is_enqueued_when_the_diff_write_rolls_back(self):
        run = _run()

        with patch("runs.views.run_security_scan.delay") as delay, patch(
            "runs.serializers.RunDiff.objects.update_or_create", side_effect=_Injected
        ):
            client, _, _ = _authenticated_client()
            with pytest.raises(_Injected):
                client.post(
                    f"/api/runs/{run.run_id}/diff/",
                    {"files": [{"path": "a.py", "additions": 1, "deletions": 0}]},
                    format="json",
                )

        delay.assert_not_called()
        assert not RunDiff.objects.filter(run=run).exists()

    def test_findings_are_stored_in_the_order_the_scanner_returned(self):
        """Requirement 17.2's ordering, which `bulk_create` preserves and the
        Security tab reads back."""
        run = _run()
        RunDiff.objects.create(
            run=run,
            files=[
                {"path": "z_last.sh", "patch": _RISKY_PATCH},
                {"path": "a_first.sh", "patch": _RISKY_PATCH},
            ],
        )

        run_security_scan(run.run_id)

        from security_scan.rules import scan_diff

        expected = [
            (finding.rule_id, finding.file_path, finding.line_number)
            for finding in scan_diff(RunDiff.objects.get(run=run))
        ]
        stored = [
            (finding.rule_id, finding.file_path, finding.line_number)
            for finding in SecurityFinding.objects.filter(run=run).order_by("pk")
        ]

        assert stored == expected


@pytest.mark.django_db
class TestDiffIngestProducesSecurityFindings:
    """Requirements 13.2, 17.1, 17.2 and 18.1, end to end: a `patch` the CLI
    posts to `/diff/` reaches the scanner, and its finding lands on the
    post-change line its hunk header implies.

    The real Celery task body runs synchronously in place of `.delay`, from
    the backend's `on_commit`, so it sees the committed `RunDiff` exactly as a
    worker would.
    """

    # The `-U0` shape `cli/vaultshield/git_capture.py` captures: the hunk
    # starts at new line 10, the removed line does not advance the position,
    # `DEBUG = False` sits at 10, so the key is at 11.
    PATCH = (
        "diff --git a/settings.py b/settings.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/settings.py\n"
        "+++ b/settings.py\n"
        "@@ -10 +10,2 @@\n"
        "-DEBUG = True\n"
        "+DEBUG = False\n"
        '+API_KEY = "sk-abcdefghij123456"\n'
    )

    def test_a_posted_secret_becomes_a_secret_literal_finding_on_its_line(self):
        client, _, _ = _authenticated_client()
        run = _run()
        files = [{"path": "settings.py", "additions": 2, "deletions": 1, "patch": self.PATCH}]

        with patch("runs.views.run_security_scan.delay", side_effect=run_security_scan):
            response = client.post(
                f"/api/runs/{run.run_id}/diff/", {"files": files}, format="json"
            )

        assert response.status_code == 201
        assert list(
            SecurityFinding.objects.filter(run=run).values_list(
                "rule_id", "file_path", "line_number"
            )
        ) == [("secret-literal", "settings.py", 11)]
        findings = client.get(f"/api/runs/{run.run_id}/").data["security_findings"]
        assert [
            (finding["rule_id"], finding["file_path"], finding["line_number"])
            for finding in findings
        ] == [("secret-literal", "settings.py", 11)]


# --- 7. sandbox cleanup (Requirement D / 25.1) ------------------------------


@pytest.mark.django_db
class TestSandboxCleanup:
    """The rewritten reaper: same behaviour, two single-collection queries.

    The one guarantee the rewrite narrows — the excluded-Run set is now a
    snapshot taken just before the write rather than evaluated inside it — is
    documented on `cleanup_orphaned_sandboxes` and is unreachable through
    VaultShield's own lifecycle, since `Run.status` never moves back to
    `running`.
    """

    TTL = 30

    def _stale_record(self, run: Run, *, minutes_stale: int = TTL + 1) -> SandboxRecord:
        record = SandboxRecord.objects.create(run=run)
        # `last_heartbeat_at` is `auto_now_add`, so it is backdated with a
        # queryset update, which does not re-run the field's default.
        SandboxRecord.objects.filter(pk=record.pk).update(
            last_heartbeat_at=timezone.now() - timedelta(minutes=minutes_stale)
        )
        return record

    def test_a_stale_record_on_a_finished_run_is_reaped(self):
        record = self._stale_record(_run(status="approved"))

        assert cleanup_orphaned_sandboxes(ttl_minutes=self.TTL) == 1

        stored = SandboxRecord.objects.get(pk=record.pk)
        assert stored.status == "torn_down"
        assert stored.torn_down_at is not None

    def test_a_record_on_a_still_running_run_is_spared(self):
        """The exclusion the cross-collection query used to express as a join,
        now expressed on the local `run_id` column."""
        record = self._stale_record(_run(status="running"))

        assert cleanup_orphaned_sandboxes(ttl_minutes=self.TTL) == 0
        assert SandboxRecord.objects.get(pk=record.pk).status == "active"

    def test_a_fresh_heartbeat_is_spared(self):
        record = self._stale_record(_run(status="approved"), minutes_stale=0)

        assert cleanup_orphaned_sandboxes(ttl_minutes=self.TTL) == 0
        assert SandboxRecord.objects.get(pk=record.pk).status == "active"

    def test_the_pass_is_idempotent(self):
        """The `status="active"` predicate is still evaluated by the write, so
        a second pass reports 0 rather than restamping."""
        record = self._stale_record(_run(status="rejected"))

        assert cleanup_orphaned_sandboxes(ttl_minutes=self.TTL) == 1
        first_stamp = SandboxRecord.objects.get(pk=record.pk).torn_down_at

        assert cleanup_orphaned_sandboxes(ttl_minutes=self.TTL) == 0
        assert SandboxRecord.objects.get(pk=record.pk).torn_down_at == first_stamp

    def test_overlapping_passes_do_not_both_count_the_same_record(self):
        """Two passes racing must not both claim a row — the property the
        single-statement PostgreSQL `UPDATE` gave for free."""
        self._stale_record(_run(status="approved"))
        barrier = threading.Barrier(2)

        def reap():
            barrier.wait()
            try:
                return cleanup_orphaned_sandboxes(ttl_minutes=self.TTL)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = [f.result() for f in [pool.submit(reap), pool.submit(reap)]]

        assert sum(counts) == 1, f"the record was counted {sum(counts)} times: {counts}"

    def test_no_run_is_modified_and_no_record_is_deleted(self):
        """The half of Property 34 this task is responsible for."""
        run = _run(status="approved")
        record = self._stale_record(run)
        status_before = Run.objects.get(pk=run.pk).status

        cleanup_orphaned_sandboxes(ttl_minutes=self.TTL)

        assert Run.objects.get(pk=run.pk).status == status_before
        assert SandboxRecord.objects.filter(pk=record.pk).exists()

    def test_flag_stuck_approvals_writes_only_the_stuck_field(self):
        """The other Beat task, which stayed a single-collection update."""
        run = _run(status="approved")
        Run.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(hours=48))
        before = Run.objects.get(pk=run.pk)

        assert flag_stuck_approvals(max_age_hours=24) == 1

        after = Run.objects.get(pk=run.pk)
        assert after.stuck is True
        assert after.status == before.status
        assert after.pr_url == before.pr_url
        # The task's own input: bumping it would erase how long the Run waited.
        assert after.updated_at == before.updated_at


# --- 8. authentication, permissions, REST API ------------------------------


@pytest.mark.django_db
class TestAuthenticationAndPermissions:
    """Requirements 27.3-27.5 on MongoDB, plus the `post_migrate` permissions.

    The permission assertions are not incidental: the first attempt at this
    migration failed with `TypeError: Model instances without primary key value
    are unhashable` inside `contrib.auth`'s `create_permissions`, leaving
    `auth_permission` empty while `migrate` exited non-zero. These tests are
    what keep that fixed.
    """

    def test_a_user_can_be_created_and_authenticated(self):
        user = User.objects.create_user(username="reviewer", password="pw-not-a-secret")

        assert isinstance(user.pk, ObjectId)
        assert User.objects.get(pk=user.pk).check_password("pw-not-a-secret")

    def test_a_token_is_issued_and_resolves_back_to_its_user(self):
        user = User.objects.create_user(username="reviewer", password="pw-not-a-secret")
        token = Token.objects.create(user=user)

        assert Token.objects.select_related("user").get(key=token.key).user == user

    def test_the_token_primary_key_is_its_key_not_an_objectid(self):
        """`Token.key` is an explicit `CharField` primary key, which is why
        `authtoken` needed no AppConfig override — see
        `vaultshield_backend/apps.py`."""
        user = User.objects.create_user(username="reviewer", password="pw-not-a-secret")
        token = Token.objects.create(user=user)

        assert token.pk == token.key
        assert isinstance(token.pk, str)

    def test_permissions_were_created_by_post_migrate(self):
        assert Permission.objects.exists(), "auth_permission is empty"

    def test_every_vaultshield_model_has_its_four_permissions(self):
        for model in (Project, Run, RunEvent, RunDiff, Approval, SandboxRecord, SecurityFinding):
            content_type = ContentType.objects.get_for_model(model)
            codenames = set(
                Permission.objects.filter(content_type=content_type).values_list(
                    "codename", flat=True
                )
            )
            expected = {
                f"{action}_{model._meta.model_name}"
                for action in ("add", "change", "delete", "view")
            }
            assert codenames == expected, f"{model.__name__}: {codenames}"

    def test_a_permission_can_be_granted_and_checked(self):
        user = User.objects.create_user(username="reviewer", password="pw-not-a-secret")
        permission = Permission.objects.get(
            content_type=ContentType.objects.get_for_model(Run), codename="change_run"
        )

        user.user_permissions.add(permission)

        assert User.objects.get(pk=user.pk).has_perm("runs.change_run")

    def test_an_unauthenticated_api_request_is_rejected(self):
        assert APIClient().get("/api/runs/").status_code == 401

    def test_a_bad_token_is_rejected(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")

        assert client.get("/api/runs/").status_code == 401

    def test_a_valid_token_is_accepted(self):
        client, _, _ = _authenticated_client()

        assert client.get("/api/runs/").status_code == 200

    def test_login_issues_a_token(self):
        User.objects.create_user(username="reviewer", password="pw-not-a-secret")

        response = APIClient().post(
            "/api/auth/login/",
            {"username": "reviewer", "password": "pw-not-a-secret"},
            format="json",
        )

        assert response.status_code == 200
        assert Token.objects.filter(key=response.data["token"]).exists()

    def test_an_unauthenticated_write_has_no_side_effect(self):
        """Requirement 27.4: rejected before the view body, so nothing is
        created."""
        project = _project()

        response = APIClient().post(
            "/api/runs/",
            {"project": str(project.pk), "run_id": "should-not-exist", "agent": "mock"},
            format="json",
        )

        assert response.status_code == 401
        assert not Run.objects.filter(run_id="should-not-exist").exists()


@pytest.mark.django_db
class TestRestApiAgainstMongoDB:
    """The documented REST surface, end to end, on MongoDB."""

    def test_create_and_list_a_project(self):
        client, _, _ = _authenticated_client()

        created = client.post(
            "/api/projects/",
            {"name": "vaultshield", "repo_url": "https://example.invalid/r.git"},
            format="json",
        )
        listed = client.get("/api/projects/")

        assert created.status_code == 201
        assert listed.status_code == 200
        assert listed.data["count"] == 1

    def test_project_id_is_serialised_as_an_objectid_string(self):
        """The one API contract change the migration forces: `id` was an
        integer under PostgreSQL and is now a 24-character hex string."""
        client, _, _ = _authenticated_client()

        response = client.post(
            "/api/projects/",
            {"name": "vaultshield", "repo_url": "https://example.invalid/r.git"},
            format="json",
        )

        identifier = response.data["id"]
        assert isinstance(identifier, str)
        assert len(identifier) == 24
        assert ObjectId.is_valid(identifier)

    def test_create_a_run_referencing_a_project_by_objectid(self):
        client, _, _ = _authenticated_client()
        project = _project()

        response = client.post(
            "/api/runs/",
            {"project": str(project.pk), "run_id": "20240101-000000-abc", "agent": "mock"},
            format="json",
        )

        assert response.status_code == 201
        assert Run.objects.get(run_id="20240101-000000-abc").project_id == project.pk

    def test_a_sequential_duplicate_run_id_keeps_todays_400_body(self):
        """Requirement 12.5: the explicit `UniqueValidator` answers exactly as
        the column-derived one did."""
        client, _, _ = _authenticated_client()
        existing = _run()

        response = client.post(
            "/api/runs/",
            {"project": str(existing.project_id), "run_id": existing.run_id, "agent": "mock"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json() == {"run_id": ["run with this run id already exists."]}
        assert Run.objects.filter(run_id=existing.run_id).count() == 1

    def test_a_lost_duplicate_run_id_race_is_a_400_not_a_500(self):
        """The validator pre-check is bypassed, so the insert reaches the real
        unique index and raises the real `IntegrityError`, which
        `perform_create` maps to the validator's own body."""
        client, _, _ = _authenticated_client()
        existing = _run()

        with patch.object(UniqueValidator, "__call__", return_value=None) as bypassed:
            response = client.post(
                "/api/runs/",
                {
                    "project": str(existing.project_id),
                    "run_id": existing.run_id,
                    "agent": "mock",
                },
                format="json",
            )

        # The stand-in ran in place of the real pre-check, so this 400 can
        # only have come from the insert's `IntegrityError`.
        assert bypassed.called
        assert response.status_code == 400
        assert response.json() == {"run_id": [RUN_ID_UNIQUE_MESSAGE]}
        assert Run.objects.filter(run_id=existing.run_id).count() == 1

    @pytest.mark.parametrize("run_id", ["a/b", "a.b", "-x", "", "a" * 65])
    def test_an_unaddressable_run_id_is_rejected(self, run_id):
        client, _, _ = _authenticated_client()
        project = _project()

        response = client.post(
            "/api/runs/",
            {"project": str(project.pk), "run_id": run_id, "agent": "mock"},
            format="json",
        )

        assert response.status_code == 400
        assert "run_id" in response.json()
        assert not Run.objects.exists()

    @pytest.mark.parametrize(
        "run_id", ["20260903-195802-e503f16521bb1a9c", "20240101-000000-abc"]
    )
    def test_a_cli_shaped_run_id_is_accepted(self, run_id):
        client, _, _ = _authenticated_client()
        project = _project()

        response = client.post(
            "/api/runs/",
            {"project": str(project.pk), "run_id": run_id, "agent": "mock"},
            format="json",
        )

        assert response.status_code == 201, response.data
        assert response.data["run_id"] == run_id
        assert Run.objects.get(run_id=run_id).status == "running"

    def test_a_run_is_addressed_by_run_id_not_by_primary_key(self):
        client, _, _ = _authenticated_client()
        run = _run()

        assert client.get(f"/api/runs/{run.run_id}/").status_code == 200

    def test_event_ingest_returns_the_assigned_sequences(self):
        client, _, _ = _authenticated_client()
        run = _run()

        response = client.post(
            f"/api/runs/{run.run_id}/events/", _event_payloads(3), format="json"
        )

        assert response.status_code == 201
        assert [event["sequence"] for event in response.data] == [0, 1, 2]

    def test_run_detail_nests_every_artifact(self):
        """Requirement 12.3 over MongoDB's `$lookup`-backed relations."""
        client, _, _ = _authenticated_client()
        run = _run()
        client.post(f"/api/runs/{run.run_id}/events/", _event_payloads(2), format="json")
        with patch("runs.views.run_security_scan.delay"):
            client.post(
                f"/api/runs/{run.run_id}/diff/",
                {"files": [{"path": "a.py", "additions": 1, "deletions": 0}]},
                format="json",
            )

        response = client.get(f"/api/runs/{run.run_id}/")

        assert response.status_code == 200
        assert len(response.data["events"]) == 2
        assert response.data["diff"]["files"][0]["path"] == "a.py"
        assert response.data["screenshots"] == []
        assert response.data["security_findings"] == []

    def test_run_detail_renders_a_missing_diff_as_null(self):
        client, _, _ = _authenticated_client()
        run = _run()

        assert client.get(f"/api/runs/{run.run_id}/").data["diff"] is None

    def test_a_diff_with_a_patch_round_trips_through_run_detail(self):
        """Requirements 13.2 and 17.1: `patch`/`patch_truncated` are stored
        and rendered by `RunDetailSerializer` exactly as posted."""
        client, _, _ = _authenticated_client()
        run = _run()
        files = [
            {
                "path": "src/app.py",
                "additions": 1,
                "deletions": 0,
                "patch": "@@ -0,0 +1 @@\n+  value = 1 \n",
                "patch_truncated": True,
            },
            # A binary entry, which the CLI sends without a patch.
            {"path": "logo.png", "additions": 0, "deletions": 0},
        ]

        with patch("runs.views.run_security_scan.delay"):
            posted = client.post(
                f"/api/runs/{run.run_id}/diff/", {"files": files}, format="json"
            )
        detail = client.get(f"/api/runs/{run.run_id}/")

        assert posted.status_code == 201
        assert detail.data["diff"]["files"] == files

    def test_a_diff_without_a_patch_still_round_trips(self):
        """A stats-only upload is stored with no keys added."""
        client, _, _ = _authenticated_client()
        run = _run()
        files = [{"path": "a.py", "additions": 1, "deletions": 0}]

        with patch("runs.views.run_security_scan.delay"):
            posted = client.post(
                f"/api/runs/{run.run_id}/diff/", {"files": files}, format="json"
            )
        detail = client.get(f"/api/runs/{run.run_id}/")

        assert posted.status_code == 201
        assert detail.data["diff"]["files"] == files

    def test_a_malformed_diff_is_rejected_with_400_and_nothing_stored(self):
        """Requirement 13.2's rejection, unchanged by the optional patch keys."""
        client, _, _ = _authenticated_client()
        run = _run()
        bodies = [
            {"files": "nope"},
            {"files": [{"path": "a.py", "additions": 1, "deletions": 0, "patch": ["x"]}]},
            {"files": [{"path": "a.py"}]},
        ]

        for body in bodies:
            with patch("runs.views.run_security_scan.delay") as delay:
                response = client.post(
                    f"/api/runs/{run.run_id}/diff/", body, format="json"
                )

            assert response.status_code == 400, body
            assert "files" in response.data
            assert not RunDiff.objects.filter(run=run).exists()
            delay.assert_not_called()

    def test_the_status_and_submitted_filters_work(self):
        """Requirement 12.6, which `?submitted=` derives from `pr_url`."""
        client, _, _ = _authenticated_client()
        project = _project()
        _run(project, status="approved")
        _run(project, status="approved", pr_url="https://example.invalid/pr/1")

        assert client.get("/api/runs/?status=approved").data["count"] == 2
        assert client.get("/api/runs/?submitted=true").data["count"] == 1
        assert client.get("/api/runs/?submitted=false").data["count"] == 1

    def test_approve_records_a_decision(self):
        client, _, _ = _authenticated_client()
        run = _run(status="awaiting_review")

        response = client.post(f"/api/runs/{run.run_id}/approve/")

        assert response.status_code == 200
        assert response.data["status"] == "approved"
        assert Approval.objects.get(run=run).decision == "approved"

    def test_approving_a_run_twice_is_a_conflict(self):
        client, _, _ = _authenticated_client()
        run = _run(status="awaiting_review")
        client.post(f"/api/runs/{run.run_id}/approve/")

        assert client.post(f"/api/runs/{run.run_id}/reject/").status_code == 409

    def test_patch_writes_back_a_pr_url(self):
        """Requirement 22.4, how `vaultshield submit` closes the loop."""
        client, _, _ = _authenticated_client()
        run = _run(status="approved")

        response = client.patch(
            f"/api/runs/{run.run_id}/",
            {"pr_url": "https://example.invalid/pr/9"},
            format="json",
        )

        assert response.status_code == 200
        assert Run.objects.get(pk=run.pk).pr_url == "https://example.invalid/pr/9"

    def test_a_malformed_run_create_is_rejected(self):
        client, _, _ = _authenticated_client()

        assert client.post("/api/runs/", {"agent": "mock"}, format="json").status_code == 400

    def test_list_responses_are_paginated(self):
        client, _, _ = _authenticated_client()
        project = _project()
        for _ in range(3):
            _run(project)

        body = client.get("/api/runs/").data

        assert set(body) >= {"count", "next", "previous", "results"}
        assert body["count"] == 3


# --- 9. Celery database operations -----------------------------------------


@pytest.mark.django_db
class TestCeleryDatabaseOperations:
    """Both Celery entry points read and write MongoDB correctly.

    Called directly rather than through a broker: what is under test is the
    database work, and `security_scan/tests.py` already covers the enqueue
    wiring.
    """

    def test_run_security_scan_reads_a_diff_and_writes_findings(self):
        run = _run()
        RunDiff.objects.create(run=run, files=_risky_files())

        run_security_scan(run.run_id)

        assert SecurityFinding.objects.filter(run=run).exists()

    def test_run_security_scan_replaces_rather_than_appends(self):
        run = _run()
        RunDiff.objects.create(run=run, files=_risky_files())

        run_security_scan(run.run_id)
        first = SecurityFinding.objects.filter(run=run).count()
        run_security_scan(run.run_id)

        assert SecurityFinding.objects.filter(run=run).count() == first

    def test_run_security_scan_tolerates_a_missing_diff(self):
        run = _run()

        run_security_scan(run.run_id)

        assert SecurityFinding.objects.filter(run=run).count() == 0

    def test_both_beat_tasks_return_counts_and_are_callable(self):
        assert cleanup_orphaned_sandboxes(ttl_minutes=30) == 0
        assert flag_stuck_approvals(max_age_hours=24) == 0


# --- 10. the transient-error retry helper ----------------------------------


class TestTransientTransactionRetry:
    """`retry_on_transient_transaction_error`, in isolation.

    No database: what is under test is the retry policy, and driving it with
    real write conflicts would make the assertions probabilistic. The
    end-to-end behaviour under genuine contention is
    `TestConcurrentSequenceAllocation::test_concurrent_decisions_on_one_run_yield_exactly_one_approval`.
    """

    @staticmethod
    def _transient() -> OperationFailure:
        """An `OperationFailure` carrying MongoDB's retry label, as the server
        sends it on a write conflict."""
        return OperationFailure(
            "WriteConflict", code=112, details={"errorLabels": [TRANSIENT_TRANSACTION_ERROR_LABEL]}
        )

    def test_a_successful_operation_runs_once(self):
        calls = []

        result = retry_on_transient_transaction_error(lambda: calls.append(1) or "done")

        assert result == "done"
        assert len(calls) == 1

    def test_a_transient_error_is_retried_until_it_succeeds(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise self._transient()
            return "eventually"

        assert retry_on_transient_transaction_error(flaky) == "eventually"
        assert len(attempts) == 3

    def test_a_transient_error_is_reraised_once_attempts_are_exhausted(self):
        attempts = []

        def always_conflicts():
            attempts.append(1)
            raise self._transient()

        with pytest.raises(OperationFailure):
            retry_on_transient_transaction_error(always_conflicts, attempts=3)

        assert len(attempts) == 3, "did not use its full attempt budget"

    def test_a_non_transient_error_is_not_retried(self):
        """An application-level refusal is a real answer, not a race. Retrying
        it would turn Requirement 21.3's 409 into a slower 409."""
        attempts = []

        def refuses():
            attempts.append(1)
            raise _Injected

        with pytest.raises(_Injected):
            retry_on_transient_transaction_error(refuses)

        assert len(attempts) == 1

    def test_a_transient_error_wrapped_in_a_django_database_error_is_detected(self):
        """The shape it actually arrives in: `django-mongodb-backend` re-raises
        PyMongo failures as `DatabaseError` with the original as `__cause__`."""
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 2:
                raise DatabaseError from self._transient()
            return "ok"

        assert retry_on_transient_transaction_error(flaky) == "ok"
        assert len(attempts) == 2

    def test_the_label_check_ignores_unrelated_failures(self):
        assert is_transient_transaction_error(OperationFailure("nope", code=1)) is False
        assert is_transient_transaction_error(ValueError("nope")) is False
        assert is_transient_transaction_error(self._transient()) is True

    def test_a_zero_attempt_budget_is_rejected(self):
        with pytest.raises(ValueError):
            retry_on_transient_transaction_error(lambda: None, attempts=0)
