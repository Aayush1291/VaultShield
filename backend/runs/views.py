"""Run REST endpoints, artifact ingest, and the approval state machine.

This module ends up carrying most of design.md's REST API surface, added
across several tasks:

    RunViewSet create/detail/patch                     Task 16.2
    events / diff / screenshot ingest                  Task 16.7 (below)
    `status` + `submitted` query-param filtering       Task 16.10
    `apply_decision` plus the approve/reject actions   Task 28.1
    `run_security_scan.delay(...)` on diff ingest      Task 23.2
    `apply_status_transition` for gated PATCH status   Batch 2 (CLI completion / `failed`)
    duplicate `run_id` create race mapped to a 400     Batch 2 (CLI registers the Run)

Serializers live alongside in `runs/serializers.py` (Tasks 16.2, 16.7).
"""

from collections.abc import Iterable
from functools import partial
from typing import Literal

from bson import ObjectId

# NOT `django.db.transaction`. `django-mongodb-backend` reports
# `supports_transactions = False` for Django's native API, which means Django
# quietly turns every `django.db.transaction.atomic()` block into a no-op and
# fires every `on_commit` callback immediately. Nothing raises; the atomicity
# simply disappears. This module's transactions are load-bearing (the
# approve/reject state machine, the all-or-nothing event batch, and the
# scan-after-diff ordering), so they use the backend's own API, which drives
# real MongoDB multi-document transactions over a session.
#
# Requires the deployment to be a replica set or a sharded cluster, which
# MongoDB Atlas and the compose `mongo` service both are. A standalone `mongod`
# cannot run transactions at all.
from django_mongodb_backend import transaction
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.fields import BooleanField
from rest_framework.response import Response

from runs.models import Approval, Run, RunDiff
from vaultshield_backend.mongodb import (
    retry_on_transient_transaction_error,
    to_mongo_precision,
)
from runs.serializers import (
    RUN_ID_UNIQUE_MESSAGE,
    RunDetailSerializer,
    RunDiffIngestSerializer,
    RunEventIngestSerializer,
    RunSerializer,
    RunUpdateSerializer,
    ScreenshotIngestSerializer,
)

# Imported at module level, like `runs/serializers.py` imports
# `security_scan.models`: the dependency runs one way only. `security_scan`
# reads `runs.models`, and `runs.models` imports nothing from `security_scan`,
# so there is no cycle to work around with a deferred in-function import — and
# a module-level import is what lets a test patch `runs.views.run_security_scan`
# by name.
from security_scan.tasks import run_security_scan

# The complete set of Run_Status values a decision can be recorded from
# (Requirements 21.1-21.3). A set rather than an `== "awaiting_review"`
# comparison because design.md names it as the state machine's transition
# table: if review ever gains a second decidable state, this line is what
# changes and `apply_decision` below does not.
ALLOWED_DECISIONS_FROM = frozenset({"awaiting_review"})

# The two values `Approval.decision` accepts, which are also the two
# Run_Status values a decision moves a Run to — Requirement 21.1 approves to
# `approved`, 21.2 rejects to `rejected`. That coincidence is why
# `apply_decision` takes one argument and writes it to both columns rather
# than taking a decision and a target status that could disagree.
Decision = Literal["approved", "rejected"]


class StateTransitionError(APIException):
    """Requirement 21.3: this Run is not in a state that accepts this
    decision or gated status transition.

    409, not 400, per design.md's Error Handling: "`StateTransitionError`
    (approve/reject on a non-`awaiting_review` Run ...) is surfaced as a
    409-style response including the Run's actual current status, never a
    generic 400". The distinction is the whole point of the status code
    here — nothing is wrong with the request, which carries no body to get
    wrong; what is wrong is the state of the Run it names, and that state
    can change without the client doing anything. A 400 would tell a
    reviewer to fix their request, when what they need to know is that the
    Run has already been decided (or has not finished uploading yet).

    ### The body says it in prose and as a value

    `detail` is a sentence explaining the refusal, because the dashboard's
    API client renders `payload.detail` verbatim as its one-line error
    message (`describeError` in `frontend/src/api.js`), so anything not in
    that string is invisible to a reviewer. `current_status` carries the
    Run_Status as a bare value alongside it, so Task 28.3's buttons can
    react to it — re-rendering the Run as already-approved, say — without
    parsing English out of a sentence.

    That value is the status as *stored*, re-read by `apply_decision` rather
    than taken from the instance the request loaded: the case this error most
    often describes is a second decision arriving after a first one landed,
    and reporting the status the loser read on the way in would name the
    state the Run has just stopped being in.
    """

    status_code = status.HTTP_409_CONFLICT

    def __init__(
        self,
        current_status: str,
        *,
        recorded_decision: str | None = None,
        target: str | None = None,
        allowed: Iterable[str] = (),
    ):
        """`current_status` is the Run's stored Run_Status.

        `recorded_decision` names an `Approval` that already exists for the
        Run, for the one case where the status alone does not explain the
        refusal — see `apply_decision` on how a decided Run can be back in
        `awaiting_review`.

        `target` and `allowed` describe a refused gated PATCH transition
        (`apply_status_transition`): the status the client asked for and
        the statuses it may be reached from, passed in from
        `GATED_PATCH_TRANSITIONS` so the message cannot drift from the
        rule. `target` and `recorded_decision` describe different refusals
        and are mutually exclusive; passing both is a programming error,
        raised as `TypeError` so the check survives `python -O`.
        """
        if recorded_decision is not None and target is not None:
            raise TypeError("StateTransitionError takes recorded_decision or target, not both")

        if target is not None:
            allowed_text = ", ".join(f"`{s}`" for s in sorted(allowed))
            message = (
                f"A Run can only move to `{target}` from {allowed_text}; "
                f"this Run is `{current_status}`."
            )
        elif recorded_decision is None:
            message = (
                "A decision can only be recorded for a Run in `awaiting_review` "
                f"status; this Run is `{current_status}`."
            )
        else:
            message = (
                f"This Run already carries a recorded decision (`{recorded_decision}`); "
                "a Run is decided at most once."
            )

        super().__init__({"detail": message, "current_status": current_status})


