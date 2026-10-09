"""Tests for the `runs` app.

Task 16.2's own checks live here: that `RunSerializer` rejects a malformed
`POST /api/runs/` payload (Requirement 12.5), that each of the three `Run`
serializers exposes the field surface its verb is specified to have
(Requirements 12.2, 12.3, 12.4), and that the routes are wired to
`RunViewSet` with the verbs design.md's REST API table gives `Run`.

Task 16.7's are alongside them: that the three ingest routes exist as
POST-only detail actions, that none of the ingest serializers accepts a
body-supplied `run` (the URL owns that), that `sequence` is the server's to
assign rather than a client's to submit (Requirement 13.1), and that a
malformed diff payload is rejected before it can reach the scanner
(Requirement 13.2). A diff entry's optional `patch`/`patch_truncated` are
checked here too: kept verbatim when sent, absent when not, and rejected
when wrong-typed, so the scanner (Requirement 17.1) reads exactly what the
CLI captured.

### Why these are database-free, and what that shapes

There is no `pytest.mark.django_db` below and no database involved: these
exercise serializer declarations and the resolved URL conf, neither of which
opens a connection. The endpoint-level assertions — a `Run` row persisted and
echoed, a detail response carrying every associated record, a PATCH touching
exactly the payload's fields, a rejected payload leaving the collection
untouched — are Properties 18-21, which are Tasks 16.3-16.6, and they run
against a real MongoDB in
`vaultshield_backend/test_mongodb.py::TestRestApiAgainstMongoDB`.

That constraint shows up in the payload shapes used for the rejection
cases: each one names only the field under test and omits the rest. Two of
`Run`'s writable fields cannot be validated without a query — `project` is
a `PrimaryKeyRelatedField` that resolves the pk, and `run_id` carries a
`UniqueValidator` for its `unique=True` column — so a payload
containing a plausible value for either would try to connect. Absent
required fields never reach their validators, and a blank, over-length,
or wrong-typed value fails before them, so every case below stays local.

The ingest cases inherit the same property for a simpler reason: no ingest
serializer declares a relation at all (`run` comes from the URL), so
validating one of their payloads touches no table. Persisting a batch does
— `sequence` numbering reads the Run's current high-water mark — which is
why every assertion below stops at `is_valid()` and never calls `save()`.

The rest of this file is populated by the other test tasks covering this
app: 14.3 (Run_Status choice set), 14.5 (admin registration), 16.3-16.6 and
16.8-16.9 (REST creation, detail, patch, ingest properties), 16.11
(`status`/`submitted` filters), 23.3 (diff ingest enqueues the scan), and
28.2 (approve/reject state machine).
"""

import pytest
from django.core.validators import RegexValidator
from django.urls import resolve, reverse
from rest_framework import serializers
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.validators import UniqueValidator

from runs.models import Run, RunDiff
from runs.serializers import (
    RUN_ID_PATTERN,
    RUN_ID_UNIQUE_MESSAGE,
    RunDetailSerializer,
    RunDiffIngestSerializer,
    RunDiffSerializer,
    RunEventBatchSerializer,
    RunEventIngestSerializer,
    RunSerializer,
    RunUpdateSerializer,
    ScreenshotIngestSerializer,
)
from runs.views import GATED_PATCH_TRANSITIONS, RunViewSet, StateTransitionError
from vaultshield_backend.testing import routed_methods

# One well-formed Event_Log batch as `cli/vaultshield/monitor.py` writes its
# JSONL records: `event_type`/`timestamp`/`payload` and nothing else, already
# timestamp-sorted by the Monitor's merge (Requirement 6.3).
SAMPLE_EVENT_BATCH = [
    {"event_type": "output", "timestamp": "2024-01-17T09:30:00.100000+00:00", "payload": "one"},
    {"event_type": "output", "timestamp": "2024-01-17T09:30:00.200000+00:00", "payload": "two"},
    {
        "event_type": "file_change",
        "timestamp": "2024-01-17T09:30:00.300000+00:00",
        "payload": "modified: src/app.py",
    },
]

# A generated `run_id` as `cli/vaultshield/run_directory.py` mints them:
# `<YYYYmmdd>-<HHMMSS>-<16 hex chars>`. Used for URL assertions only — no
# payload below carries a `run_id` that would reach the unique validator.
SAMPLE_RUN_ID = "20240117-093000-0123456789abcdef"


