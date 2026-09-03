"""Serialization and validation for `Run` and its uploaded artifacts.

Task 16.2 — the request/response shapes behind `POST /api/runs/`
(Requirement 12.2), `GET /api/runs/:id/` (Requirement 12.3), and
`PATCH /api/runs/:id/` (Requirement 12.4), plus the validation that
rejects a malformed create request (Requirement 12.5, Property 21).

### Three `Run` serializers, one per verb

design.md's REST API surface gives `Run` three verbs with three different
contracts, and each one wants a different field to be writable:

- `POST /api/runs/` establishes a Run's identity: which Project it belongs
  to, its CLI-generated `run_id`, and which agent produced it.
- `PATCH /api/runs/:id/` "Update Run fields (status transitions,
  `pr_url`)" — the two fields that change over a Run's life.
- `GET /api/runs/:id/` returns the Run plus every associated artifact.

So `RunSerializer` carries the shared representation, `RunUpdateSerializer`
narrows the writable set to what PATCH is specified to change, and
`RunDetailSerializer` adds the nested artifacts. All three render the same
`Run` fields, so a client sees one consistent `Run` shape whichever verb it
used; only what it is allowed to *write* differs.

Collapsing them into one serializer would mean PATCH could rewrite
`run_id`, which is the value the CLI addresses a Run by across process
boundaries (`vaultshield submit <run-id>`, `vaultshield teardown <run-id>`,
`run_security_scan(run_id)`), or reassign `project`, moving a completed
Run to a different repository. Neither has a use case in the spec, and the
first one strands the Run.

### Validation is derived from the model, not restated here

As in `projects/serializers.py`, these are plain `ModelSerializer`s with no
hand-written `validate_*` methods, and that is the implementation of
Requirement 12.5 rather than a gap in it: each field is built from its
model column, so the column's constraints become request validation with
one source of truth. For `Run`'s writable fields that yields:

- `project` — required, and a `PrimaryKeyRelatedField` whose queryset is
  `Project.objects.all()`, so a pk that does not exist is a validation
  error. That is the "associated with an existing Project" half of
  Requirement 12.2, enforced at validation time rather than as an
  integrity error at `INSERT`.
- `run_id` — required, non-blank (whitespace-only included: DRF's
  `CharField` strips before the blank check), at most 64 characters, and
  unique, since the column is `unique=True`. A duplicate `run_id` comes
  back as a 400 rather than a database-level `IntegrityError` surfacing as
  a 500.
- `agent` — required, non-blank, at most 32 characters.
- `status` — a `ChoiceField` over `Run.STATUS_CHOICES`, so Requirement
  11.2's five Run_Status values are the only accepted input. Optional on
  create because the column defaults to `running`, which is what the CLI
  wants when it opens a Run.
- `pr_url` — optional and blank-able (the column is `blank=True,
  default=""`), URL-format checked when non-empty.

DRF runs all of that before `create()`/`update()`, so a rejected request
performs no write — the "SHALL NOT create any record" half of Property 21
holds by ordering, not by a check in this file.

### `run_id` format is not validated here

`run_id` is checked for length and uniqueness but not for shape, even
though it is what `RunViewSet` looks a Run up by in the URL. The producer
side already owns that grammar: `cli/vaultshield/run_directory.py` is the
only thing that mints a `run_id`, and it validates every id it accepts
against a single safe path component (alphanumeric start, then
alphanumerics/`-`/`_`, no dot, no separator) because it also turns the id
into a filesystem path and a git ref. Re-deriving that pattern here would
put a second copy of a CLI-side rule in the backend, free to drift from it.

The consequence worth naming: a client that posts a `run_id` containing a
`/` or a `.` creates a Run that its own detail URL cannot address, because
Django's default detail-lookup pattern excludes both characters. Nothing
in the spec fixes a backend-side `run_id` grammar, so this is left as the
CLI's invariant rather than half-enforced in two places.

### Read-side and write-side artifact serializers are separate

`RunEventSerializer`, `RunDiffSerializer`, `ScreenshotSerializer`, and
`SecurityFindingSerializer` exist for Requirement 12.3's nesting: they are
read-only members of `RunDetailSerializer`, and none of them declares its
own `run` field, because under a `Run` the parent is already known and
echoing it back on every one of a Run's events would be noise.

The three ingest serializers below them — `RunEventIngestSerializer`,
`RunDiffIngestSerializer`, and `ScreenshotIngestSerializer` (Task 16.7,
Requirement 13) — are the write side of the same three tables, and they
are separate classes rather than the read serializers made writable for
one reason that applies to all three: **`run` is never part of an ingest
payload.** Every ingest endpoint is a detail route
(`/api/runs/:id/events/` and friends), so the Run is already identified by
the URL, and a body-supplied `run` would be a second, contradictable
source for it — a client could POST to one Run's URL and attach the
artifact to another. `RunViewSet` passes the Run it resolved from the URL
as `serializer.save(run=run)`, which is why none of the three declares the
field.

They also do not share a base class with the read serializers, because
what each one has to do on write differs: an event batch has its
`sequence` assigned by the server, a diff is an upsert against a
one-to-one row, and a screenshot is an ordinary create. Those are spelled
out on each class.
"""