def apply_decision(run: Run, decision: Decision) -> Run:
    """Move `run` to `decision` and record the `Approval`, or refuse.

    The state machine behind `POST /api/runs/:id/approve/` and
    `POST /api/runs/:id/reject/` (Task 28.1), and the whole of Requirement
    21.1-21.3: a Run in `awaiting_review` takes the decision and gains an
    `Approval` row, a Run in any other status is refused with a
    `StateTransitionError` naming the status it is actually in.

    Returns the same instance it was given, with `status`/`updated_at`
    updated in memory to match the row, so a caller can serialize it
    without a re-read.

    ### The gate is the write, not a check before it

    design.md spells the transition as an `if` on `run.status` followed by
    `run.save(...)`. This performs both in one conditional `UPDATE`
    (`filter(status__in=ALLOWED_DECISIONS_FROM).update(...)`), which is the
    same rule — the row changes only from `awaiting_review` — with the read
    and the write closed up.

    That matters because approve and reject are two endpoints acting on one
    row, and the dashboard puts both buttons in front of the same reviewer.
    With a separate check, two decisions arriving together both read
    `awaiting_review`, both pass, both write a status, and then the second
    `Approval.objects.create` hits the one-to-one column and fails — an
    `IntegrityError`, which is a 500 telling a reviewer nothing about a
    situation that has a perfectly good answer. As a conditional `UPDATE`
    only one of the two can match a row: the winner proceeds to its
    `Approval`, and the loser finds zero rows updated and gets the same 409
    it would have got had it arrived a second later. Requirement 21.3 is
    then true of concurrent requests, not just sequential ones.

    `updated_at` is passed explicitly because a queryset `UPDATE` does not
    run the field's `auto_now`. Its value is the same instant the decision
    is timestamped with, and it is load-bearing beyond bookkeeping:
    `flag_stuck_approvals` (Task 33.2) selects approved Runs by
    `updated_at` age, so a decision that left this column untouched would
    let a Run be flagged stuck for time it spent awaiting review.

    ### An already-decided Run is refused before the row is touched

    The status gate alone would be enough if `status` could only ever move
    forward, but `PATCH /api/runs/:id/` still accepts `running` from any
    status (only `awaiting_review` and `failed` are gated, see
    `GATED_PATCH_TRANSITIONS` and `RunUpdateSerializer`), so a decided Run
    can be put back into `awaiting_review` in two steps: PATCH `running`,
    then PATCH `awaiting_review`. `Approval.run` is a `OneToOneField` — the
    schema's own statement that a Run is decided at most once — so a second
    decision on that Run would raise `IntegrityError` from `create` below.
    Checking for the existing `Approval` first turns that 500 into a 409
    that says which decision is already on file, and keeps the schema's
    guarantee and the API's behaviour saying the same thing.

    The whole sequence runs in one transaction, so a refusal at either gate
    leaves the Run exactly as it was, and a Run never ends up carrying a
    decided status with no `Approval` behind it.

    No lock is taken, and none is needed. The conditional `UPDATE` already
    serialises the only race there is here: MongoDB applies an update to a
    single document atomically, so of two concurrent decisions exactly one
    matches `status__in=ALLOWED_DECISIONS_FROM` and the other sees zero rows
    modified. This is the one PostgreSQL correctness mechanism in the
    codebase that carried over to MongoDB unchanged — verified in
    `vaultshield_backend/test_mongodb.py::TestApprovalAtomicity`.

    The `atomic()` here is `django_mongodb_backend`'s, not Django's; see the
    import comment at the top of this module for why that distinction is
    load-bearing rather than cosmetic.
    """
    # Truncated to MongoDB's millisecond storage precision before it is written
    # anywhere. This function stores `decided_at` and then assigns the same
    # value to `run.updated_at` in memory so the caller can serialise without a
    # re-read; if the two differed by the microseconds MongoDB drops, the
    # approve/reject response would report a different `updated_at` than every
    # later `GET /api/runs/:id/` for the same unchanged row. See
    # `vaultshield_backend/mongodb.py`.
    decided_at = to_mongo_precision(timezone.now())

    def record_decision() -> None:
        """One attempt at the transition. Re-read on every retry.

        Everything it decides on is read inside the block, which is what makes
        a retry converge instead of repeating a stale answer: a caller that
        lost the write race and comes back round finds the winner's `Approval`
        already committed and takes the refusal path.
        """
        with transaction.atomic():
            recorded_decision = (
                Approval.objects.filter(run=run).values_list("decision", flat=True).first()
            )
            if recorded_decision is not None:
                raise StateTransitionError(
                    _stored_status_of(run), recorded_decision=recorded_decision
                )

            transitioned = Run.objects.filter(
                pk=run.pk, status__in=ALLOWED_DECISIONS_FROM
            ).update(status=decision, updated_at=decided_at)

            if not transitioned:
                raise StateTransitionError(_stored_status_of(run))

            Approval.objects.create(run=run, decision=decision)

    # Retried on MongoDB's `TransientTransactionError` only, and this is the one
    # place in the codebase that needs it. Concurrent decisions all write the
    # *same* Run document inside a transaction, so MongoDB aborts the losers
    # with a write conflict and expects the client to retry — unlike
    # PostgreSQL, where the conditional `UPDATE` blocked briefly and then
    # reported zero rows matched.
    #
    # Without the retry the guard still held (exactly one caller ever won), but
    # a losing reviewer got a 500 instead of Requirement 21.3's 409 naming the
    # status the Run is actually in. Measured at 54 spurious errors out of 72
    # losing calls before this was added.
    #
    # `StateTransitionError` is not retried: it is a real answer about the
    # Run's state, not a race. See
    # `vaultshield_backend/mongodb.retry_on_transient_transaction_error`.
    retry_on_transient_transaction_error(record_decision)

    run.status = decision
    run.updated_at = decided_at

    return run