def _writable_fields(serializer) -> set[str]:
    return {name for name, field in serializer.fields.items() if not field.read_only}


def _read_only_fields(serializer) -> set[str]:
    return {name for name, field in serializer.fields.items() if field.read_only}


class TestRunSerializerFieldSurface:
    """Requirement 12.2: what `POST /api/runs/` accepts and returns."""

    # The only `Run` column deliberately absent from the API surface.
    #
    # `next_event_sequence` is the MongoDB event-sequence allocation cursor
    # (`runs/sequences.py`), advanced by a raw atomic `$inc` and meaningless to
    # a client. It arrived with the MongoDB migration as the replacement for
    # `SELECT ... FOR UPDATE` + `MAX(sequence)`, which needed no column because
    # PostgreSQL could hold a row lock instead.
    INTERNAL_RUN_COLUMNS = {"next_event_sequence"}

    def test_rendered_fields_are_every_run_column(self):
        """The response echoes the whole model bar the documented internals,
        and nothing invented.

        Still the guard it was before the MongoDB migration: `RunSerializer`
        lists `fields` explicitly rather than using `__all__` so that a column
        added by a later migration has to be opted in deliberately
        (Requirement 22.5 — the API must never publish a repository-hosting
        access token). Subtracting a named set keeps that guard intact, because
        a *new* unlisted column still fails this test; only the one internal
        column enumerated above is permitted to be missing.
        """
        model_fields = {field.name for field in Run._meta.concrete_fields}

        assert set(RunSerializer().fields) == model_fields - self.INTERNAL_RUN_COLUMNS

    def test_internal_columns_are_absent_from_every_run_serializer(self):
        """The allocation cursor is not exposed by any Run representation.

        `RunSerializer` is checked above; this covers the two subclasses,
        because inheriting `Meta.fields` is what keeps them in step and a
        subclass that re-listed fields could reintroduce the column.
        """
        for serializer_class in (RunSerializer, RunUpdateSerializer, RunDetailSerializer):
            exposed = set(serializer_class().fields)

            assert not (exposed & self.INTERNAL_RUN_COLUMNS), (
                f"{serializer_class.__name__} exposes internal column(s) "
                f"{exposed & self.INTERNAL_RUN_COLUMNS}"
            )

    def test_identity_fields_are_writable_on_create(self):
        """A Run is created with its Project, `run_id`, and agent.

        `status` and `pr_url` are writable but optional — both columns have
        defaults, so the CLI opens a Run without naming either.
        """
        assert _writable_fields(RunSerializer()) == {
            "project",
            "run_id",
            "agent",
            "status",
            "pr_url",
        }

    def test_required_fields_are_exactly_the_identity_fields(self):
        required = {name for name, field in RunSerializer().fields.items() if field.required}

        assert required == {"project", "run_id", "agent"}

    def test_stuck_and_timestamps_are_read_only(self):
        """`stuck` is the backend's own flag, not a client's input.

        Requirement 11.4 defines it as what `flag_stuck_approvals` sets on
        an approved Run that was never submitted, so a client able to write
        it could hide the situation the field exists to surface.
        """
        assert _read_only_fields(RunSerializer()) == {
            "id",
            "stuck",
            "created_at",
            "updated_at",
        }

    def test_status_accepts_only_the_defined_choice_set(self):
        """Requirement 11.2's six Run_Status values, no others."""
        status_field = RunSerializer().fields["status"]

        assert set(status_field.choices) == {value for value, _label in Run.STATUS_CHOICES}

    def test_failed_is_a_run_status(self):
        """`failed` is the sixth Run_Status, set by the CLI on an error after
        registration."""
        assert [value for value, _label in Run.STATUS_CHOICES] == [
            "running",
            "awaiting_review",
            "approved",
            "rejected",
            "submitted",
            "failed",
        ]
        assert ("failed", "failed") in Run.STATUS_CHOICES

    def test_run_id_carries_the_format_and_unique_validators(self):
        """Run lifecycle: create validates the CLI's safe path-component
        grammar, and still checks uniqueness with today's message.

        Asserted on the declared validators so it stays database-free; the
        request-level cases run in `test_mongodb.py`.
        """
        validators = RunSerializer().fields["run_id"].validators

        regex_validators = [v for v in validators if isinstance(v, RegexValidator)]
        unique_validators = [v for v in validators if isinstance(v, UniqueValidator)]
        assert len(regex_validators) == 1
        assert regex_validators[0].regex.pattern == RUN_ID_PATTERN
        assert len(unique_validators) == 1
        assert unique_validators[0].message == RUN_ID_UNIQUE_MESSAGE
        assert RUN_ID_UNIQUE_MESSAGE == "run with this run id already exists."

    def test_the_run_id_pattern_is_the_cli_grammar(self):
        """Same accepted and refused ids as
        `cli/vaultshield/run_directory._RUN_ID_PATTERN`."""
        regex = RegexValidator(RUN_ID_PATTERN).regex

        for good in (
            "20260903-195802-e503f16521bb1a9c",
            "20240101-000000-abc",
            "a",
            "A_b-9",
            "a" * 64,
        ):
            assert regex.search(good), good
        for bad in ("a/b", "a.b", "-x", "_x", "", "a" * 65, "a b", "a\n"):
            assert not regex.search(bad), bad