from django.db.models import Max
from rest_framework import serializers

from runs.models import Run, RunDiff, RunEvent, Screenshot
from security_scan.models import SecurityFinding


class RunEventSerializer(serializers.ModelSerializer):
    """One `Run_Event` as it appears nested in the run detail response.

    Read back in `sequence` order — `RunEvent.Meta.ordering` is
    `["sequence"]`, which is what preserves the Monitor's timestamp-sorted
    Event_Log order (Requirement 6.3) through upload and retrieval rather
    than re-sorting by ingest time.
    """

    class Meta:
        model = RunEvent
        fields = ["id", "event_type", "timestamp", "payload", "sequence"]


class RunDiffSerializer(serializers.ModelSerializer):
    """The `Run_Diff` as it appears nested in the run detail response.

    `files` is a `JSONField`, so the CLI's `diff.json` shape
    (`[{"path", "additions", "deletions"}, ...]`) round-trips exactly —
    which is what the dashboard's Diff tab (Requirement 20.2) and the
    static scanner (Requirement 17.1) both read.
    """

    class Meta:
        model = RunDiff
        fields = ["id", "files"]


class ScreenshotSerializer(serializers.ModelSerializer):
    """One `Screenshot` as it appears nested in the run detail response.

    `image` renders as a URL under `MEDIA_URL`, absolute when the
    serializer has the request in its context (which it does when built by
    a viewset), so the dashboard's Screenshots tab (Requirement 20.4) can
    load it directly. That URL is what makes an uploaded Screenshot
    "retrievable through the Run detail endpoint" per Requirement 13.3.
    """

    class Meta:
        model = Screenshot
        fields = ["id", "route", "image"]


class SecurityFindingSerializer(serializers.ModelSerializer):
    """One `Security_Finding` as it appears nested in the run detail response.

    Lives in this module rather than a `security_scan/serializers.py`
    because it is only ever read through a Run: design.md's app layout
    gives `security_scan/` models, rules, and tasks but no serializers, and
    findings reach a client exclusively via the `security_findings` reverse
    accessor below (Requirement 12.3, and the dashboard's Security tab in
    Requirement 20.5).
    """

    class Meta:
        model = SecurityFinding
        fields = ["id", "rule_id", "file_path", "line_number", "description"]