def _stored_status_of(run: Run) -> str:
    """The Run's Run_Status as currently stored, for a refusal message.

    A fresh read rather than `run.status`, because the reason a decision was
    refused is usually that the stored status is no longer what this request
    read on the way in.

    `values_list(...).first()` rather than `refresh_from_db()`: a Run that
    has disappeared between the two statements returns `None` here instead
    of raising `Run.DoesNotExist`, and an error path is the last place worth
    replacing a 409 with a 500. Nothing routed deletes a Run — `RunViewSet`
    has no `DELETE` — so the fallback is for a hand-deleted row, and the
    last status this request saw is the best answer available for one.
    """
    stored_status = Run.objects.filter(pk=run.pk).values_list("status", flat=True).first()

    return stored_status or run.status


# Run_Status values PATCH may only move a Run to from `running`. The two the
# CLI itself writes: completion and failure. Any other `status` value keeps
# today's unrestricted PATCH behaviour (see RunUpdateSerializer).
GATED_PATCH_TRANSITIONS = {
    "awaiting_review": frozenset({"running"}),
    "failed": frozenset({"running"}),
}


def apply_status_transition(run: Run, target: str, other_fields: dict) -> Run:
    """Move `run` to a gated `target` status, or refuse with a 409.

    The Run lifecycle's two CLI-written transitions through
    `PATCH /api/runs/:id/` (Requirement 12.4): `running -> awaiting_review`
    once the CLI's upload completes, and `running -> failed` when a
    registered Run errors. `target` must be a key of
    `GATED_PATCH_TRANSITIONS`; a Run whose stored status is not in that
    entry is refused with `StateTransitionError` (409, `detail` +
    `current_status`), the same response `apply_decision` gives, and its
    row is left exactly as it was.

    `other_fields` are the PATCH's other validated fields (in practice only
    `pr_url`). They are written in the same conditional update, so a
    refused transition writes nothing at all.

    Returns the same instance, with `status`, `updated_at` and
    `other_fields` updated in memory to match the row.

    ### The gate is the write, as in `apply_decision`

    One conditional `filter(pk=..., status__in=allowed).update(...)` inside
    `django_mongodb_backend`'s `transaction.atomic()`, retried only on
    MongoDB's `TransientTransactionError`. Of two concurrent writers from
    the same source state (completion vs failure, or either vs a decision,
    which needs `awaiting_review`) exactly one matches; the other finds zero
    rows updated, re-reads the stored status, and gets the 409. That is why
    `submitted`, `approved`, `rejected`, `awaiting_review` and `failed` can
    never become `failed`: the filter excludes them.

    `updated_at` is passed explicitly (a queryset `update` skips
    `auto_now`) and truncated to MongoDB's millisecond precision, so the
    response reports the value every later `GET` will read.
    """
    updated_at = to_mongo_precision(timezone.now())
    allowed = GATED_PATCH_TRANSITIONS[target]

    def transition() -> None:
        """One attempt at the transition. Re-reads on every retry."""
        with transaction.atomic():
            moved = Run.objects.filter(pk=run.pk, status__in=allowed).update(
                status=target, updated_at=updated_at, **other_fields
            )
            if not moved:
                raise StateTransitionError(
                    _stored_status_of(run), target=target, allowed=allowed
                )

    retry_on_transient_transaction_error(transition)

    run.status = target
    run.updated_at = updated_at
    for name, value in other_fields.items():
        setattr(run, name, value)

    return run


class RunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """`POST /api/runs/`, the filtered `GET /api/runs/`, and the detail routes.

    Task 16.2, covering Requirements 12.2 (create a Run for an existing
    Project), 12.3 (full detail including every associated artifact), 12.4
    (partial update), and 12.5 (reject a malformed create), with the
    validation itself in `runs/serializers.py`.

    ### `:id` is the CLI's `run_id`, not the database primary key

    `lookup_field = "run_id"` because design.md's CLI signatures address
    Runs by the id the CLI generated: `upload_run` posts to
    `/api/runs/{run_id}/events/` and `submit` PATCHes `/api/runs/{run_id}/`.
    That is also why `Run.run_id` is `unique=True` and why
    `run_security_scan` resolves a Run by `run__run_id`. The CLI never has
    to learn the database pk of a row it created, so nothing on the CLI
    side has to hold a second identifier for a Run it already named.

    The generated `run_id` format (`<YYYYmmdd>-<HHMMSS>-<16 hex>`) contains
    no `/` and no `.`, so it fits Django's default detail-lookup pattern
    without a custom `lookup_value_regex`. Create now enforces that for
    every client-supplied id: `RunSerializer` validates the CLI's safe
    path-component grammar — see "`run_id` format" in
    `runs/serializers.py` for why, and why that grammar.

    ### Which operations are routed, and which are not

    Create, retrieve, and update, plus the three artifact-ingest detail
    actions (Task 16.7) and the two decision actions (Task 28.1), so the
    routed URLs match design.md's REST API table rather than exposing
    operations nothing specified:

    - **No `DELETE`.** Deleting a Run cascades its events, diff,
      screenshots, findings, approval, and sandbox records. `vaultshield
      teardown` (Requirement 23) removes the Sandbox and a rejected Run's
      remote branch; it does not delete the Run record, and the review
      history is the point of the dashboard.
    - **No `PUT`.** `UpdateModelMixin` supplies both `update` and
      `partial_update`, and the router binds each to its verb, so PUT is
      excluded via `http_method_names` below instead of by dropping the
      mixin. design.md specifies PATCH only, and full replacement of a Run
      is not a meaningful operation when identity fields are immutable.
    - **`GET /api/runs/` is Requirement 12.6's filtered collection.**
      `?status=...&submitted=...`, applied in `get_queryset` below (Task
      16.10). It renders `RunSerializer` rather than
      `RunDetailSerializer`: a page of up to `PAGE_SIZE` Runs each
      carrying every event, screenshot, and finding it owns would be a
      large multiple of what the requirement asks for, and Requirement
      12.3 already names the detail endpoint as where the associated
      records are read.
    - **No `GET` on the ingest sub-routes.** `events`, `diff`, and
      `screenshots` accept `POST` only. Each artifact is already readable
      through `GET /api/runs/:id/`, which Requirement 12.3 defines as the
      single endpoint carrying all four associated record types, so a
      per-artifact read endpoint would be a second way to fetch the same
      rows with nothing specifying it.

    ### Why ingest and the decisions are actions here, not their own views

    design.md puts them here ("`views.py` — ProjectViewSet, RunViewSet,
    ingest + approve/reject actions"), and the shape of the work agrees:
    all five are detail routes under a Run, so all five need exactly what
    `GenericViewSet` already provides — `get_object()` resolving `run_id`
    from the URL against the same queryset, a 404 for an unknown Run before
    any write, and the inherited authentication and permission policy.
    Standalone `APIView`s would each restate that.

    Every ingest action follows the same three steps: resolve the Run from
    the URL, validate the payload, then `serializer.save(run=run)`. `run` is
    passed in rather than accepted from the body so the URL is the only
    thing that can decide which Run an artifact attaches to. `approve` and
    `reject` share the first step and skip the other two — a decision has no
    payload to validate, because which URL was posted to *is* the decision.
    The transition they perform is `apply_decision` above, not a method on
    this class: design.md specifies it as a standalone state machine over a
    `Run`, and keeping it free of `self` is what lets Task 28.2's property
    test drive it directly over generated statuses without a request.

    ### Access control

    No `permission_classes` or `authentication_classes` here on purpose.
    `settings.REST_FRAMEWORK` sets `TokenAuthentication` + `IsAuthenticated`
    project-wide (Task 15.2), and Requirement 27.5 names run creation,
    `PATCH /api/runs/:id/`, event/diff/screenshot ingest, approve, and
    reject specifically, so every endpoint on this class requires a valid
    `Dashboard_Auth_Token` by inheritance — including the `@action` methods,
    which DRF runs through the same `APIView.initial()` check as the routed
    mixins.
    Restating the defaults would be harmless; overriding them is what would
    break the requirement, so this class stays silent and inherits.
    """

    # No `queryset` class attribute: Task 16.10 needs the base queryset
    # narrowed per request, so it is built in `get_queryset()` below. The
    # router registration in `vaultshield_backend/urls.py` passes
    # `basename="run"` explicitly, which is what keeps the `run-list` /
    # `run-detail` reverse names working without an attribute for DRF to
    # derive them from.
    #
    # The default for any action `get_serializer_class` does not special-case
    # (create, list, the two decision actions, and DRF's own `OPTIONS`
    # metadata).
    serializer_class = RunSerializer
    lookup_field = "run_id"
    # Drops `PUT` (405) while keeping `UpdateModelMixin`'s `partial_update`.
    # `head` and `options` are Django/DRF's own — `head` so the detail route
    # answers a HEAD like the GET it mirrors, `options` so DRF can serve the
    # endpoint metadata the browsable API reads.
    http_method_names = ["get", "post", "patch", "head", "options"]

    # Which serializer each Task 16.7 ingest action validates against, keyed
    # by `self.action` (which for an `@action` is its method name). A mapping
    # rather than three `if` branches in `get_serializer_class` because the
    # action name, the URL segment, and the artifact are one-to-one-to-one
    # here, so the whole ingest surface is legible as a table.
    INGEST_SERIALIZERS = {
        "events": RunEventIngestSerializer,
        "diff": RunDiffIngestSerializer,
        "screenshots": ScreenshotIngestSerializer,
    }

    # What `?submitted=` accepts, borrowed from DRF's own `BooleanField`
    # rather than restated as `{"true", "false"}` so this query parameter
    # spells a boolean exactly the way a request body does: `false`, `0`,
    # `no`, and `off` all read as false, in any case. Requirement 12.6
    # names `true`/`false`; accepting the rest costs nothing and surprises
    # no one, while a hand-rolled pair of literals would make this the one
    # boolean in the API with its own vocabulary.
    # Copied into frozensets rather than aliased: DRF's own sets are
    # mutable and shared by every `BooleanField` in the process.
    SUBMITTED_TRUE_VALUES = frozenset(BooleanField.TRUE_VALUES)
    SUBMITTED_FALSE_VALUES = frozenset(BooleanField.FALSE_VALUES)

    def get_queryset(self):
        """Every Run, narrowed by `status`/`submitted` (Requirement 12.6)
        and by `project`.

        The three filters are independent and all optional, so applying each
        only when its parameter is present is what makes every combination
        behave: `status` alone, `submitted` alone either way, `project`
        scoping the page to one Project's Runs, any pair together (design.md's
        `?status=approved&submitted=false`), all three, and none, which is
        the unfiltered page. `project` is an equality on the `project_id` the
        row already carries, so it composes with the other two as a further
        `AND` and changes neither the ordering nor the pagination.

        `submitted` is derived rather than stored — "has this Run been
        submitted" is `pr_url != ""` — which is why `Run.pr_url` defaults to
        the empty string instead of being nullable. Over a non-nullable
        column `exclude(pr_url="")` and `filter(pr_url="")` are exact
        complements, so no Run is missing from both answers and none appears
        in both. Note that the derivation is deliberately not
        `status="submitted"`: a Run marked submitted whose `pr_url` never
        got written back is still unsubmitted by this filter, which is the
        discrepancy `flag_stuck_approvals` exists to surface rather than one
        to paper over here.

        ### Only `list` filters

        `get_queryset` is also the queryset `get_object()` resolves
        `run_id` against, so without this guard a stray `?submitted=true`
        would turn an existing Run's detail URL into a 404, and the ingest
        actions would start depending on the query string of a request whose
        Run the path already names.

        ### An unreadable `submitted` is a 400; an unknown `status` is not

        A `status` value outside `Run.STATUS_CHOICES` matches no row, so it
        fails closed — an empty page is the true answer to what was asked,
        and there is nothing to guess. An unrecognised `submitted` has no
        such reading: dropping it would answer a *wider* question than the
        client asked, and the caller design.md names for this endpoint is
        `vaultshield submit` polling for approved Runs that have no PR yet.
        Quietly handing that poll Runs which already carry a `pr_url` is how
        one change set gets a second pull request, so the request is
        rejected instead.

        A blank value (`?status=&submitted=&project=`) counts as not supplied
        for all three: a query string assembled from empty inputs is
        expressing no filter, and `status=""` matches no row in any case.

        An unparseable `project` is rejected like an unreadable `submitted`:
        a Project pk is a MongoDB ObjectId, so a value that is not one can
        name no Project and cannot be looked up, and `_parse_project` turns
        it into the same 400 rather than a driver-level 500. A *valid* id
        that names no Project is not an error — it matches no row and the
        page is empty.
        """
        queryset = Run.objects.all()

        if self.action != "list":
            return queryset

        status_value = self.request.query_params.get("status", "").strip()
        if status_value:
            queryset = queryset.filter(status=status_value)

        submitted_value = self.request.query_params.get("submitted", "").strip()
        if submitted_value:
            if self._parse_submitted(submitted_value):
                queryset = queryset.exclude(pr_url="")
            else:
                queryset = queryset.filter(pr_url="")

        project_value = self.request.query_params.get("project", "").strip()
        if project_value:
            queryset = queryset.filter(project_id=self._parse_project(project_value))

        # `Run.Meta.ordering` is `-created_at`, so the paginated page split
        # is stable without ordering again here. No `select_related`:
        # `RunSerializer` renders `project` as a pk, which is `project_id`
        # on the row already, and it nests no artifacts — so a page of 50
        # Runs is one query regardless.
        return queryset

    def _parse_submitted(self, raw_value: str) -> bool:
        """`?submitted=` as a boolean, or a 400 naming the parameter.

        Raised as a DRF `ValidationError` so it is rendered by the same
        exception handler, in the same `{"field": [...]}` shape, as a
        rejected `POST /api/runs/` body (Requirement 12.5) — a client that
        can already read one error format does not need a second.
        """
        lowered = raw_value.lower()

        if lowered in self.SUBMITTED_TRUE_VALUES:
            return True
        if lowered in self.SUBMITTED_FALSE_VALUES:
            return False

        raise ValidationError(
            {
                "submitted": (
                    "Must be a boolean: `true` for Runs with a pr_url, "
                    "`false` for Runs without one."
                )
            }
        )

    def _parse_project(self, raw_value: str) -> str:
        """`?project=` as a Project pk, or a 400 naming the parameter.

        A Project's pk is a MongoDB ObjectId (`ObjectIdAutoField`), so a
        value that is not a 24-character hex string can match no Project and
        is not a thing the database can look up at all — filtering on it
        raises deep in the driver rather than returning an empty page. This
        rejects it up front with the same DRF `ValidationError` shape
        (`{"project": [...]}`) a rejected `POST /api/runs/` body gets, so a
        malformed id is a 400 a client can read rather than a 500.

        A syntactically valid id that names no Project is *not* an error:
        `ObjectId.is_valid` passes it, the `filter(project_id=...)` matches
        no row, and the caller gets an empty page — the true answer to
        "which Runs belong to this project" when the project is gone.
        """
        if ObjectId.is_valid(raw_value):
            return raw_value

        raise ValidationError(
            {"project": ["Must be a valid project id."]}
        )

    def get_serializer_class(self):
        """Pick the serializer for the action: detail nests, PATCH narrows,
        each ingest route takes its own artifact.

        Requirement 12.3 wants the nested artifacts on retrieve, and only
        on retrieve — a create, update, or filtered-list response that also
        carried every event of a Run would be echoing data the client did
        not ask about, which is why `list` falls through to the flat
        `RunSerializer` below. Requirement 12.4's write surface is narrower
        still, so PATCH uses the serializer that makes the identity fields
        read-only.

        `update` is listed alongside `partial_update` even though `PUT` is
        405 here: if `http_method_names` is ever widened, the write surface
        should widen with it deliberately rather than as a side effect.

        The three ingest actions are dispatched from a mapping rather than
        by each action calling its serializer directly, so `get_serializer`
        keeps working inside them — which is what supplies the request in
        the serializer context, and therefore what makes an ingested
        screenshot's `image` render as an absolute URL in the response.

        `approve` and `reject` fall through to `RunSerializer`, which is
        what they render the decided Run with. Neither reads a request
        body, so nothing here validates one: the decision is carried by
        which URL was posted to, not by a payload — see `_record_decision`.
        """
        if self.action == "retrieve":
            return RunDetailSerializer
        if self.action in {"update", "partial_update"}:
            return RunUpdateSerializer
        if self.action in self.INGEST_SERIALIZERS:
            return self.INGEST_SERIALIZERS[self.action]
        return RunSerializer

    def perform_create(self, serializer):
        """Save a new Run; a lost duplicate-`run_id` race is a 400, not a 500.

        Requirements 12.2 and 12.5. `RunSerializer`'s `UniqueValidator`
        answers a sequential duplicate with a 400 before any write, but two
        concurrent creates for one `run_id` can both pass it, and the second
        `INSERT` then hits the column's unique index as an `IntegrityError`.
        That is mapped to the validator's own body
        (`{"run_id": [RUN_ID_UNIQUE_MESSAGE]}`), so a client cannot tell the
        two paths apart.

        Only when a Run with that `run_id` now exists: any other integrity
        failure is re-raised unchanged and stays a loud 500. The re-check is
        a plain read outside any transaction (create runs in none), and
        `CreateModelMixin.create` still builds the 201 response.
        """
        try:
            serializer.save()
        except IntegrityError:
            if Run.objects.filter(run_id=serializer.validated_data["run_id"]).exists():
                raise ValidationError({"run_id": [RUN_ID_UNIQUE_MESSAGE]})
            raise

    def partial_update(self, request, *args, **kwargs):
        """`PATCH /api/runs/:id/`, with `awaiting_review`/`failed` gated.

        Requirement 12.4. The payload is validated by `RunUpdateSerializer`
        first, so an unknown Run is a 404 and a bad value a 400 exactly as
        before. A `status` in `GATED_PATCH_TRANSITIONS` (the CLI's completion
        and failure signals) goes through `apply_status_transition`, which
        accepts it only from `running` and otherwise answers 409 with the
        stored status, writing nothing. Every other PATCH — `pr_url` alone,
        or an ungated `status` — takes the unchanged update path.

        That path is `UpdateModelMixin.update`'s tail copied rather than
        `super().partial_update(...)`, because calling super would resolve
        the Run and validate the payload a second time; the observable
        behaviour is the same.
        """
        run = self.get_object()
        serializer = self.get_serializer(run, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        target = serializer.validated_data.get("status")
        if target not in GATED_PATCH_TRANSITIONS:
            self.perform_update(serializer)
            return Response(serializer.data)
        other_fields = {k: v for k, v in serializer.validated_data.items() if k != "status"}
        apply_status_transition(run, target, other_fields)
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"], url_path="events")
    def events(self, request, run_id=None):
        """`POST /api/runs/:id/events/` — bulk ingest of an event batch.

        Requirement 13.1: store each submitted event as a `RunEvent` and
        preserve the batch's original chronological order. The body is a
        bare JSON array, so `many=True` builds
        `RunEventBatchSerializer`, which assigns each event's `sequence`
        from its position in that array — see it for why order is carried by
        a server-assigned field rather than a submitted one.

        ### How concurrent batches are serialised without a row lock

        `sequence` numbering continues past whatever this Run has already
        been allocated, so ingest used to be a read-modify-write: read
        `MAX(sequence)`, insert past it. Two concurrent batches would both
        read the same maximum and assign overlapping numbers, and because
        retrieval orders by `sequence` alone the two batches would come
        back interleaved — precisely what Requirement 13.1 rules out.
        PostgreSQL solved that with `SELECT ... FOR UPDATE` on the Run row.

        MongoDB has no row lock, and `select_for_update()` is *silently
        ignored* by `django-mongodb-backend` rather than raising, so keeping
        that code would have left the race wide open with nothing to
        indicate it. The read-modify-write is gone instead: the batch's
        numbers come from one atomic `$inc` on the Run document
        (`runs/sequences.allocate_event_sequences`), which MongoDB
        serialises per document. A `UniqueConstraint(run, sequence)` on
        `RunEvent` backs it up, so a duplicate would be a loud
        `IntegrityError` rather than a quietly reordered log.

        The transaction is still what makes a batch all-or-nothing. A failed
        ingest stores none of its events, so the Uploader's retry from the
        same local artifacts (Requirement 14.4) starts from a clean slate
        rather than duplicating a half-written batch. Note that a retry gets
        a *new* block of sequence numbers, leaving a gap where the abandoned
        block was — harmless, since ordering is relative.

        Note what this does not claim: a batch whose response was lost
        *after* it committed will duplicate its events if re-posted. Events
        have no natural key — two identical output chunks a second apart are
        two real events — so nothing here can tell a retry from a genuine
        repeat, and inventing a de-duplication rule would risk discarding
        real events. Nothing in the spec asks for idempotent event ingest.
        """
        run = self.get_object()
        serializer = self.get_serializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            # No re-fetch under a lock: `get_object()` above has already
            # resolved and authorised this Run, and the ordering guarantee no
            # longer comes from holding it still. The allocator inside
            # `serializer.save` is what serialises concurrent batches.
            serializer.save(run=run)

        # The stored events, in stored order, each carrying the `sequence`
        # the server assigned — so a client can see the order it was
        # recorded in without a follow-up read.
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="diff")
    def diff(self, request, run_id=None):
        """`POST /api/runs/:id/diff/` — store a Run's structured diff.

        Requirement 13.2: store the submitted diff as a `RunDiff`
        associated with that Run. The body is the CLI's `diff.json`
        verbatim (`{"files": [...]}`).

        `RunDiff.run` is a `OneToOneField`, so a repeat POST replaces the
        stored change set rather than adding a second one — the reasoning is
        on `RunDiffIngestSerializer.create`. The two outcomes are
        distinguished here instead of there: 201 when this Run had no diff
        yet, 200 when an existing one was replaced. The existence check runs
        inside the same transaction as the write so the status code cannot
        describe a state that changed underneath it.

        ### The security scan is enqueued here, and only here

        Requirement 18.1: the Security_Scan_Service is triggered without a
        manual scan request. This view is the only place that knows a Run's
        change set has landed and is therefore scannable — which is why
        design.md calls it "the diff-ingest view" and why the MVP has no
        manual trigger endpoint to pair with it. `run_security_scan`
        (Task 22.3) does the scanning; all that happens here is the
        `.delay(run_id)` that hands it off, so a slow scan never holds up
        the upload's response.

        `transaction.on_commit` rather than a bare `.delay()` inside the
        block. A worker can begin the task the instant it is published, and
        until this transaction commits the `RunDiff` is not visible to any
        other session — so an early task finds no diff, takes
        `run_security_scan`'s `DoesNotExist` no-op branch, and returns,
        leaving the Run with an empty Security tab and nothing logged as
        having failed. Registering on commit is what makes that branch mean
        "the diff is gone" (a Run torn down between enqueue and execution),
        as its docstring claims, rather than "the diff is not committed
        yet". It also ties the enqueue to the write: if this block rolls
        back, the callback is discarded with it, so no scan is ever queued
        for a diff that was not stored.

        This is `django_mongodb_backend.transaction.on_commit`, and the
        distinction matters here more than anywhere else in the codebase.
        Django's own `on_commit` fires the callback *immediately* on a
        backend that reports no transaction support, which is exactly the
        early-task race described above — the scan would be enqueued before
        the diff document was committed, and the Security tab would come up
        silently empty. Requirement 18.1 depends on this import being the
        right one; `vaultshield_backend/test_mongodb.py::TestSecurityScanOrdering` pins it.

        Celery's `delay_on_commit` is not used: it routes through Django's
        native `on_commit`, so it would reintroduce the same problem.

        A replaced diff enqueues a second scan, which is what should
        happen: `run_security_scan` replaces that Run's findings rather
        than adding to them, so the stored findings keep describing the
        change set currently stored for the Run.

        `run.run_id` is passed, not `run.pk` — the task resolves a Run by
        `run__run_id`, and `run_id` is the identifier already in this URL.
        """
        run = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            replaced = RunDiff.objects.filter(run=run).exists()
            serializer.save(run=run)
            transaction.on_commit(partial(run_security_scan.delay, run.run_id))

        return Response(
            serializer.data,
            status=status.HTTP_200_OK if replaced else status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="screenshots")
    def screenshots(self, request, run_id=None):
        """`POST /api/runs/:id/screenshots/` — upload one screenshot.

        Requirement 13.3: store the uploaded image as a `Screenshot`
        associated with the Run, and make it retrievable through the Run
        detail endpoint. The second half needs no code here —
        `RunDetailSerializer` nests every `Screenshot` pointing at the Run,
        so a stored row is retrievable the moment it exists.

        One image per request, multipart (`route` plus `image`), matching
        design.md's REST API surface and how `upload_run` posts a Run's
        captures: one POST per captured route.

        No transaction wrapper, unlike the two above: this is a single
        `INSERT` with nothing read beforehand, so there is no
        read-modify-write to protect and Django's autocommit already makes
        it atomic. The response carries the stored row, including `image`
        as the URL the dashboard will load it from.
        """
        run = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(run=run)

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, run_id=None):
        """`POST /api/runs/:id/approve/` — Requirement 21.1.

        Sets the Run's Run_Status to `approved` and records an `Approval`,
        for a Run in `awaiting_review`; refuses anything else with
        Requirement 21.3's state-transition error. Both halves are
        `apply_decision`'s.
        """
        return self._record_decision("approved")

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, run_id=None):
        """`POST /api/runs/:id/reject/` — Requirement 21.2.

        The same transition as `approve` with the other decision, which is
        why both are one line over a shared helper: the two requirements
        differ only in the value recorded, and a reject path that had drifted
        from the approve path would be a gap in the same state machine.
        """
        return self._record_decision("rejected")

    def _record_decision(self, decision: Decision) -> Response:
        """Resolve the Run from the URL, apply `decision`, return the Run.

        ### The decision is in the path, never in the body

        Two URLs rather than one endpoint taking `{"decision": ...}`,
        because that is design.md's REST API surface (`/approve/` and
        `/reject/` are separate rows in it) and because it leaves nothing
        about a decision for a request body to get wrong — no missing field,
        no unrecognised value, no third decision to validate against.
        Anything posted as a body is ignored.

        ### The response is the decided Run, at 200

        Requirement 21 does not fix a response shape, and an empty `204`
        would be defensible. The updated Run is returned instead because of
        what happens next in the dashboard: Requirement 21.4 has the detail
        view re-render as "approved — awaiting submission" the moment a
        decision lands, so the alternative is every decision costing a
        second `GET /api/runs/:id/` to learn a status this request just
        decided. `RunSerializer`, not `RunDetailSerializer` — a decision
        changes one field, and re-sending every event, screenshot, and
        finding of the Run to report it would be a large response saying
        very little.

        Nothing here catches `StateTransitionError`: it is an
        `APIException`, so DRF's exception handler renders it as the 409 in
        the same `{"detail": ...}` family as every other error this API
        returns. Requirement 21.3's refusal needs no code in this method.

        Authentication and permissions are the class-wide defaults
        (`TokenAuthentication` + `IsAuthenticated` from `settings`), which
        `@action` methods go through exactly as the routed mixins do —
        Requirement 27.5 names approve and reject specifically, so the point
        of this method is what it does *not* declare.
        """
        # `get_object()` first, so an unknown `run_id` is a 404 before any
        # decision logic runs, and so the Run this decides is the one the
        # URL names — the same resolution the ingest actions above use.
        run = self.get_object()
        apply_decision(run, decision)

        return Response(self.get_serializer(run).data)