class TestRunSerializerRejectsInvalidPayload:
    """Requirement 12.5: missing or invalid required fields are rejected.

    Each case asserts both that validation failed and that the error is
    attributed to the offending field, since "rejected with a validation
    error response" is what the client has to be able to act on. Because
    DRF validates before `create()` runs, none of these could have written
    a row — the "SHALL NOT create any record" half of Property 21.
    """

    def test_empty_payload_reports_every_required_field(self):
        serializer = RunSerializer(data={})

        assert not serializer.is_valid()
        assert set(serializer.errors) == {"project", "run_id", "agent"}

    def test_blank_and_whitespace_only_run_id_is_rejected(self):
        """A whitespace-only id must not become a stored blank `run_id`.

        It would also be unaddressable: `RunViewSet` looks Runs up by this
        value in the URL.
        """
        for bad_value in ("", "   ", "\t\n"):
            serializer = RunSerializer(data={"run_id": bad_value})

            assert not serializer.is_valid()
            assert "run_id" in serializer.errors, f"accepted run_id={bad_value!r}"

    def test_blank_agent_is_rejected(self):
        serializer = RunSerializer(data={"agent": ""})

        assert not serializer.is_valid()
        assert "agent" in serializer.errors

    def test_over_max_length_agent_is_rejected(self):
        """Caught in validation, not as a database error at INSERT."""
        serializer = RunSerializer(data={"agent": "a" * 33})

        assert not serializer.is_valid()
        assert "agent" in serializer.errors

    def test_non_string_structures_are_rejected(self):
        for bad_value in ({"nested": "object"}, ["list"], True):
            serializer = RunSerializer(data={"agent": bad_value})

            assert not serializer.is_valid()
            assert "agent" in serializer.errors, f"accepted agent={bad_value!r}"

    def test_status_outside_the_choice_set_is_rejected(self):
        for bad_value in ("bogus", "Running", "", None):
            serializer = RunSerializer(data={"status": bad_value})

            assert not serializer.is_valid()
            assert "status" in serializer.errors, f"accepted status={bad_value!r}"

    def test_malformed_pr_url_is_rejected(self):
        serializer = RunSerializer(data={"pr_url": "not a url"})

        assert not serializer.is_valid()
        assert "pr_url" in serializer.errors

    def test_client_supplied_id_and_timestamps_are_ignored(self):
        """Read-only fields in a payload are dropped, not honoured.

        Asserted against the declared fields rather than through
        `is_valid()` so the check stays database-free.
        """
        for field_name in ("id", "stuck", "created_at", "updated_at"):
            assert RunSerializer().fields[field_name].read_only


class TestRunUpdateSerializerSurface:
    """Requirement 12.4: what `PATCH /api/runs/:id/` may change.

    design.md's REST API table scopes PATCH to "status transitions,
    `pr_url`", which are also the only two fields anything in the spec
    patches: the CLI moving a Run to `awaiting_review` after upload, and
    `vaultshield submit` writing back `pr_url` (Requirement 22.4).
    """

    def test_only_status_and_pr_url_are_writable(self):
        assert _writable_fields(RunUpdateSerializer()) == {"status", "pr_url"}

    def test_identity_fields_cannot_be_reassigned(self):
        """`run_id` is how the CLI addresses a Run; `project`/`agent` describe
        what produced it. None can become untrue after creation."""
        for field_name in ("project", "run_id", "agent"):
            assert RunUpdateSerializer().fields[field_name].read_only

    def test_nothing_is_required_so_any_single_field_may_be_patched(self):
        required = {
            name for name, field in RunUpdateSerializer().fields.items() if field.required
        }

        assert required == set()

    def test_response_shape_matches_the_create_serializer(self):
        """"Return the updated Run" means the same `Run` shape as elsewhere."""
        assert set(RunUpdateSerializer().fields) == set(RunSerializer().fields)