class RunSerializer(serializers.ModelSerializer):
    """The `Run` representation `POST /api/runs/` accepts and returns.

    Also the shape every other `Run` response is built from:
    `RunUpdateSerializer` narrows what it accepts, `RunDetailSerializer`
    extends what it returns, and both inherit these fields unchanged.
    """

    class Meta:
        model = Run
        # Every column on the model, listed explicitly rather than via
        # `__all__` so a column added by a later migration has to be opted
        # in to the API surface deliberately. That matters for Requirement
        # 22.5 (the backend schema never carries a repository-hosting
        # access token): `__all__` is exactly the mechanism that would
        # quietly publish one.
        fields = [
            "id",
            "project",
            "run_id",
            "agent",
            "status",
            "pr_url",
            "stuck",
            "created_at",
            "updated_at",
        ]
        # `id`/`created_at`/`updated_at` are already read-only by
        # derivation (primary key, `auto_now_add`, `auto_now`); naming them
        # pins the contract that a client cannot choose a Run's id or forge
        # its timestamps. `updated_at` in particular is the clock
        # `flag_stuck_approvals` (Task 33.2) selects on.
        #
        # `stuck` is the one that is read-only by decision rather than
        # derivation. Requirement 11.4 defines it as a flag the backend
        # sets on Runs approved long ago that were never submitted; it is
        # `flag_stuck_approvals`'s output, never a client's input, and a
        # CLI able to clear it could hide exactly the situation the field
        # exists to surface.
        read_only_fields = ["id", "stuck", "created_at", "updated_at"]


class RunUpdateSerializer(RunSerializer):
    """The `Run` representation `PATCH /api/runs/:id/` accepts and returns.

    Same rendered fields as `RunSerializer` — Requirement 12.4 returns "the
    updated Run", and a client should not get a different `Run` shape back
    depending on the verb it used — but only `status` and `pr_url` are
    writable, which is the PATCH surface design.md's REST API table
    specifies ("Update Run fields (status transitions, `pr_url`)").

    Those two are also the only fields anything in the spec patches:
    the CLI moves a Run to `awaiting_review` once its upload completes, and
    `vaultshield submit` writes back `pr_url` (Requirement 22.4).

    Two notes on what this does *not* do:

    - A payload naming a read-only field is ignored rather than rejected.
      That is DRF's standard behaviour for read-only fields and it is what
      already happens to `id` and `created_at`, so `PATCH {"agent": ...}`
      succeeds and changes nothing rather than 400-ing.
    - `status` is validated only against Requirement 11.2's choice set, not
      against the approve/reject state machine. Requirement 21's gate ("a
      decision is only recordable from `awaiting_review`") is a property of
      the `approve`/`reject` actions and the `Approval` row they write
      (Task 28.1), and Requirement 12.4 places no transition restriction on
      PATCH. Adding one here would be a rule the spec does not state; it is
      called out rather than silently invented, because it does mean a
      PATCH can set `status="approved"` without an `Approval` record
      existing.
    """

    class Meta(RunSerializer.Meta):
        # A Run's identity is fixed at creation. `run_id` is the value the
        # CLI and the scanner address a Run by across process boundaries,
        # so renaming it mid-flight would strand the Run; `project` and
        # `agent` describe what produced the Run and cannot become untrue
        # after the fact.
        read_only_fields = [
            *RunSerializer.Meta.read_only_fields,
            "project",
            "run_id",
            "agent",
        ]


class RunDetailSerializer(RunSerializer):
    """The full `Run` detail `GET /api/runs/:id/` returns.

    Requirement 12.3: the Run plus "its associated Run_Event, Run_Diff,
    Screenshot, and Security_Finding records" — all four nested inline, so
    one request gives the dashboard's `RunDetailView` everything its four
    tabs render (Requirements 20.2-20.5) and gives Requirement 14.3 a
    single endpoint that proves an upload completed.

    Each nested field is `read_only=True`: writes to these tables go
    through the ingest endpoints (Task 16.7), not through a nested payload
    on the Run. That also keeps `POST`/`PATCH` unaffected — they use
    `RunSerializer`/`RunUpdateSerializer` and never see these fields.

    `Approval` is deliberately not nested. Requirement 12.3 names four
    associated record types and `Approval` is not one of them; the decision
    it records is already visible as the Run's `status`, and the dashboard
    reads that.
    """

    # `many=True` for the three `ForeignKey` relations, singular for
    # `diff`, matching the cardinality on the models: a Run has many
    # events, screenshots, and findings but exactly one change set.
    events = RunEventSerializer(many=True, read_only=True)
    # `RunDiff` is a reverse one-to-one, so `run.diff` raises
    # `RunDiff.DoesNotExist` for a Run whose diff has not been ingested
    # yet. DRF catches `ObjectDoesNotExist` while resolving an attribute
    # and renders the field as `null`, so a Run still in progress
    # serializes with `"diff": null` rather than erroring — which is the
    # behaviour Requirement 12.3 needs, since a Run is retrievable from
    # the moment it is created and its artifacts arrive later.
    diff = RunDiffSerializer(read_only=True)
    screenshots = ScreenshotSerializer(many=True, read_only=True)
    # Sourced from `SecurityFinding.run`'s `related_name`, which is why the
    # run detail endpoint can read findings without importing anything from
    # `security_scan` beyond the serializer above.
    security_findings = SecurityFindingSerializer(many=True, read_only=True)

    class Meta(RunSerializer.Meta):
        fields = [
            *RunSerializer.Meta.fields,
            "events",
            "diff",
            "screenshots",
            "security_findings",
        ]