class TestGatedPatchTransitions:
    """Run lifecycle: PATCH moves a Run to `awaiting_review` or `failed` only
    from `running`; the request-level behaviour runs against MongoDB in
    `test_mongodb.py::TestRunStatusTransitions`."""

    def test_the_gated_table_is_completion_and_failure_from_running(self):
        assert GATED_PATCH_TRANSITIONS == {
            "awaiting_review": frozenset({"running"}),
            "failed": frozenset({"running"}),
        }

    def test_every_gated_target_and_source_is_a_run_status(self):
        statuses = {value for value, _label in Run.STATUS_CHOICES}

        for target, sources in GATED_PATCH_TRANSITIONS.items():
            assert target in statuses
            assert sources <= statuses

    def test_a_refused_transition_names_target_allowed_and_current_status(self):
        error = StateTransitionError(
            "approved", target="failed", allowed=GATED_PATCH_TRANSITIONS["failed"]
        )

        assert error.status_code == 409
        assert error.detail["current_status"] == "approved"
        assert str(error.detail["detail"]) == (
            "A Run can only move to `failed` from `running`; this Run is `approved`."
        )

    def test_the_decision_refusal_text_is_unchanged(self):
        """`target=None` keeps the approve/reject messages byte for byte."""
        assert str(StateTransitionError("running").detail["detail"]) == (
            "A decision can only be recorded for a Run in `awaiting_review` "
            "status; this Run is `running`."
        )
        assert str(
            StateTransitionError("awaiting_review", recorded_decision="approved").detail[
                "detail"
            ]
        ) == (
            "This Run already carries a recorded decision (`approved`); "
            "a Run is decided at most once."
        )

    def test_recorded_decision_and_target_are_mutually_exclusive(self):
        with pytest.raises(TypeError, match="recorded_decision or target, not both"):
            StateTransitionError("running", recorded_decision="approved", target="failed")


class TestRunDetailSerializerNesting:
    """Requirement 12.3: detail includes every associated record type."""

    NESTED_FIELDS = {"events", "diff", "screenshots", "security_findings"}

    def test_all_four_associated_record_types_are_nested(self):
        assert self.NESTED_FIELDS <= set(RunDetailSerializer().fields)

    def test_detail_extends_rather_than_replaces_the_run_shape(self):
        assert set(RunDetailSerializer().fields) == set(RunSerializer().fields) | self.NESTED_FIELDS

    def test_nested_collections_are_many_and_the_diff_is_singular(self):
        """Cardinality follows the models: many events, one change set.

        `many=True` builds a `ListSerializer` wrapper, so the distinction is
        visible in the field type: the three `ForeignKey` relations render as
        lists, the reverse one-to-one `diff` as a single object (or `null`
        before it has been ingested).
        """
        fields = RunDetailSerializer().fields

        for field_name in ("events", "screenshots", "security_findings"):
            assert isinstance(fields[field_name], serializers.ListSerializer), field_name
        assert not isinstance(fields["diff"], serializers.ListSerializer)
        assert isinstance(fields["diff"], RunDiffSerializer)

    def test_nested_fields_are_read_only(self):
        """Artifacts are written by the ingest endpoints, not nested here."""
        for field_name in self.NESTED_FIELDS:
            assert RunDetailSerializer().fields[field_name].read_only

    def test_nested_records_expose_what_the_dashboard_tabs_render(self):
        """Each tab's fields, minus the redundant parent `run` pointer."""
        fields = RunDetailSerializer().fields

        assert set(fields["events"].child.fields) == {
            "id",
            "event_type",
            "timestamp",
            "payload",
            "sequence",
        }
        assert set(fields["diff"].fields) == {"id", "files"}
        assert set(fields["screenshots"].child.fields) == {"id", "route", "image"}
        assert set(fields["security_findings"].child.fields) == {
            "id",
            "rule_id",
            "file_path",
            "line_number",
            "description",
        }


class TestRunRouteRegistration:
    """The router exposes the paths and verbs design.md's table specifies."""

    def test_collection_path_matches_the_design_api_surface(self):
        assert reverse("run-list") == "/api/runs/"

    def test_detail_path_is_keyed_by_the_cli_run_id(self):
        """`:id` is the CLI-generated `run_id`, not the database pk.

        design.md's `upload_run` and `submit` both address
        `/api/runs/{run_id}/`, so the reverse has to accept that kwarg.
        """
        assert RunViewSet.lookup_field == "run_id"
        assert reverse("run-detail", kwargs={"run_id": SAMPLE_RUN_ID}) == (
            f"/api/runs/{SAMPLE_RUN_ID}/"
        )

    def test_collection_path_routes_create_and_the_filtered_list(self):
        """`GET /api/runs/` is Requirement 12.6's filtered list (Task 16.10)."""
        match = resolve("/api/runs/")

        assert match.func.cls is RunViewSet
        assert routed_methods(match) == {"post": "create", "get": "list"}

    def test_detail_path_routes_retrieve_and_patch(self):
        match = resolve(f"/api/runs/{SAMPLE_RUN_ID}/")

        assert match.func.cls is RunViewSet
        assert match.func.initkwargs["suffix"] == "Instance"
        assert match.func.actions["get"] == "retrieve"
        assert match.func.actions["patch"] == "partial_update"

    def test_put_and_delete_are_not_available(self):
        """design.md gives `Run` PATCH, not PUT, and never a destroy.

        Deleting a Run would cascade its events, diff, screenshots,
        findings, approval, and sandbox records — the review history the
        dashboard exists to show.
        """
        assert "delete" not in resolve(f"/api/runs/{SAMPLE_RUN_ID}/").func.actions
        assert "put" not in RunViewSet.http_method_names
        assert "delete" not in RunViewSet.http_method_names


class TestRunViewSetSerializerSelection:
    """Each verb gets the serializer its requirement specifies."""

    def _viewset_for(self, action: str) -> RunViewSet:
        viewset = RunViewSet()
        viewset.action = action
        return viewset

    def test_retrieve_nests_the_associated_records(self):
        assert self._viewset_for("retrieve").get_serializer_class() is RunDetailSerializer

    def test_partial_update_narrows_the_writable_fields(self):
        assert self._viewset_for("partial_update").get_serializer_class() is RunUpdateSerializer

    def test_create_uses_the_full_writable_surface(self):
        assert self._viewset_for("create").get_serializer_class() is RunSerializer

    def test_each_ingest_action_uses_its_own_artifact_serializer(self):
        """A `Run` payload must never be what an ingest route validates."""
        assert self._viewset_for("events").get_serializer_class() is RunEventIngestSerializer
        assert self._viewset_for("diff").get_serializer_class() is RunDiffIngestSerializer
        assert (
            self._viewset_for("screenshots").get_serializer_class() is ScreenshotIngestSerializer
        )


class TestIngestRouteRegistration:
    """The three Task 16.7 paths exist, POST-only, under a Run's detail URL."""

    INGEST_PATHS = {"events": "events", "diff": "diff", "screenshots": "screenshots"}

    def test_paths_match_the_design_api_surface(self):
        for action_name, segment in self.INGEST_PATHS.items():
            expected = f"/api/runs/{SAMPLE_RUN_ID}/{segment}/"

            assert reverse(f"run-{action_name}", kwargs={"run_id": SAMPLE_RUN_ID}) == expected

    def test_each_path_routes_post_to_its_own_action(self):
        for action_name, segment in self.INGEST_PATHS.items():
            match = resolve(f"/api/runs/{SAMPLE_RUN_ID}/{segment}/")

            assert match.func.cls is RunViewSet
            assert match.func.actions == {"post": action_name}

    def test_no_read_verb_is_exposed_on_an_ingest_path(self):
        """Every artifact is already readable through `GET /api/runs/:id/`.

        Requirement 12.3 makes that the one endpoint carrying all four
        associated record types, so a per-artifact read route would be a
        second way to the same rows with nothing specifying it.
        """
        for segment in self.INGEST_PATHS.values():
            actions = resolve(f"/api/runs/{SAMPLE_RUN_ID}/{segment}/").func.actions

            assert set(actions) == {"post"}