class RunEventBatchSerializer(serializers.ListSerializer):
    """Persists a whole `POST /api/runs/:id/events/` batch in one write.

    Requirement 13.1 is two claims: store each submitted event as a
    `RunEvent`, and preserve the batch's original chronological order.
    Storage is `bulk_create`; the ordering half is what this class exists
    for, because it is the only place that sees a submitted event's
    *position*.

    ### `sequence` is assigned here, never accepted from the client

    `RunEvent.sequence` is what reads come back ordered by
    (`RunEvent.Meta.ordering`), so it is the field that carries
    Requirement 13.1's order through to retrieval. It is assigned from each
    event's index in the submitted list, which makes Property 22 ("bulk
    event ingest preserves submitted order") hold by construction rather
    than by trusting the payload: a client cannot submit a batch whose
    stored order differs from its submitted order, because it has no way
    to express an order at all.

    That is also the shape the producer needs. The CLI's Event_Log
    (`cli/vaultshield/monitor.py`) writes JSONL records of exactly
    `event_type`/`timestamp`/`payload` — already merged and timestamp-sorted
    per Requirement 6.3 — and design.md's `upload_run` posts those lines
    "batched, in file order". So no `sequence` exists on the wire to accept,
    and the Uploader posts parsed Event_Log lines through untranslated.

    ### Numbering continues across batches

    A Run's events arrive in several POSTs (`upload_run` batches them), so
    numbering restarts from the highest `sequence` already stored for that
    Run rather than from zero. Starting every batch at zero would make the
    second batch's events sort *interleaved* with the first's — the exact
    failure Requirement 13.1 rules out — since retrieval orders by
    `sequence` alone.

    The read of that high-water mark and the insert that extends it are one
    read-modify-write, so `RunViewSet.events` runs this inside a
    transaction holding a row lock on the Run. Two concurrent batches for
    one Run would otherwise both read the same maximum and assign
    overlapping numbers.

    ### An empty batch is accepted and stores nothing

    `allow_empty` is left at its default. A Run that produced no events is
    a legitimate (if unusual) outcome, and rejecting an empty batch would
    make the Uploader special-case it for no gain: zero events submitted
    means zero events stored, which satisfies "store each event" trivially.
    """

    def create(self, validated_data: list[dict]) -> list[RunEvent]:
        """Assigns `sequence` by list position and inserts the batch.

        `validated_data` is the submitted list in submitted order, each
        item already carrying the `run` that `RunViewSet` passed to
        `save(run=...)`. `bulk_create` preserves the order of the list it
        is given, so the returned instances — and therefore the response
        body — read in the same order the client sent.
        """
        if not validated_data:
            return []

        # Every item carries the same Run: `ListSerializer.save(**kwargs)`
        # merges the view's `run=` into each item, so the first is
        # representative.
        run = validated_data[0]["run"]
        first_sequence = _next_sequence_for(run)

        return RunEvent.objects.bulk_create(
            RunEvent(sequence=first_sequence + offset, **attributes)
            for offset, attributes in enumerate(validated_data)
        )


def _next_sequence_for(run: Run) -> int:
    """The `sequence` the next ingested event for `run` should take.

    Zero for a Run with no events yet, otherwise one past the highest
    already stored. Zero-based so a single-batch Run's sequences line up
    with the line indices of the `events.jsonl` they came from.
    """
    highest = run.events.aggregate(highest=Max("sequence"))["highest"]

    return 0 if highest is None else highest + 1


class RunEventIngestSerializer(serializers.ModelSerializer):
    """One event of a `POST /api/runs/:id/events/` batch (Requirement 13.1).

    Used with `many=True`, which builds `RunEventBatchSerializer` (see
    `list_serializer_class`) — that is where the batch is actually written
    and where `sequence` is assigned.

    The request body is a bare JSON array of these objects rather than an
    envelope such as `{"events": [...]}`: the Event_Log is a JSONL file, so
    a batch is literally a list of parsed lines, and `many=True` is DRF's
    own idiom for that. One accepted shape means the Uploader (Task 18.2)
    has one thing to produce and this endpoint has no branch to pick
    between them.

    Field validation is derived from `RunEvent`'s columns, as everywhere
    else in this module — `event_type` non-blank and at most 32 characters,
    `payload` non-blank, `timestamp` a parseable datetime. Two notes on
    what that does *not* constrain:

    - `event_type` is not restricted to a choice set. The column carries no
      `choices` (design.md's inline comment lists `"output" |
      "file_change"`, but the Monitor also emits `"violation"` records for
      an `off_limits_paths` match per Requirement 4.5), and the vocabulary
      is owned by the CLI-side Event_Log. Pinning it here would mean a
      backend migration every time the Monitor learns a new record kind.
    - `timestamp` is the time the Monitor observed the event on the
      developer's machine, not ingest time, so no bound is placed on it.
      That is the clock Requirement 6.3's merge already sorted by, and
      rejecting a timestamp for being old would reject a legitimately
      late-uploaded Run.
    """

    class Meta:
        model = RunEvent
        # `run` is absent deliberately (see the module docstring): the URL
        # identifies it. `id` and `sequence` are rendered but not accepted
        # — `sequence` is the server's own ordering key, so the response
        # shows the client what order was recorded.
        fields = ["id", "event_type", "timestamp", "payload", "sequence"]
        read_only_fields = ["id", "sequence"]
        list_serializer_class = RunEventBatchSerializer


class FileDiffStatSerializer(serializers.Serializer):
    """One per-file entry of an ingested `RunDiff.files` payload.

    `RunDiff.files` is a `JSONField`, so nothing about the shape design.md
    fixes for it (`[{"path", "additions", "deletions"}, ...]`) is enforced
    by the column. This is where that shape is checked, and the reason to
    check it at all is who reads the stored value later: the Security_Scan
    Service iterates these entries inside a Celery worker (Requirement
    17.1) and the dashboard's Diff tab renders them (Requirement 20.2).
    Accepting arbitrary JSON here would turn a malformed upload into a
    worker traceback or a broken tab, instead of a 400 the Uploader can
    report while the artifacts are still on local disk (Requirement 14.4).

    A plain `Serializer`, not a `ModelSerializer`: there is no per-file
    model to derive from, and design.md is explicit that there should not
    be one — the change set is stored whole in a JSON column because
    nothing queries individual file rows.

    `additions`/`deletions` are floored at zero because they are line
    counts. `cli/vaultshield/git_capture.py` reports both as 0 for a binary
    file (git prints `-`, and line counts are undefined for binary
    content), so 0 is a real value and only negatives are rejected.

    The validated result carries exactly these three keys, so an entry
    with extra keys is stored without them. Nothing in the spec produces or
    reads a fourth key, and letting unrecognised keys through would put
    unvalidated client data into the payload the scanner walks.
    """

    path = serializers.CharField()
    additions = serializers.IntegerField(min_value=0)
    deletions = serializers.IntegerField(min_value=0)