class TestIngestSerializersTakeTheRunFromTheUrl:
    """No ingest payload may name the Run it attaches an artifact to.

    Every ingest endpoint is a detail route, so the Run is already
    identified by the URL. A writable `run` field would be a second,
    contradictable source for it — a client could POST to one Run's URL and
    file the artifact under another.
    """

    def test_no_ingest_serializer_declares_a_run_field(self):
        for serializer_class in (
            RunEventIngestSerializer,
            RunDiffIngestSerializer,
            ScreenshotIngestSerializer,
        ):
            assert "run" not in serializer_class().fields, serializer_class.__name__


class TestRunEventIngestSerializer:
    """Requirement 13.1: store each submitted event, in submitted order."""

    def test_only_the_event_log_record_fields_are_writable(self):
        """Exactly what `cli/vaultshield/monitor.py` writes per JSONL line.

        So the Uploader (Task 18.2) posts parsed Event_Log lines through
        without translating them.
        """
        assert _writable_fields(RunEventIngestSerializer()) == {
            "event_type",
            "timestamp",
            "payload",
        }

    def test_sequence_is_rendered_but_never_accepted(self):
        """The ordering key is the server's to assign, not a client's to send.

        That is what makes Property 22 hold by construction: a client cannot
        submit a batch whose stored order differs from its submitted order,
        because it has no way to express an order at all. It is still
        rendered, so the response shows what order was recorded.
        """
        sequence_field = RunEventIngestSerializer().fields["sequence"]

        assert sequence_field.read_only
        assert _read_only_fields(RunEventIngestSerializer()) == {"id", "sequence"}

    def test_a_batch_is_handled_by_the_bulk_list_serializer(self):
        """`many=True` has to reach the class that assigns `sequence`."""
        assert isinstance(RunEventIngestSerializer(many=True), RunEventBatchSerializer)

    def test_a_well_formed_batch_validates_in_submitted_order(self):
        serializer = RunEventIngestSerializer(data=SAMPLE_EVENT_BATCH, many=True)

        assert serializer.is_valid(), serializer.errors
        assert [event["payload"] for event in serializer.validated_data] == [
            event["payload"] for event in SAMPLE_EVENT_BATCH
        ]

    def test_an_empty_batch_is_accepted(self):
        """A Run that produced no events stores none, rather than 400-ing."""
        serializer = RunEventIngestSerializer(data=[], many=True)

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data == []

    def test_a_malformed_event_rejects_the_whole_batch(self):
        """Ingest is all-or-nothing, and the error names the bad position.

        DRF keys a list serializer's errors by the index of each offending
        item, which is what a client needs to fix one line of an
        `events.jsonl` batch rather than re-deriving which record failed.
        """
        bad_position = len(SAMPLE_EVENT_BATCH)
        serializer = RunEventIngestSerializer(
            data=[*SAMPLE_EVENT_BATCH, {"event_type": "", "payload": "x"}], many=True
        )

        assert not serializer.is_valid()
        assert set(serializer.errors) == {bad_position}
        assert set(serializer.errors[bad_position]) == {"event_type", "timestamp"}

    def test_event_type_is_not_restricted_to_a_choice_set(self):
        """The Event_Log's vocabulary is the CLI's, not the schema's.

        `RunEvent.event_type` carries no `choices`, and the Monitor emits a
        third kind — `violation`, for an `off_limits_paths` match
        (Requirement 4.5) — beyond the two design.md's inline comment names.
        """
        batch = [{**SAMPLE_EVENT_BATCH[0], "event_type": "violation"}]
        serializer = RunEventIngestSerializer(data=batch, many=True)

        assert serializer.is_valid(), serializer.errors