class RunDiffIngestSerializer(serializers.ModelSerializer):
    """The structured diff `POST /api/runs/:id/diff/` accepts (Req. 13.2).

    The request body is the CLI's `diff.json` verbatim —
    `{"files": [{"path", "additions", "deletions"}, ...]}` — which is why
    `files` is the only writable field and why the envelope is not
    unwrapped: `cli/vaultshield/git_capture.py` writes that object, so the
    Uploader posts the parsed artifact through unchanged.

    An empty `files` list is accepted. `build_structured_diff` writes
    `{"files": []}` for a Run that changed nothing, specifically so the
    Uploader always has a diff to post, and a Run with an empty change set
    is exactly what a reviewer needs to see rather than a 400.

    ### Ingest is an upsert, because `RunDiff.run` is a `OneToOneField`

    A Run has exactly one change set, so a second POST cannot mean "add
    another" — the only readings are "replace" or "reject". This replaces,
    because the alternative punishes the retry that Requirement 14.4 is
    built around: an upload whose response was lost mid-flight would fail
    permanently on retry, and an `IntegrityError` on the one-to-one column
    would surface as a 500 rather than as anything a client could act on.
    Replacing keeps diff ingest idempotent, so re-posting the same
    `diff.json` is a no-op and re-posting a corrected one is possible.

    `RunViewSet.diff` distinguishes the two outcomes in the status code
    (201 created / 200 replaced) rather than here; `create` below performs
    both.
    """

    files = FileDiffStatSerializer(many=True)

    class Meta:
        model = RunDiff
        # As with events, `run` is absent: the URL identifies it.
        fields = ["id", "files"]
        read_only_fields = ["id"]

    def create(self, validated_data: dict) -> RunDiff:
        """Stores the change set, replacing any diff already ingested.

        `ModelSerializer.create` cannot be used as-is for two independent
        reasons: it refuses to write a declared nested field like `files`
        (which here is a validated list destined for a single JSON column,
        not a relation), and it would `INSERT` unconditionally against a
        one-to-one column that may already have a row.
        """
        run = validated_data["run"]
        # `validated_data["files"]` is a list of `dict`s built by
        # `FileDiffStatSerializer`, which is JSON-serialisable as-is — the
        # column takes it without any further conversion.
        run_diff, _created = RunDiff.objects.update_or_create(
            run=run, defaults={"files": validated_data["files"]}
        )

        return run_diff


class ScreenshotIngestSerializer(serializers.ModelSerializer):
    """One screenshot upload for `POST /api/runs/:id/screenshots/` (Req. 13.3).

    One image per request, as design.md's REST API surface specifies
    ("Upload one Screenshot (multipart)") and as `upload_run` posts them —
    each captured route's PNG is its own POST. The body is multipart: a
    `route` string plus the `image` file, which the project-wide
    `MultiPartParser` already accepts, so this endpoint needs no parser of
    its own.

    Validation is derived from the columns, and the `image` field is the
    interesting one: `Screenshot.image` is an `ImageField`, so DRF
    validates that the upload actually decodes as an image (via Pillow)
    before storing it. A truncated or non-image upload is rejected at
    ingest instead of being discovered when the dashboard's Screenshots tab
    tries to render it. `route` is required and non-blank — a screenshot
    with no route is unattributable in that tab — and capped at the
    column's 255 characters.

    Nothing constrains a Run to one screenshot per route.
    `Screenshot.run` is a plain `ForeignKey` with no unique-together, and
    `capture_route_screenshots` already de-duplicates a route listed twice
    in `.vaultshield.yml`, so adding a uniqueness rule here would only
    turn a retried upload into a 400 — while Requirement 13.3 asks for the
    image to be stored and retrievable, which a second row satisfies.

    Requirement 13.3's "retrievable through the Run detail endpoint" needs
    nothing from this class: `RunDetailSerializer.screenshots` nests every
    `Screenshot` pointing at the Run, so a stored row is retrievable the
    moment it exists.
    """

    class Meta:
        model = Screenshot
        # `run` absent for the same reason as the two above; `image`
        # renders back as its `MEDIA_URL` path, absolute when the
        # serializer has the request in context, which is what makes the
        # response usable as a confirmation of what was stored.
        fields = ["id", "route", "image"]
        read_only_fields = ["id"]