class TestRunDiffIngestSerializer:
    """Requirement 13.2: store the submitted structured diff for a Run."""

    WELL_FORMED = {
        "files": [
            {"path": "src/app.py", "additions": 12, "deletions": 3},
            # A binary file: `git diff --numstat` prints `-` for both counts
            # and `cli/vaultshield/git_capture.py` records them as 0.
            {"path": "assets/logo.png", "additions": 0, "deletions": 0},
        ]
    }

    def test_files_is_the_only_writable_field(self):
        assert _writable_fields(RunDiffIngestSerializer()) == {"files"}

    def test_the_cli_diff_json_body_validates_unchanged(self):
        """The request body is `diff.json` verbatim, envelope included."""
        serializer = RunDiffIngestSerializer(data=self.WELL_FORMED)

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["files"] == self.WELL_FORMED["files"]

    def test_an_empty_change_set_is_accepted(self):
        """`build_structured_diff` writes `{"files": []}` for a no-op Run.

        Specifically so the Uploader always has a diff to post, and an empty
        change set is what a reviewer needs to see rather than a 400.
        """
        serializer = RunDiffIngestSerializer(data={"files": []})

        assert serializer.is_valid(), serializer.errors

    def test_a_missing_files_key_is_rejected(self):
        serializer = RunDiffIngestSerializer(data={})

        assert not serializer.is_valid()
        assert "files" in serializer.errors

    def test_a_payload_that_is_not_a_list_of_file_stats_is_rejected(self):
        """Rejected here so the scanner and the Diff tab can trust `files`.

        `RunDiff.files` is a `JSONField`, so nothing about the shape
        design.md fixes for it is enforced by the column; accepting
        arbitrary JSON would turn a malformed upload into a Celery
        traceback (Requirement 17.1) or a broken tab (Requirement 20.2).
        """
        for bad_value in ("nope", 7, {"path": "x"}, [["src/app.py", 1, 0]], [None]):
            serializer = RunDiffIngestSerializer(data={"files": bad_value})

            assert not serializer.is_valid(), f"accepted files={bad_value!r}"
            assert "files" in serializer.errors

    def test_an_entry_missing_a_stat_is_rejected(self):
        for bad_entry in (
            {"path": "src/app.py"},
            {"path": "src/app.py", "additions": 1},
            {"additions": 1, "deletions": 0},
        ):
            serializer = RunDiffIngestSerializer(data={"files": [bad_entry]})

            assert not serializer.is_valid(), f"accepted entry={bad_entry!r}"

    def test_negative_line_counts_are_rejected(self):
        """0 is a real value (binary files); negative is not a line count."""
        for bad_entry in (
            {"path": "src/app.py", "additions": -1, "deletions": 0},
            {"path": "src/app.py", "additions": 0, "deletions": -2},
        ):
            serializer = RunDiffIngestSerializer(data={"files": [bad_entry]})

            assert not serializer.is_valid(), f"accepted entry={bad_entry!r}"

    def test_a_blank_path_is_rejected(self):
        """A stat attributed to no file is unrenderable and unscannable."""
        serializer = RunDiffIngestSerializer(
            data={"files": [{"path": "  ", "additions": 1, "deletions": 1}]}
        )

        assert not serializer.is_valid()

    def test_unrecognised_keys_are_dropped_rather_than_stored(self):
        """Only the declared keys (stats, `patch`, `patch_truncated`) are kept."""
        serializer = RunDiffIngestSerializer(
            data={"files": [{"path": "src/app.py", "additions": 1, "deletions": 1, "extra": True}]}
        )

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["files"] == [
            {"path": "src/app.py", "additions": 1, "deletions": 1}
        ]

    # A `-U0` patch as `cli/vaultshield/git_capture.py` captures it, with the
    # whitespace that must survive byte for byte: a leading-space content
    # line, a trailing space, and git's no-newline marker.
    PATCH = (
        "diff --git a/src/app.py b/src/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -3 +3,2 @@\n"
        "-    old = 1 \n"
        "+    new = 2 \n"
        "+\tindented\n"
        "\\ No newline at end of file\n"
    )

    def test_a_patch_and_truncation_flag_are_kept_verbatim(self):
        """Requirements 13.2 and 17.1: the scanner reads the patch as sent."""
        files = [
            {
                "path": "src/app.py",
                "additions": 2,
                "deletions": 1,
                "patch": self.PATCH,
                "patch_truncated": True,
            }
        ]
        serializer = RunDiffIngestSerializer(data={"files": files})

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["files"] == files

    def test_entries_without_a_patch_validate_to_the_three_stat_keys(self):
        """Absent optional keys stay absent — never `None` or `False`."""
        serializer = RunDiffIngestSerializer(data=self.WELL_FORMED)

        assert serializer.is_valid(), serializer.errors
        for entry in serializer.validated_data["files"]:
            assert set(entry) == {"path", "additions", "deletions"}

    def test_an_empty_patch_is_accepted(self):
        """`""` is what the CLI sends once its request budget is spent."""
        files = [
            {"path": "a.py", "additions": 1, "deletions": 0, "patch": "", "patch_truncated": True}
        ]
        serializer = RunDiffIngestSerializer(data={"files": files})

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["files"] == files

    def test_a_flagged_entry_without_a_patch_is_accepted(self):
        """Requirements 13.2 and 17.1: a patch-less flagged entry is what the
        CLI sends for a path that is not valid UTF-8 and so could not be
        handed back to git. It validates as-is, with no `patch` key added.
        """
        files = [
            {"path": "\ufffd.txt", "additions": 1, "deletions": 0, "patch_truncated": True}
        ]
        serializer = RunDiffIngestSerializer(data={"files": files})

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["files"] == files

    def test_a_malformed_patch_or_flag_is_rejected(self):
        """Wrong-typed or null values 400 like the other fields.

        Ints are deliberately absent from the `patch` cases: DRF's
        `CharField` coerces them to strings.
        """
        stat = {"path": "a.py", "additions": 1, "deletions": 0}
        bad_entries = [{**stat, "patch": bad} for bad in (None, ["x"], {"a": 1}, True)]
        bad_entries += [{**stat, "patch_truncated": bad} for bad in ("maybe", [], None)]
        for bad_entry in bad_entries:
            serializer = RunDiffIngestSerializer(data={"files": [bad_entry]})

            assert not serializer.is_valid(), f"accepted entry={bad_entry!r}"
            assert "files" in serializer.errors

    def test_the_rendered_diff_round_trips_the_patch(self):
        """What is stored is what `RunDetailSerializer.diff` renders back.

        Uses an unsaved `RunDiff`, so no database is touched.
        """
        posted = {
            "files": [
                {
                    "path": "src/app.py",
                    "additions": 2,
                    "deletions": 1,
                    "patch": self.PATCH,
                    "patch_truncated": True,
                },
                {"path": "assets/logo.png", "additions": 0, "deletions": 0},
            ]
        }
        serializer = RunDiffIngestSerializer(data=posted)
        assert serializer.is_valid(), serializer.errors

        rendered = RunDiffSerializer(RunDiff(files=serializer.validated_data["files"])).data

        assert rendered["files"] == posted["files"]


class TestScreenshotIngestSerializer:
    """Requirement 13.3: store one uploaded image against a Run."""

    def test_route_and_image_are_the_writable_fields(self):
        assert _writable_fields(ScreenshotIngestSerializer()) == {"route", "image"}

    def test_both_are_required(self):
        """An unattributed image cannot be rendered in the Screenshots tab."""
        serializer = ScreenshotIngestSerializer(data={})

        assert not serializer.is_valid()
        assert set(serializer.errors) == {"route", "image"}

    def test_a_blank_route_is_rejected(self):
        serializer = ScreenshotIngestSerializer(data={"route": "   "})

        assert not serializer.is_valid()
        assert "route" in serializer.errors

    def test_the_upload_is_validated_as_a_decodable_image(self):
        """`Screenshot.image` is an `ImageField`, so Pillow checks the file.

        A corrupt upload is rejected at ingest instead of being discovered
        when the dashboard tries to render it.
        """
        assert isinstance(ScreenshotIngestSerializer().fields["image"], serializers.ImageField)


class TestRunEndpointsRequireADashboardToken:
    """Requirements 27.3-27.5: no endpoint here weakens the global default.

    Task 15.3 asserts the request-level rejection. This is the cheaper
    structural half: `RunViewSet` inherits `settings.REST_FRAMEWORK`'s
    policy rather than declaring its own, which is the only way a viewset
    could opt out of it.
    """

    def test_authentication_and_permission_defaults_are_inherited(self):
        viewset = RunViewSet()

        assert [type(auth) for auth in viewset.get_authenticators()] == [TokenAuthentication]
        assert [type(perm) for perm in viewset.get_permissions()] == [IsAuthenticated]

    def test_the_viewset_declares_no_access_control_of_its_own(self):
        for attribute in ("permission_classes", "authentication_classes"):
            assert attribute not in RunViewSet.__dict__
