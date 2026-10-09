# Batch 2 — Run created at `run` start, `failed` status, failed-on-every-error-path

Requirements + technical design. No code has been changed in this step. Pre-existing
uncommitted work (patch passthrough, non-UTF-8 gap, interactive-auth stand-in) is left
as is; nothing here reverts or redoes it.

## 1. What the code does today (evidence)

| Question | Answer today | Evidence |
|---|---|---|
| Does POST `/api/runs/` take a client-supplied `run_id`? | Yes, and it must: `run_id` is a required writable field on `RunSerializer`. Length (≤64) and uniqueness are validated. The format is not: any string up to 64 chars is accepted. | `runs/serializers.py:174-213` (fields), `:62-78` ("`run_id` format is not validated here"), `runs/models.py:72` `CharField(max_length=64, unique=True)`, `runs/tests.py:155-158` required = `{project, run_id, agent}`, `test_mongodb.py:1351-1362` creates with `"20240101-000000-abc"` |
| Duplicate `run_id` | Sequential duplicate: 400 from DRF's `UniqueValidator` (`{"run_id": ["run with this run id already exists."]}`). Concurrent duplicate: both requests pass the validator, the second `INSERT` hits the unique index and raises `IntegrityError`, which is an unhandled **500**. | `serializers.py:43-48`, `test_mongodb.py:350-356` (index raises `IntegrityError`) |
| Can `create` set `status`? | Yes. `status` is writable on create, any of the 5 choices. | `serializers.py:52-54` |
| Where does `running → awaiting_review` happen? | **Nowhere.** No backend code and no CLI code writes `awaiting_review`. Two docstrings say the CLI does it after upload, but no CLI code does. | grep `awaiting_review` over `cli/vaultshield` and `backend` (non-test): only docstrings, `serializers.py:226`, `runs/tests.py:257`, `tasks/scheduled.py:193` |
| PATCH `/api/runs/:run_id/` status rules | Accepts any value from the choice set, with no transition rules. This is documented as deliberate. | `serializers.py:216-258` (`RunUpdateSerializer`), `views.py:504-526` |
| PATCH authz | Authentication only: `TokenAuthentication` + `IsAuthenticated` project-wide. There is **no ownership model at all**: `Project` has no owner field by design (single-developer MVP), so any valid token can PATCH any Run. | `settings.py:371-376`, `views.py` RunViewSet "Access control" docstring, `projects/models.py:1-12` |
| CLI upload order | events → diff → screenshots. Absent artifacts are skipped. Screenshots are 0..N per Run. | `uploader.py:529-531`, `_upload_screenshots` `:665` |
| When the CLI touches the backend in `run` | Only at step 10, after teardown: `_upload_run_artifacts` loads the token and uploads. A fresh `run_id` 404s because nothing creates the row. | `cli.py:828`, `:982-1012`, the comment "One gap this step inherits" `cli.py:502-515` |
| `apply_decision` pattern | `retry_on_transient_transaction_error(fn)`. `fn` opens `django_mongodb_backend.transaction.atomic()` and runs a conditional `Run.objects.filter(pk=..., status__in=ALLOWED).update(status=..., updated_at=to_mongo_precision(now))`. Zero rows updated → `StateTransitionError(_stored_status_of(run))` (409 with `detail` + `current_status`). | `views.py:65`, `:74-126`, `:129-255` |
| Run_id grammar on the CLI | `_RUN_ID_PATTERN = \A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z`. Generated ids are `%Y%m%d-%H%M%S-<16 hex>`. | `run_directory.py:151-157`, `:185-200`, `:203-238` |
| Project identity | The backend `Project` pk is an ObjectId (a 24-hex string in the API). An invalid ObjectId string gives a clean 400 (`drf_mongodb` `fail("invalid")` / `incorrect_type`). The CLI has no notion of a project: `ProjectConfig` has no such field, and the repo's `.vaultshield.yml` has none. | `drf_mongodb.py` (InvalidId → `self.fail`), `config.py:35-56`, `/.vaultshield.yml` |
| Sandbox reaper vs new status | `ACTIVE_RUN_STATUSES = {"running"}`. A `failed` Run's stale sandbox records are therefore reaped, which is what we want, with no change. | `tasks/scheduled.py:116` |
| Dashboard | `STATUS_STYLES` holds 5 keys, with an unknown status falling back to `UNKNOWN_STATUS_STYLE` (slate). Decision buttons only render for `DECIDABLE_STATUS = 'awaiting_review'`. | `RunHistoryView.jsx:61-74`, `RunDetailView.jsx:113`, `:460` |
| Carried item (c) | The test calls `guarded.read_output(redact=_STDIN_SENTINEL)` and sends before reading on the `bare` handle, but the real API has `read_output()` with no params and redaction armed in `send_line`. Because `send_line` arms redaction, the "bare" echo is *also* scrubbed now, so the hazard assertion `_STDIN_SENTINEL in echoed` would fail too. | `test_sandbox_execution.py:1181-1240`, `sandbox.py:334-398`, `_REDACTED = "***"` `sandbox.py:192` |
| Carried item (d) | `_FakeInteractiveExec.timed_out` already exists; `cli.py` raises `AuthError("The {agent} sign-in produced no further output for {N}s and was abandoned. Nothing was started.")` | `test_interactive_auth.py:86-130`, `cli.py:` `_authenticate_in_sandbox` timed_out branch |

## 2. Requirements

### Functional

- **FR1** `Run.status` gains `failed` (6 values). Migration `runs/migrations/0002_alter_run_status.py` (AlterField, generated by `makemigrations runs`).
- **FR2** PATCH `status=failed` succeeds only from `running`. From any other status it returns 409 `StateTransitionError`-shaped (`detail` + `current_status`), atomically and conditionally.
- **FR3** PATCH `status=awaiting_review` succeeds only from `running`, under the same mechanism. This is the new completion signal (see D3).
- **FR4** approve/reject are unchanged: only from `awaiting_review`.
- **FR5** POST `/api/runs/` validates `run_id` format. A duplicate `run_id` returns 400 in both the sequential and the race case, never 500.
- **FR6** The CLI resolves the project (`--project` > `.vaultshield.yml: project` > error), loads the token, and POSTs the Run after step 2 and before the Sandbox. On failure it aborts with a one-line ClickException; `Sandbox`/`docker.from_env` are never called.
- **FR7** After a successful upload, the CLI PATCHes `status=awaiting_review`.
- **FR8** Any failure (including `KeyboardInterrupt`) after the Run exists leads to exactly one best-effort PATCH `status=failed`, after the Sandbox teardown. It never masks or replaces the original error, and artifacts stay on disk.
- **FR9** Dashboard: a `failed` pill (neutral slate/grey) in the history view. The detail view shows no approve/reject for `failed`.
- **FR10** Carried items 6(a)–(d).

### Non-functional

- No change to `security_scan/rules.py`, and no change to the security-scan enqueue (diff ingest `on_commit` stays as is).
- Requirement-tagged docstring style. No unrelated refactors. Nothing committed. No weakened assertions.
- The failed PATCH is bounded so it can't hang a crashed Run's exit: `httpx.Timeout(5.0, connect=2.0)`, i.e. each phase is bounded (connect ≤ 2 s, write/read/pool ≤ 5 s each). It is not a single total deadline.

### Acceptance criteria

1. With the §6 replica set up and `VAULTSHIELD_MONGODB_URL` exported, `makemigrations --check --dry-run` reports no changes after the new migration, and `0002_alter_run_status.py` matches the body pinned in §4.1, i.e. everything from `from django.db import …` onward (the whole `# Generated by …` line is excluded from the comparison). Without MongoDB this criterion is reported as not run.
2. PATCH `{"status":"failed"}` on `running` → 200, stored `failed`, `updated_at` bumped. On each of `awaiting_review`, `approved`, `rejected`, `submitted`, `failed` → 409 with `current_status` equal to the stored value, and the row is unchanged.
3. PATCH `{"status":"awaiting_review"}` on `running` → 200. On any other status → 409, row unchanged.
4. Concurrent `failed` and `awaiting_review` PATCHes on one `running` Run: exactly one wins, the other gets 409, and nothing returns 500.
5. approve/reject on `running`/`failed` → 409. On `awaiting_review` → 200 as before.
6. POST with a duplicate `run_id` → 400 with a `run_id` key. A simulated race (validator bypassed, `IntegrityError` on insert) → 400 with the same shape.
7. POST with `run_id` `"a/b"`, `"a.b"`, `"-x"`, `""` or 65 chars → 400 `run_id`. `"20260903-195802-e503f16521bb1a9c"` and `"20240101-000000-abc"` → 201.
8. CLI with the backend unreachable, a 401, a missing token, or no project → non-zero exit, one-line message, `Sandbox.create` never called, `docker.from_env` never called.
9. CLI: a failure injected at each of {credential file, docker.from_env, sandbox.create, interactive auth, supervise (AdapterError / OffLimitsViolation / SandboxMonitorError / OSError), screenshot (ReadinessTimeout / ScreenshotError), git capture, upload, completion PATCH, KeyboardInterrupt, `click.exceptions.Abort` at the sign-in code prompt} → exactly one `mark_run_failed` call, made after `sandbox.teardown` wherever a sandbox existed, and the original message (or KeyboardInterrupt) is what surfaces.
10. A `mark_run_failed` that raises, or reports failure, leaves the original error's text and exit code unchanged, adds at most one warning line on stderr, and has no traceback.
11. The success path makes no `mark_run_failed` call and exactly one completion PATCH, after the upload.
12. The dashboard history test renders a `failed` badge with `data-status="failed"` and the slate style. The detail test with `status: 'failed'` finds no Approve/Reject buttons.
13. Items 6(a)–(d) land as specified in §4.6.

### Out of scope

A server-side reaper for Runs stuck in `running` (SIGKILL, power loss, network gone at failure time) is a follow-up. Also out of scope: a per-user ownership model; a full PATCH transition table for the other statuses; re-uploading a failed Run; the `.gitattributes` scan gap.

## 3. Decisions

**D1 — Duplicate `run_id`: 400, not 409.** 400 is what the API already returns today for the sequential case (`UniqueValidator`). It's what `runs/serializers.py:43-48` documents as the contract, and it fits Requirement 12.5's "malformed create is a 400" family. In this project, 409 is reserved for "the request is fine, the *resource state* refused it" (`StateTransitionError` docstring, `views.py:74-100`). A colliding id is a property of the request body. The race path is mapped to the same 400 body so a client can't tell the two cases apart.

**D2 — `run_id` format: the CLI's safe-path-component grammar, not the strict generated shape.** Regex `\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z`. It's the rule `uploader.upload_run`/`submit`/`teardown` already enforce before using an id in a URL. It guarantees the Run stays addressable by its detail route (no `/`, no `.`), and it accepts every existing fixture (`"20240101-000000-abc"`, `20240101-000000-<ObjectId>`, `SAMPLE_RUN_ID`). The strict `\d{8}-\d{6}-[0-9a-f]{16}` would break `test_mongodb.py:1351`'s fixture and couple the backend to the CLI's minting format. It goes on the serializer (`RunSerializer.Meta.extra_kwargs["run_id"]["validators"]`, plus the existing `UniqueValidator`), **not** the model, so no migration is needed for it. The `serializers.py` "run_id format is not validated here" section gets rewritten to say why this changed: the backend now accepts client-supplied ids as the start of a Run, so an unaddressable Run would be stranded. The two copies of the regex are named as deliberately equal, with a comment pointing at `run_directory._RUN_ID_PATTERN`.
- *User-visible:* a client that used to post ids with `.`/`/`/leading `-` now gets 400. Those Runs were already unaddressable, so nothing working breaks.
- Note: `extra_kwargs` with `validators` **replaces** the auto-derived validators, so the `UniqueValidator` has to be listed explicitly alongside the `RegexValidator`. A bare `UniqueValidator(queryset=…)` would say `"This field must be unique."` (`rest_framework/validators.py:75`), which differs from today's body: the derived validator's text comes from the model field via `rest_framework/utils/field_mapping.py:335-342` (`get_unique_error_message`), giving `"run with this run id already exists."`. So the explicit validator passes `message=RUN_ID_UNIQUE_MESSAGE`, a constant pinned to that exact string (§4.1), and a test proves the sequential-duplicate body is byte-identical to today's.

**D3 — Completion signal: an explicit PATCH `status=awaiting_review` from the CLI after `upload_run` returns. Not a side effect of diff ingest.** The user suggested "after the diff upload", but diff is **not** the last artifact (`uploader.py:529-531`: events, diff, screenshots). The number of screenshots is unknown to the server (0..N), and diff ingest is an idempotent upsert that can be retried. So the server has no ingest call that means "the upload finished". Moving the Run to `awaiting_review` on diff ingest would expose a Run to reviewers (and to approve) while its screenshots were still arriving, or before a later failure marked it `failed`. An explicit signal after the last POST is the only correct point. It reuses the existing PATCH route, which `RunUpdateSerializer`'s docstring already names as the intended mechanism ("the CLI moves a Run to `awaiting_review` once its upload completes"), so no new endpoint is needed. The diff view and its `on_commit(run_security_scan.delay)` are untouched. This still satisfies the task's wording: the transition happens after the diff upload (and after the screenshots that follow it), and it uses `apply_decision`'s atomic conditional-update style (D4). What changes from the suggested placement is only *which request* carries it. The rejected alternative was a transition inside the diff view's `atomic()` block, which would expose a Run to approve before its screenshots land. This is listed for sign-off in §7 item 9, and the final report must state where the transition lives and why.

**D4 — Gated transitions in PATCH, mirroring `apply_decision`.** New in `runs/views.py`:

```python
# Run_Status values PATCH may only move a Run to from `running`. The two the
# CLI itself writes: completion and failure. Any other `status` value keeps
# today's unrestricted PATCH behaviour (see RunUpdateSerializer).
GATED_PATCH_TRANSITIONS = {
    "awaiting_review": frozenset({"running"}),
    "failed": frozenset({"running"}),
}

def apply_status_transition(run: Run, target: str, other_fields: dict) -> Run:
    updated_at = to_mongo_precision(timezone.now())
    def transition():
        with transaction.atomic():   # django_mongodb_backend
            moved = Run.objects.filter(
                pk=run.pk, status__in=GATED_PATCH_TRANSITIONS[target]
            ).update(status=target, updated_at=updated_at, **other_fields)
            if not moved:
                raise StateTransitionError(
                    _stored_status_of(run),
                    target=target,
                    allowed=GATED_PATCH_TRANSITIONS[target],
                )
    retry_on_transient_transaction_error(transition)
    run.status = target; run.updated_at = updated_at
    for k, v in other_fields.items(): setattr(run, k, v)
    return run
```

`StateTransitionError.__init__` gains two optional keywords, `target: str | None = None` and `allowed: Iterable[str] = ()`. When `target` is given, the message is built from the table entry passed in, so the text can't drift from the rule: `allowed_text = ", ".join(f"`{s}`" for s in sorted(allowed))`, then ``f"A Run can only move to `{target}` from {allowed_text}; this Run is `{current}`."`` (today that reads "from `running`"). The class is defined above the table (`views.py:75`), so the caller passes `allowed` in rather than the class reading the module global. `target` and the existing `recorded_decision` keyword (`views.py:107`, used only by `apply_decision`) are mutually exclusive: one describes a gated status transition, the other an already-recorded decision, and no caller passes both. `__init__` enforces it as its first statement with `if recorded_decision is not None and target is not None: raise TypeError("StateTransitionError takes recorded_decision or target, not both")`. That's a `TypeError` rather than the review's suggested `assert`, so the check survives `python -O`; it is a programming error either way and never reaches a client. A non-DB unit test in `runs/tests.py` covers it. The existing approve/reject messages are unchanged (`target=None` keeps the current text byte for byte). The response shape stays `{"detail", "current_status"}`.

`RunViewSet.partial_update` is overridden (there is no override today; `views.py` only has `get_serializer_class` mapping `partial_update` → `RunUpdateSerializer`, `:503-536`). Exact body:

```python
def partial_update(self, request, *args, **kwargs):
    run = self.get_object()                                   # 404 first
    serializer = self.get_serializer(run, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)                 # bad choice → 400, unchanged
    target = serializer.validated_data.get("status")
    if target not in GATED_PATCH_TRANSITIONS:
        self.perform_update(serializer)                       # today's path, byte for byte
        return Response(serializer.data)
    other_fields = {k: v for k, v in serializer.validated_data.items() if k != "status"}
    apply_status_transition(run, target, other_fields)        # 409 via StateTransitionError
    return Response(self.get_serializer(run).data)
```

`other_fields` can only ever be `pr_url` (the one other writable field on `RunUpdateSerializer`, `serializers.py:216-258`), and it is written in the same conditional update, so a refused transition leaves `pr_url` untouched too. The non-gated branch is a deliberate copy of `UpdateModelMixin.update`'s tail rather than `super().partial_update(...)`, because calling super would re-run `get_object()` and re-validate; the observable behaviour is identical. `PUT` stays 405 (`http_method_names`, `views.py:388`), so `update` needs no override.

Docstrings that become stale and must be rewritten in the same change: `RunUpdateSerializer`'s second bullet ("`status` is validated only against Requirement 11.2's choice set … Requirement 12.4 places no transition restriction on PATCH", `serializers.py:236-245`) now says that `awaiting_review` and `failed` are gated in the view and everything else is still unrestricted; and `apply_decision`'s "An already-decided Run is refused before the row is touched" paragraph (`views.py:~165-175`), which says a decided Run "can be put back into `awaiting_review`" by PATCH. That is still reachable (PATCH `running`, then PATCH `awaiting_review`), so the `Approval` pre-check stays; only the sentence changes to name the two-step route.

**Non-gated PATCH writes only the fields it names.** Today the non-gated path reaches DRF's `ModelSerializer.update`, which ends in a bare `instance.save()` (`rest_framework/serializers.py:1064`) and writes every column, including the `status` read at `get_object()` time. So a `PATCH {"pr_url": …}` racing a gated transition or an `apply_decision` could write the stale `running`/`awaiting_review` back over the winner. Chosen fix (the alternative, keeping the full save and flagging the window, was rejected because `vaultshield submit` PATCHes `pr_url` for real): `RunUpdateSerializer` overrides `update`:

```python
def update(self, instance, validated_data):
    # Only the columns this PATCH named, plus the `auto_now` clock, so a
    # pr_url-only PATCH cannot write a stale `status` back over a
    # concurrent gated transition or decision.
    for name, value in validated_data.items():
        setattr(instance, name, value)
    instance.save(update_fields=[*validated_data, "updated_at"])
    return instance
```

`validated_data` keys are model field names (`status`, `pr_url`), which is what `update_fields` takes. `updated_at` is listed so Django runs its `auto_now` `pre_save` (Django only computes `pre_save` for fields in `update_fields`), so it still bumps as today. An empty PATCH body becomes `update_fields=["updated_at"]`: same observable result as today (timestamp bumped, nothing else changed). `partial_update`'s non-gated branch calls `self.perform_update(serializer)` → `serializer.save()` → this `update`; no `perform_update` override is needed. The existing `test_patch_writes_back_a_pr_url` (`vaultshield_backend/test_mongodb.py:1497-1509`) must still pass, and a new DB test asserts that a `pr_url`-only PATCH on a Run whose stored status was changed behind the loaded instance leaves the stored status alone (load the instance, `Run.objects.filter(pk=…).update(status="failed")`, then call `RunUpdateSerializer(instance, data={"pr_url": …}, partial=True)` + `save()`, and assert stored status is still `failed`).

Why the race is closed: `failed` and `awaiting_review` both require `running`, and approve/reject require `awaiting_review`. Each of those four writers is a conditional single-document update inside a Mongo transaction, so of any two concurrent writers from the same source state exactly one matches. A PATCH without `status` no longer writes `status` at all. The one remaining unconditional `status` writer is a non-gated PATCH that explicitly sets `status` to `running`/`approved`/`rejected`/`submitted`; that is today's documented unrestricted behaviour and is flagged below, not a side effect. Approve vs failed can't both match: a Run is in `running` or in `awaiting_review`, never both. A loser's transient write conflict is retried and then re-reads the state, and it takes the 409 path. `submitted`, `approved`, `rejected`, `awaiting_review` and `failed` can never become `failed`, because the filter excludes them.

**Flagged:** the other status values (`running`, `approved`, `rejected`, `submitted`) remain settable by PATCH from anything, as today. So, for example, `PATCH status=running` on a `failed` Run would revive it. Tightening that into a full transition table changes documented behaviour (`RunUpdateSerializer` docstring) and isn't asked for, so it's a follow-up for the user to decide.

**D5 — POST race → 400.** In `RunViewSet`, `perform_create` wraps `serializer.save()` in `try/except django.db.IntegrityError`. It re-raises as `ValidationError({"run_id": [<the UniqueValidator's message>]})` **only if** `Run.objects.filter(run_id=...).exists()`; otherwise it re-raises the original (an unrelated integrity failure stays a loud 500). The message comes from one module-level constant used by both the validator and this handler, so the two paths stay identical. The handler is a `perform_create` override (none exists today); `CreateModelMixin.create` still builds the 201 response, so nothing else about create changes. The existence re-check runs outside any transaction (create is not in one) and is a plain `filter(run_id=serializer.validated_data["run_id"]).exists()`.

Race test, chosen concretely: in `TestRestApiAgainstMongoDB`, create a Run with id `X` directly via the ORM, then POST the same `X` inside `patch.object(UniqueValidator, "__call__", return_value=None)` (import `rest_framework.validators.UniqueValidator`). That disables only the pre-check, so the insert reaches the real Mongo unique index and raises the real `IntegrityError`. Assert 400, `response.json() == {"run_id": [RUN_ID_UNIQUE_MESSAGE]}`, and that exactly one Run with id `X` exists. A second test asserts the sequential duplicate produces the byte-identical body.

**Flagged, not changed:** create still accepts any `status`, so a client could create a Run directly as `failed` or `approved`. Forcing `running` on create is a behaviour change nobody asked for. Follow-up.

**D6 — Authz.** Unchanged: token + `IsAuthenticated`, inherited. **Flagged:** there is no owner/authorized-user concept anywhere in the schema (`projects/models.py:1-12` says this is deliberate for the single-developer MVP), so "only the run's owner" can't be enforced without a schema change. Not built here.

**D7 — Project resolution: `--project` option > `.vaultshield.yml` `project:` key > error.**
- `--project TEXT`: the backend Project id (the 24-hex ObjectId shown by `GET /api/projects/` and the dashboard's project card `data-project-id`). No envvar, no default.
- `ProjectConfig` gets `project: str = ""` (last, defaulted, so existing constructions keep working), read with `_optional_text(raw, "project", path)`. A wrong type is a `ConfigError`, consistent with the other fields. `write_config` (init) does **not** add a placeholder. `init` output is unchanged; the README documents the key.
- Value is `.strip()`ed; empty means absent. The CLI doesn't validate the ObjectId format. The backend answers an invalid or unknown id with 400 `{"project": [...]}`, which the CLI surfaces.
- Resolved in step 1, right after `load_config`, so "no project" aborts before the Run_Directory exists (same no-side-effect guarantee as `ConfigError`). Message: `No project for this Run: pass --project <project-id> or set `project: <project-id>` in .vaultshield.yml (ids are listed by the dashboard / GET /api/projects/).`
- Name-based lookup was rejected because `Project.name` isn't unique.
- *User-visible:* `vaultshield run` now **requires** a project and a reachable backend plus a login before it does anything. Today a Run works offline and only fails at upload. This is what the user decided; it's called out in the README.

**D8 — Run creation sits right after step 2, per the task. Consequence (flagged):** local preflight failures that come after it, such as `--env-file not found` (step 3) or Docker daemon down (step 5), now leave a `failed` Run in the dashboard. The alternative is moving the POST after `docker.from_env()` (still before any container), which would avoid those. I'm following the task literally; the user can choose the alternative.

**D9 — Failed is terminal for the CLI.** If the upload fails, the Run is marked `failed`. A later manual re-upload can't move it to `awaiting_review` (FR3 requires `running`). Flagged as a consequence of FR8 plus FR3; a "retry upload" command is a follow-up.

## 4. Design by file

### 4.1 Backend

**`backend/runs/models.py`**: add `("failed", "failed")` to `STATUS_CHOICES` after `submitted`, and update the comment "exactly these five values" to six, with a line explaining `failed` (set by the CLI when a Run errors after it was registered; terminal). Docstring of `Run`: add "or marked `failed` by the CLI on any error after registration".

**`backend/runs/migrations/0002_alter_run_status.py`**: generated by `makemigrations runs` (header names the installed Django, 5.2.18 in `backend/.venv`, `dependencies=[('runs','0001_initial')]`, a single `AlterField` on `status` with the 6-tuple choices, `default='running'`, `max_length=32`). `contrib_migrations/` isn't affected: it only covers admin/auth/contenttypes (`settings.py:262-270`). AlterField of choices is a no-op on Mongo (no schema).

Every `manage.py` command needs a configured *and reachable* MongoDB: without `VAULTSHIELD_MONGODB_URL`, settings import raises `ImproperlyConfigured` (`vaultshield_backend/settings.py:140-147`), and with the URL set but nothing listening, `makemigrations` dies with an uncaught `pymongo.errors.ServerSelectionTimeoutError` after about 30 s (Django's history-consistency check only downgrades `OperationalError`). So the migration is generated only after the §6 replica-set container is up and the URLs are exported. The expected file is pinned below, in the same style as `0001_initial.py` (single quotes). The generated file must match it from `from django.db import …` onward; the `# Generated by …` line is excluded from the comparison:

```python
# Generated by Django <installed version> on <date>

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('runs', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='run',
            name='status',
            field=models.CharField(choices=[('running', 'running'), ('awaiting_review', 'awaiting_review'), ('approved', 'approved'), ('rejected', 'rejected'), ('submitted', 'submitted'), ('failed', 'failed')], default='running', max_length=32),
        ),
    ]
```

Fallback rule: if no MongoDB can be started (no Docker, or the image can't be pulled), hand-write exactly this file with the header `# Generated by Django 5.2.18 on <date>` (the version `django.get_version()` reports in `backend/.venv`), say in the final report that it was hand-written, and report acceptance 1 (`makemigrations --check --dry-run`) and `manage.py check` as **not run**, with the reason. A hand-written file is not described as verified.

**`backend/runs/serializers.py`**:
- `RUN_ID_PATTERN` and `RUN_ID_UNIQUE_MESSAGE` module constants. The latter is exactly today's text, with this comment:
  ```python
  # Today's DRF-derived text (Django's `unique` error_message for Run.run_id),
  # restated because `extra_kwargs["validators"]` replaces the derived validator.
  RUN_ID_UNIQUE_MESSAGE = "run with this run id already exists."
  ``` `RunSerializer.Meta.extra_kwargs = {"run_id": {"validators": [RegexValidator(RUN_ID_PATTERN, message=...), UniqueValidator(queryset=Run.objects.all(), message=RUN_ID_UNIQUE_MESSAGE)]}}`. `RunUpdateSerializer` has `run_id` read-only, so it's unaffected. RegexValidator message: ``"run_id must be 1-64 characters: a letter or digit, then letters, digits, `-` or `_`."``.
- Rewrite the module-docstring section `### run_id format is not validated here` → `### run_id format` (why it's validated now, why this grammar, why the serializer and not the model).
- `status` docstring bullets: "five" → six.
- `RunUpdateSerializer` docstring: add a paragraph saying that `awaiting_review` and `failed` are gated in the view (`GATED_PATCH_TRANSITIONS`) and the rest are unchanged. Add the `update` override from D4 ("Non-gated PATCH writes only the fields it names") with a Requirement-tagged docstring (Requirement 12.4).
- **6(a)** `FileDiffStatSerializer` docstring: replace the `patch_truncated` paragraph with: *`patch_truncated: true` means the security scan did not see this file's whole patch. That covers a patch cut at the per-file or total budget, a patch omitted entirely because the budget was spent (`""` or absent), and a path that is not valid UTF-8, for which no patch was captured. An entry with no `patch` and no flag is a binary file.* Also fix the `allow_blank` bullet so it doesn't claim the flag only means "prefix".

**`backend/runs/views.py`**: `GATED_PATCH_TRANSITIONS`, `apply_status_transition`, the `StateTransitionError(target=...)` extension, the `partial_update` override, and the `perform_create` IntegrityError mapping, all as in D4/D5. Plus module-docstring table rows. Imports: `from django.db import IntegrityError` (this is not `transaction`, so it's fine). Requirement-tagged docstrings on every new function. No change to `diff`, `events`, `screenshots`, or `apply_decision` logic.

Docstrings that become false and are rewritten in the same change (wording only, no logic):
- `runs/views.py` `RunViewSet` docstring (`:302-303`), "see the note in `runs/serializers.py` on why the backend does not re-derive the CLI's `run_id` grammar" → it now does (D2); point at the rewritten `### run_id format` section.
- `StateTransitionError` class docstring (`views.py:76`, "not in a state that accepts a decision") → "not in a state that accepts this decision or gated status transition".
- `RunUpdateSerializer` second bullet and `apply_decision`'s re-open paragraph (D4).
- `tasks/scheduled.py:86-90`: the list of statuses in which a still-`active` SandboxRecord is stale gains `failed`. `:94-95` ("a Run whose CLI was killed mid-flight keeps `status="running"` forever") stays true for SIGKILL; append that an error the CLI survives now ends in `failed`.
- `tasks/scheduled.py:193`: the lifecycle chain becomes `running -> awaiting_review | failed -> approved/rejected -> submitted`. The sentence that only a hand-edit or a PATCH can move a Run back to `running` stays true (non-gated PATCH is unchanged).
- Not touched: `cli/vaultshield/sandbox.py:308-318` still says `read_output(redact=...)`. That is pre-existing, product code outside this task's items, and changing it would be an unrelated edit; the final report mentions it as a follow-up.

**`backend/README.md`**: a short "Run lifecycle" note: `running → awaiting_review` (CLI completion PATCH) `| failed` (CLI on error), approve/reject from `awaiting_review`, `failed` terminal; Runs stuck in `running` after SIGKILL remain (follow-up reaper).

### 4.2 CLI — `cli/vaultshield/uploader.py`

New public functions, in the module's existing style (`_build_url`, `_auth_headers`, `_error_detail`, the `UploadError` messages never containing the token):

- `RUNS_PATH = "/api/runs/"`, `RUN_DETAIL_PATH_TEMPLATE = "/api/runs/{run_id}/"` with the comment `# Same path as submit.RUN_DETAIL_PATH_TEMPLATE (submit imports this module, so it cannot be shared the other way).` (`submit.py:260` imports from `uploader`; `submit.py:314` defines its copy), `_RUN_CONTROL_TIMEOUT_S = 15.0`, `_MARK_FAILED_TIMEOUT = httpx.Timeout(5.0, connect=2.0)`, `FAILED_STATUS = "failed"`, `AWAITING_REVIEW_STATUS = "awaiting_review"`. An httpx timeout applies per phase (connect, write, read, pool), not in total, so the failed mark is bounded at roughly 2 s to connect plus 5 s per remaining phase; the explicit short connect is what keeps the common "backend gone" case quick.
- Transport, chosen: module-level `httpx.post(...)` / `httpx.patch(...)` with `headers=_auth_headers(token)`, `json=...`, `timeout=...`, the same one-shot style as `_request_token` (`uploader.py:552-592`). Not a `Client`: each of these is a single request. Exceptions caught: `(httpx.HTTPError, httpx.InvalidURL)`, exactly as `_post` does (`uploader.py:866-871`), because `InvalidURL` is not an `HTTPError`. Non-2xx is checked with `response.is_success`, and bodies are quoted with `_error_detail`.
- Run-id guard: `validate_run_id` imported from `run_directory` (as `upload_run` does at `uploader.py:506-514`), `RunDirectoryError` → `UploadError(f"Cannot register run {run_id!r}: {exc}")`. `complete_run` and `mark_run_failed` apply the same guard (for `mark_run_failed`, a bad id becomes a returned warning, not a raise).
- `create_run(backend_url, token, *, project, run_id, agent) -> None`: guard first, then POST JSON `{project, run_id, agent}` to `RUNS_PATH`. `status` is not sent (model default `running`).
  - `httpx.HTTPError`/`InvalidURL` → `UploadError("Cannot reach the VaultShield backend at {url}: {exc}")`.
  - 401/403 → `UploadError("The backend at {url} rejected the dashboard auth token (HTTP {code}). Run `vaultshield login --backend-url {backend_url}` again.")`.
  - Any other non-2xx → `UploadError("The backend at {url} refused to register run {run_id} (HTTP {code}): {_error_detail(response)}")`. That covers an unknown project and a duplicate id.
  - 201 → return.
- `complete_run(backend_url, token, run_id) -> None`: PATCH `{"status": "awaiting_review"}`. Errors map the same way. A 409 gets the message "…is no longer running (backend reports `{current_status}`)".
- `mark_run_failed(backend_url, token, run_id) -> str | None`: PATCH `{"status":"failed"}` with `timeout=_MARK_FAILED_TIMEOUT`. **Never raises**: it catches `Exception` and `KeyboardInterrupt`, and returns `None` on 2xx or a warning string otherwise (including 409, which means the Run already left `running`, e.g. a completion that landed whose response was lost). It doesn't import click and doesn't print.
- `upload_run` is unchanged.

### 4.3 CLI — `cli/vaultshield/config.py`

`ProjectConfig.project: str = ""`, and `load_config` reads it with `_optional_text`. Docstring note: it's the backend Project id, and `run` resolves it with `--project` taking precedence.

### 4.4 CLI — `cli/vaultshield/cli.py` `run`

- New option `--project` (help: "Backend Project id this Run belongs to  [default: `project` in .vaultshield.yml]"). Signature gains `project: str | None`.
- Step 1: after `load_config`, `project_id = _resolve_project(project, config)` → ClickException (D7 message) if empty.
- Step 2 unchanged. Then **2b**:
  ```python
  try:
      token = uploader.load_token()
      uploader.create_run(backend_url, token, project=project_id, run_id=run_id, agent=agent)
  except uploader.UploadError as exc:
      # Parenthesised suffix: several UploadError texts end in an httpx message
      # with no full stop ("…: All connection attempts failed").
      raise click.ClickException(f"Cannot start run {run_id}: {exc} (no Sandbox was created)") from exc
  click.echo(f"Run {run_id} registered with {backend_url}.")
  ```
- Everything from step 3 to the end goes into an outer `try`/`except BaseException`:
  ```python
  try:
      ...steps 3-8 with the existing inner try/except/finally (teardown) unchanged...
      click.echo(f"Run {run_id} complete. ...")
      _upload_run_artifacts(run_dir, run_id, backend_url, token)   # now takes token
      _complete_run(run_id, backend_url, token)                    # ClickException on UploadError
  except BaseException:
      _mark_run_failed_best_effort(run_id, backend_url, token)
      raise
  ```
  The outer `try` opens on the line immediately after the 2b `click.echo`, so it covers step 3 (`_build_run_credential_file`, which raises `ClickException("--env-file not found: …")` or `ClickException("Cannot build the Sandbox_Credential_File …")`, `cli.py:1060-1071`), step 4 (adapter instantiation), and step 5's `docker.from_env()` ClickException (`cli.py:669-675`) as well as the existing inner `try` (`cli.py:679-821`). Nothing between `create_run` returning and the outer `try` can raise except `click.echo`. `log_path` is assigned inside the inner `try` and read after it, inside the outer `try`; scoping is unchanged.
  The inner `finally: sandbox.teardown()` runs before the outer `except`, which gives the "teardown first, then failed PATCH" ordering. If `teardown()` itself raises, that exception replaces the original (today's behaviour, unchanged) and the outer handler still marks the Run failed exactly once. A single outer handler gives "exactly one PATCH". The bare `raise` keeps the original exception, traceback, and chain. `SystemExit`/`click.exceptions.Exit` aren't raised inside `run` on success, so catching `BaseException` doesn't fire on the success path.
- `_mark_run_failed_best_effort`, exactly:
  ```python
  def _mark_run_failed_best_effort(run_id: str, backend_url: str, token: str) -> None:
      """Requirement-tagged docstring: marks the Run `failed`; never raises and
      never replaces the error in flight."""
      try:
          warning = uploader.mark_run_failed(backend_url, token, run_id)
      except BaseException as exc:  # incl. KeyboardInterrupt during the PATCH
          # No str(exc): keep it one line and token-free.
          warning = f"unexpected {type(exc).__name__}"
      if warning:
          try:
              click.echo(
                  f"Warning: could not mark run {run_id} as failed on the backend: {warning}",
                  err=True,
              )
          except Exception:
              pass
  ```
  `uploader.mark_run_failed` is already specified never to raise. This CLI-side guard is a second layer so that a defect there (or a test stub that raises) still can't replace the original error. Swallowing a `KeyboardInterrupt` that lands during the PATCH is deliberate: the caller's bare `raise` then re-raises the original exception, which is the one the user needs to see. If the original exception was itself a `KeyboardInterrupt`, it is the one re-raised.
- `_upload_run_artifacts(run_dir, run_id, backend_url, token)`: no `load_token` inside any more, and the message text is unchanged. `_complete_run` maps `UploadError` → `ClickException(f"Run {run_id} was uploaded but could not be marked ready for review: {exc}")`.
- Update the `run` docstring step list (it's the `--help` text): add "2b. Backend — register the Run (POST /api/runs/); abort if unreachable" and "11. Backend — mark the Run awaiting_review; on any failure after 2b the Run is marked failed". Rewrite the "One gap this step inherits" comment block (`cli.py:502-515`) to describe the resolution. Add the SIGKILL caveat there: a SIGKILLed or power-cut CLI leaves the Run `running`, and a server reaper is a follow-up.
- `KeyboardInterrupt`: it's caught by the outer handler, marked failed, and re-raised. Click then turns it into `Aborted!` with exit 1, as today. During interactive auth there are two cases: Ctrl-C while the sign-in output streams is caught by `_authenticate_in_sandbox`'s `except KeyboardInterrupt` (`cli.py:969`) and becomes an `AuthError` → ClickException; Ctrl-C or EOF at the code `click.prompt` (`cli.py:927`) is turned into `click.exceptions.Abort` by Click itself (`click/termui.py`), which never reaches that `except KeyboardInterrupt`. Both reach the outer `except BaseException`, so both mark the Run failed exactly once and re-raise unchanged.

### 4.5 CLI tests

- **`tests/test_cli.py` `_wire_run_pipeline`**: it already stubs `uploader.load_token`, `uploader.upload_run` and `docker.from_env` (`test_cli.py:332-352`); it additionally stubs `uploader.create_run`, `uploader.complete_run` and `uploader.mark_run_failed`, recording into `seen["backend"]` (a list of `("create"|"complete"|"failed", ...)`) rather than into `calls`, so every existing exact `calls == [...]` assertion stays valid. New kwargs `create=None`, `complete=None`, `mark_failed=None` let a test inject failures. `_upload_run` stub's signature is unchanged (`token` is still passed). `_CONFIG_YAML` (`test_cli.py:229-236`) has no `project:` key and stays that way, so the option is what supplies it.
- **Every non-help `run` invocation gains `--project=0123456789abcdef01234567`.** Grepped call sites in `cli/tests/` (the implementer re-greps `"run",` and `_RUN_ARGS` before editing and lists every site touched in the final report):
  - `test_cli.py`: `_RUN_ARGS` (`:75-80`), used at `:193, :209, :368, :405, :421, :446, :467, :499, :553, :592, :620, :667, :698, :1289, :1300, :1313, :1330`; and the inline per-agent argv at `:638-649`, which builds its own list and needs the option added explicitly. `:193`/`:209`/`:1330` fail in step 1 at `load_config` before project resolution, so the option doesn't change what they prove. Not changed: `:96-104` (a Click-choice rejection, which fails at parsing before the command body) and the `--help` calls at `:125, :145, :167, :184`.
  - `test_interactive_auth.py`: `_RUN_ARGS` (`:76-80`), which every `run` invocation there uses (`:245`–`:613`, including the `--agent=mock` rewrite at `:308`).
  - `test_sandbox_execution.py`: the four inline argvs at `:308, :463, :524, :564` (see the bullet below). Not changed: the `teardown` invocations at `:677`, `:711`.
- **Existing test that must change because the specified behaviour changes:** `test_run_reports_a_missing_token_and_preserves_the_artifacts` currently asserts the artifacts exist after a missing token. Under FR6 a missing token aborts before the Sandbox, so no Event_Log is ever written. It gets rewritten as `test_run_aborts_before_any_sandbox_without_a_token`, which asserts non-zero exit, "vaultshield login" in output, no `sandbox.create` in calls, `Sandbox` never constructed, `docker.from_env` never called, and no `create_run` call. This replaces an assertion whose premise the user changed; it isn't a weakening, and the final report must name it.
- **New tests (test_cli.py):**
  - backend down / 401 / missing project → no `Sandbox` construction, no `docker.from_env` (patched to record), one-line message, no traceback.
  - Parametrized "error at each stage" over the stages in acceptance 9 (the `click.exceptions.Abort` case lives in `test_interactive_auth.py`, where the fake reaches the code prompt: monkeypatch `cli_module.click.prompt` to raise `click.exceptions.Abort()`, expect exit 1 and `"Aborted!"`, one failed mark after `sandbox.teardown`): each asserts `len([b for b in seen["backend"] if b[0]=="failed"]) == 1`, that it comes after `"sandbox.teardown"` in a merged order log (the failed stub appends `"backend.failed"` to `calls` **only in these new tests**, via the kwarg), and that the original message is in the output.
  - `mark_run_failed` returning a warning and `mark_run_failed` raising (stub raises `RuntimeError`, which proves the CLI-side guard): the original text is still in the output, exit code unchanged, one "Warning:" line, no "Traceback".
  - Success: there's one `complete` call after `upload` and no `failed`.
  - `--project` beats config, config `project:` used when no option, a wrong-type `project:` is a ConfigError.
  - **6(b):** `test_run_reports_diff_metadata_over_the_upload_cap`. Monkeypatch `git_capture.MAX_DIFF_REQUEST_BYTES = 256` (the function reads the module global at call time, `git_capture.py:640-648`). After `_wire_run_pipeline`, re-patch `cli_module.build_structured_diff` with a wrapper `lambda sandbox, base_ref, branch, run_dir: git_capture.build_structured_diff(local, EMPTY_TREE, "HEAD", run_dir)`, so the **real** function runs against a temp git repo (created with `git init` + 10 committed one-line files, outside the CliRunner isolated dir) through a minimal local-git sandbox defined in test_cli.py (the same shape as `LocalGitSandbox` in `test_git_capture.py:171-201`: `exec(cmd)` → `subprocess.run(cwd=repo)` → `ExecResult(exit_code, stdout, stderr, stdout_bytes)`). `git_capture` exports no empty-tree constant (it computes it at `git_capture.py:731-741`), so the test obtains the base with the real `git_capture.resolve_diff_base(local, "HEAD")` on a single-commit repo, which exercises the root-commit fallback and returns the empty tree. No literal hash and no new product constant. Skip with `pytest.skip` if `git` is not on PATH, matching test_git_capture.py's gating. Assert the output contains `"Run "`, `"failed:"`, `"MAX_DIFF_REQUEST_BYTES"`, `"10 changed files"`, `"events.jsonl"`, and has no traceback, plus exactly one failed mark.
- **`tests/test_uploader.py`**: `create_run`, `complete_run` and `mark_run_failed` against the existing `ThreadingHTTPServer` pattern. `_IngestHandler` (`test_uploader.py:430-462`) gains `do_PATCH = do_POST`, reusing `responder`. `_IngestRequest` is not widened; tests tell POST from PATCH by path (`/api/runs/` vs `/api/runs/<id>/`), so existing assertions on `received` are untouched. Cover: request body and headers, 201/200 success, 401 message names `vaultshield login`, 400 detail quoted, unreachable (`_closed_port`), the token never in any message, `mark_run_failed` returns a string and doesn't raise on 409/500/unreachable, and the bounded timeout is passed (chosen: monkeypatch `uploader.httpx.patch` with a spy that records its kwargs and returns a 200 `httpx.Response`; assert `kwargs["timeout"] is uploader._MARK_FAILED_TIMEOUT` and that it equals `httpx.Timeout(5.0, connect=2.0)`). `mark_run_failed` swallowing `KeyboardInterrupt` is tested with a spy that raises it: the function returns a warning string and does not propagate.
- **`tests/test_interactive_auth.py` `_wire`**: add the same three backend stubs (default success). The arg lists used by its tests gain `--project=...`. **6(d):** `test_a_sign_in_that_goes_silent_is_reported_as_a_timeout`. The fake has `prompt_lines=["Signing in..."]` (no prompt marker), with `timed_out = True` set before invoke. Expect a non-zero exit, `"produced no further output"` and `"abandoned"` in the output, no `monitor.supervise_in_sandbox` in calls, `sandbox.teardown` in calls, and the handle closed. Use `catch_exceptions=False` per `_invoke`. ClickException is handled by Click and doesn't propagate, which is the existing pattern.
- **`tests/test_config.py`**: `project` optional / present / wrong type.
- **`tests/test_sandbox_execution.py` Docker-gated `run` tests (`:308` isolation probe, `:463` tamper, `:524` crash, `:564` host-only).** Today each runs the pipeline offline with `--backend-url=http://127.0.0.1:1` and relies on only the final upload failing. Under FR6 they would abort at project resolution: `:308` would fail ("no Run_Directory was created"), and the rest would fail or prove the wrong thing. Fix, without weakening any existing assertion:
  - A module helper used by all four:
    ```python
    def _offline_backend(monkeypatch, cli_module):
        """Stands in for the backend's Run registration, completion and failed
        mark, so the pipeline runs to the stage under test. The real
        `upload_run` still runs against the unreachable backend URL."""
        marks = []
        monkeypatch.setattr(cli_module.uploader, "load_token", lambda: "test-token")
        monkeypatch.setattr(cli_module.uploader, "create_run", lambda *a, **k: None)
        monkeypatch.setattr(cli_module.uploader, "complete_run", lambda *a, **k: None)
        monkeypatch.setattr(
            cli_module.uploader, "mark_run_failed", lambda *a, **k: marks.append(a) or None
        )
        return marks
    ```
    `cli.py` calls these as `uploader.<name>` (`cli.py:71` imports the module), so patching the module attribute takes effect, and `monkeypatch` restores it.
  - Each argv gains `--project=0123456789abcdef01234567`. `--backend-url=http://127.0.0.1:1` stays, so `:308`'s real upload still fails as its comment says. Update that comment: the token is now stubbed, and the upload fails because the backend is unreachable.
  - New assertions, added on top of the existing ones: all four assert `len(marks) == 1` (each of them ends in a failure after registration: upload unreachable for `:308`; violation, crash and host-only for the others). `:534`'s existing `"failed" in result.output.lower()` is loose, so it also asserts `"Cannot start run" not in result.output` and that the Run_Directory's `events.jsonl` exists (it already reads it). `:473` and `:574` already assert stage-specific text (`"off_limits_paths violation"`, `"does not support running inside a Sandbox"`), which can't come from a preflight abort.
  - These need Docker plus `vaultshield-sandbox:local`. Run them if available; otherwise report all four as not run, by name.
- **6(c) `test_sandbox_execution.py::test_a_tty_echoes_stdin_and_redaction_removes_it`**: rewrite it to the real contract, still one test with two assertions:
  1. Hazard: write the sentinel to the raw socket directly (`raw = getattr(bare.socket, "_sock", bare.socket); raw.sendall(sentinel + b"\n")`), bypassing `send_line` so redaction isn't armed. Iterate `bare.read_output()` in one pass bounded as the current test does (`for line in bare.read_output(): lines.append(line); if "done" in line: break`, cf. `test_sandbox_execution.py:1206-1209`; EOF also ends it since `sh -c "read value; echo done"` exits) and assert the sentinel is echoed. Keep both `exec_interactive` calls without `timeout_s`.
  2. Mitigation: `guarded.send_line(_STDIN_SENTINEL)`, then a single pass over `guarded.read_output()` bounded the same way (break on `"done"`, cf. `:1222-1225`), then append `flush_remaining()` to the guarded output only. Assert the sentinel is absent and `sandbox._REDACTED` (`"***"`) is present.

  Update its docstring and the comment at `:1132` that mentions `read_output(redact=...)`. It's Docker- and image-gated: try to run it if Docker and `vaultshield-sandbox:local` are available, else report "not run".

### 4.6 Frontend

- `RunHistoryView.jsx`: `STATUS_STYLES.failed = 'border-gray-300 bg-gray-100 text-gray-600'`, inserted after `submitted` (`RunHistoryView.jsx:66-73`). Tailwind's `gray` rather than `slate`: both are neutral grey, but `UNKNOWN_STATUS_STYLE` is already `slate-*` (`:74`), and keying `failed` to a distinct class is what lets a test prove the entry is explicit rather than the unknown-status fallback. Tailwind 4.3.3 ships `gray` by default; no config change. Comment: "five" → six, with `failed` neutral because nothing is pending on a reviewer.
- `RunHistoryView.test.jsx`: a **separate** test with its own one-Run fixture (do not add to the shared `RUNS` array at `:59-65`, so existing row-count/order assertions are untouched). Reword the fixture comment at `RunHistoryView.test.jsx:56` to "One Run per Run_Status other than `failed`, which has its own test below, …". Assert the badge has text `failed`, `data-status="failed"`, `toHaveClass('bg-gray-100')`, and `not.toHaveClass('bg-slate-100')`.
- `RunDetailView.jsx`: no logic change (`decidable` is only `awaiting_review`). Optionally update the comment listing statuses. `RunDetailView.test.jsx`: a new test that renders a `status: 'failed'` Run and asserts `queryByRole('button', {name: /approve/i})` and `/reject/i` are null and the badge reads `failed`. Button labels are `'Approve'` and `'Reject'` (`RunDetailView.jsx:421`, `:427`), so the queries are `queryByRole('button', { name: 'Approve' })` / `{ name: 'Reject' }`, matching the exact-name style already used at `RunDetailView.test.jsx:314`. Render `DecisionControls` directly with `status="failed"` (it is exported, `:460`) rather than the full view, so no fetch mock is needed; plus one assertion via `RunStatusBadge` that `data-status="failed"` renders.
- Commands: `cd frontend && npm test`.

### 4.7 Docs

`cli/README.md` gets a "Registering the Run: `--project`" section. It covers the resolution order, where to find the id, that the backend + login are required before anything runs (one-line abort, no container), the `running → awaiting_review | failed` lifecycle, the best-effort failed mark, and the SIGKILL caveat. `backend/README.md` gets the lifecycle note (§4.1).

## 5. Error handling summary

| Operation | Failure | Recoverable? | Caller gets | Logged |
|---|---|---|---|---|
| resolve project | none given | fatal, pre-side-effect | ClickException (D7) | stderr via Click |
| load_token | missing/empty/unreadable | fatal, before sandbox | ClickException "Cannot start run …" with load_token's text | Click |
| create_run | unreachable/401/403/400/5xx | fatal, before sandbox | ClickException, one line, no token | Click |
| pipeline stage after 2b | any | fatal | the original ClickException / KeyboardInterrupt, unchanged | Click; plus a failed mark |
| upload / complete_run | UploadError | fatal | ClickException (existing upload text / new completion text) | Click; plus a failed mark |
| mark_run_failed | any (incl. 409) | swallowed | nothing; original error proceeds | one `Warning:` line on stderr |
| PATCH gated transition | wrong source state | client-visible | 409 `{detail, current_status}` | none (normal DRF) |
| PATCH | transient Mongo conflict | retried ×5 | 200 or 409 | none |
| POST create | duplicate (pre-check or race) | client-visible | 400 `{"run_id": [...]}` | none |
| POST create | other IntegrityError | fatal | 500 (unchanged) | Django error log |

Invariant ownership: status transitions belong to the backend view layer (`apply_status_transition`/`apply_decision`), because only a conditional DB write closes the race. The `run_id` grammar belongs to the CLI (minting) and is re-checked at the backend serializer for addressability. "Exactly one failed PATCH" belongs to `cli.run`'s single outer handler.

## 6. Verification plan

- Backend (`cd backend`, venv at `backend/.venv`). Every `manage.py` command needs a reachable MongoDB (§4.1), so the container comes first, in this order (per `patch-passthrough-plan.md:562-570`):
  ```
  docker run -d --rm --name vs-test-mongo -p 27018:27017 mongo:7 --replSet rs0 --bind_ip_all
  docker exec vs-test-mongo mongosh --quiet --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]})'
  export VAULTSHIELD_MONGODB_URL='mongodb://localhost:27018/vaultshield?directConnection=true'
  export VAULTSHIELD_TEST_MONGODB_URL="$VAULTSHIELD_MONGODB_URL"
  python manage.py makemigrations runs              # writes 0002; compare with §4.1
  python manage.py makemigrations --check --dry-run # acceptance 1: "No changes detected"
  python manage.py check
  pytest                                            # full suite, incl. test_mongodb.py
  docker stop vs-test-mongo
  ```
  Wait for the replica set to report PRIMARY before the `manage.py` steps (retry `rs.status().myState === 1`). If no MongoDB can be started: hand-write `0002` exactly as pinned in §4.1, run only the non-`django_db` tests (`pytest runs/tests.py`) with a dummy `VAULTSHIELD_MONGODB_URL=mongodb://localhost:1/vaultshield` exported so settings import succeeds (pytest-django only sets up the test database for `django_db`-marked tests; this is expected but not verified, so if collection still tries to connect, report those as not run too), and report `makemigrations`, `manage.py check`, acceptance 1 and every `test_mongodb.py` test as **not run**, naming the reason.
- New backend DB tests go in `backend/vaultshield_backend/test_mongodb.py` (every bare `test_mongodb.py:N` reference in this document means that file): a `TestRunStatusTransitions` class under `@pytest.mark.django_db` (allowed/refused per status through the API client, approve/reject unchanged, `updated_at` stored equals returned, as `TestApprovalAtomicity.test_updated_at_is_stored_exactly_as_returned` does at `:925-935`), and a separate `TestConcurrentStatusTransitions` under `@pytest.mark.django_db(transaction=True)` that reuses the `threading.Barrier` + `ThreadPoolExecutor` + `connections.close_all()` worker shape of `TestConcurrentSequenceAllocation._run_workers` (`:642-656`). It calls `apply_status_transition` directly from two threads (one `failed`, one `awaiting_review`, each on its own fresh `Run.objects.get`), collecting either the returned Run or the raised `StateTransitionError`. Assert exactly one success, one `StateTransitionError`, no other exception, and that the stored status equals the winner's target. Repeat 20 iterations to make a lost race likely to surface. Also in `TestRestApiAgainstMongoDB`: the sequential duplicate returns exactly `{"run_id": ["run with this run id already exists."]}`; the race uses `patch.object(rest_framework.validators.UniqueValidator, "__call__", return_value=None)` as fixed in D5 and returns the same body with exactly one stored Run; the `run_id` format cases from acceptance 7; and the stale-`status` `pr_url` PATCH test from D4. Non-DB tests go in `runs/tests.py`: the status choice set includes `failed`, the run_id regex validator is present, and `GATED_PATCH_TRANSITIONS` content.
- CLI: `cd cli && .venv/bin/pytest` (full), and report the Docker-gated skips. Frontend: `cd frontend && npm test`.
- Report counts for all three suites and list everything not run.

## 7. Points that change user-visible behaviour (for sign-off)

1. `vaultshield run` now needs a project, a login, and a reachable backend *before* anything runs (decided by the user; the details are in D7).
2. New `--project` option and `project:` config key, with precedence option > config.
3. Local preflight errors after registration (bad `--env-file`, Docker down) leave a `failed` Run in the dashboard (D8). The alternative is to register after `docker.from_env()`.
4. An upload failure marks the Run `failed`, which is terminal (D9).
5. POST `/api/runs/` rejects `run_id`s outside the safe grammar (D2).
6. PATCH `status=awaiting_review|failed` is gated to `running` (409 otherwise). Other status values stay unrestricted (D4, flagged).
7. Create can still set any status, and there's no per-owner authz (D5, D6, flagged).
8. `test_run_reports_a_missing_token_and_preserves_the_artifacts` is rewritten for the new abort-before-sandbox behaviour (§4.5).
9. `running → awaiting_review` is an explicit CLI `PATCH status=awaiting_review` after the last artifact (screenshots), gated in the view in `apply_decision`'s conditional-update style (D3/D4). It is not a side effect of `POST …/diff/` as the task suggested, because the diff is not the last artifact (`uploader.py:529-531`). Rejected alternative: the transition inside the diff view's `atomic()` block, which would expose a Run to approve before its screenshots land. Consequences: the backend never moves a Run on its own; any non-CLI uploader must send the PATCH; a screenshot upload failure ends in `failed`, not `awaiting_review`. The final report states where the transition lives and why.
10. Residual stuck-`running` cases, documented in the `run` comment block, `cli/README.md` and `backend/README.md` next to the SIGKILL caveat, with no code change: a `create_run` whose response is lost (timeout after the server committed) or a Ctrl-C during step 2b leaves a `running` Run that nothing marks failed, because the outer handler isn't entered until registration has returned. Every 2b abort also leaves an empty `.vaultshield/runs/<run_id>/`, because step 2 created it.
11. A `PATCH` without `status` now writes only the named fields (D4). Not observable through the API except that it can no longer overwrite a concurrent transition.

## 8. Tightening pass (no review existed)

No `batch2-design-review.json` was present, so this pass re-checked the design against the code and pinned down the decisions it had left loose. No product code was changed.

- `partial_update`: the exact override body is now given (D4). It validates first so a bad choice stays a 400, reaches the gated path only for `awaiting_review`/`failed`, writes `pr_url` in the same conditional update, and leaves PUT at 405. Two docstrings that the gating makes stale are now named for rewrite: `RunUpdateSerializer`'s "no transition restriction" bullet and `apply_decision`'s re-open paragraph. The `Approval` pre-check stays, because `running` → `awaiting_review` after a decision is still reachable.
- Duplicate-race test: the mechanism is fixed. `patch.object(UniqueValidator, "__call__")` lets the real Mongo unique index raise. The expected body is pinned to `RUN_ID_UNIQUE_MESSAGE`.
- Uploader transport: fixed as one-shot `httpx.post`/`httpx.patch`, with the same exception set as `_post` and the `validate_run_id` → `UploadError` guard used by `upload_run`. `create_run` does not send `status`, because the model default is `running` (`models.py:75`).
- `run` outer `try`: its exact start line is fixed, so step 3 (`--env-file`), step 4 and step 5 (`docker.from_env`) failures are all covered. A raising `teardown()` is also stated: the existing replacement behaviour stays, and there is still exactly one failed mark.
- 6(b) test: `git_capture` exports no empty-tree constant, so the test uses the real `resolve_diff_base` on a single-commit repo. It is skipped if `git` is missing.
- Dashboard: `failed` uses Tailwind `gray-*`, not `slate-*`. That keeps it distinct from `UNKNOWN_STATUS_STYLE`, so the test can tell the explicit entry from the fallback. The test has its own fixture so the shared `RUNS` array stays unchanged. Detail-view queries use the real labels `'Approve'`/`'Reject'` against the exported `DecisionControls`.
- Concurrency test: placed in a `transaction=True` class that reuses `TestConcurrentSequenceAllocation`'s worker shape.
- Uploader tests: `do_PATCH = do_POST` on the existing handler, and the timeout is asserted with an `httpx.patch` spy.
- Checked, no change needed: only one existing PATCH call exists in the backend tests (`test_mongodb.py:1502`, `pr_url` on an approved Run), and it takes the non-gated path. No frontend or CLI code PATCHes `status` today. (Corrected in §9: `test_sandbox_execution.py` also invokes `run`, four times.) The config-error `run` tests (`test_cli.py:193`, `:209`, `:1330`) fail in step 1 before project resolution, so they are unaffected.

No new open questions. The user-visible consequences already listed in §7 (notably D8: registration after step 2 means preflight failures leave a `failed` Run) follow the task text literally and are reported, not decided here.

## 9. Responses to the design review (`batch2-design-review.md`)

All 12 findings are addressed in place. None needed a user decision: each one either fixes this design's own inconsistency or adds detail, and none changes behaviour beyond the task. No product code was changed.

| # | Sev | Response |
|---|---|---|
| 1 | HIGH | Addressed. Confirmed in `test_sandbox_execution.py:306-317, :461-475, :522-536, :562-576`: each invokes `run` with only `--backend-url=http://127.0.0.1:1`. §4.5 now has the `_offline_backend` stub helper, `--project` on each argv, `len(marks) == 1` on all four, and a stage-tying assertion for `:524` (`:463`/`:564` already assert stage-specific text). The four are named as not-run if Docker or the image is missing. The wrong §8 claim is corrected. |
| 2 | MED | Addressed. §4.5 lists every `run` call site in the three test files, including the inline argv at `test_cli.py:638-649`, and says which are left alone (Click-choice rejection, `--help`, `teardown`). |
| 3 | MED | Addressed with the preferred fix. Confirmed that DRF's `ModelSerializer.update` does a bare `instance.save()` (`rest_framework/serializers.py:1064`). `RunUpdateSerializer.update` now saves `update_fields=[*validated_data, "updated_at"]` (D4, §4.1), with a new DB test for the stale-status case. D4's race claim is reworded to what actually holds: an explicit non-gated `status` write stays unconditional, as today and flagged. |
| 4 | MED | Addressed. Confirmed `get_unique_error_message` (`field_mapping.py:335-342`) yields `"run with this run id already exists."`. The conflicting D2 sentence is replaced, the constant is pinned to that text, and a byte-identical body test is in §6. |
| 5 | MED | Addressed. §4.4 gives the exact `_mark_run_failed_best_effort`, with the call guarded by `except BaseException` (a one-line, token-free warning) and the echo guarded too. It also states why swallowing a `KeyboardInterrupt` there is safe. |
| 6 | MED | Addressed. §7 item 9 names the placement, the reason and the rejected alternative. D3 explains why it still meets "after the diff upload, in apply_decision's atomic style", and the final report must state it. |
| 7 | NIT | Addressed. §7 item 10, plus the README and `run` comment-block caveat. No code change. |
| 8 | NIT | Addressed. `httpx.Timeout(5.0, connect=2.0)`, the NFR reworded to per-phase, and the spy test asserts the object. |
| 9 | NIT | Addressed. The copy is kept with the import-cycle comment (`submit.py:260` imports `uploader`). |
| 10 | NIT | Addressed. §4.1 lists the `RunViewSet` and `StateTransitionError` docstrings and `scheduled.py:86-90` / `:193`. The optional `sandbox.py:308-318` `read_output(redact=...)` docstring is backlogged: it's pre-existing product code outside the task's items, so editing it would be an unrelated change. The final report flags it. |
| 11 | NIT | Addressed. The refusal text is built from `GATED_PATCH_TRANSITIONS[target]`, which is passed in as `allowed=` to avoid a definition-order dependency. |
| 12 | NIT | Addressed. §6 now names only the D5 mechanism. |

Still unverified until implementation: the two-gated-writers concurrency behaviour (one 200, one 409, no 500 within 5 transient retries) needs the Mongo replica set. `Model.save(update_fields=...)` under `django_mongodb_backend` is expected to issue a `$set` of only those fields, and the D4 stale-status test checks this directly.

## 10. Responses to the design review, round 2

All four findings are addressed in place. None changes user-visible behaviour beyond the task, so there are no open questions. No product code was changed.

| # | Sev | Response |
|---|---|---|
| 1 | MED | Addressed. Confirmed `settings.py:140-147` raises `ImproperlyConfigured` with no URL. §4.1 now says every `manage.py` command needs a reachable MongoDB, pins the exact `0002_alter_run_status.py` in the style of `0001_initial.py` (whose 5-tuple choices, `default='running'`, `max_length=32` were re-read), and gives the fallback: hand-write that file and report acceptance 1 and `manage.py check` as not run. §6 now starts the replica set and exports both URLs before any `manage.py` command. Acceptance 1 is reworded to match. |
| 2 | NIT | Addressed. §4.4 separates Ctrl-C while output streams (`cli.py:969`, becomes `AuthError`) from Ctrl-C/EOF at `click.prompt` (`cli.py:927`, Click's `Abort`). Both reach the outer handler. `click.exceptions.Abort` is added to acceptance 9, with a test in `test_interactive_auth.py` (§4.5). |
| 3 | NIT | Addressed. The 2b message is now `f"Cannot start run {run_id}: {exc} (no Sandbox was created)"`. |
| 4 | NIT | Addressed. D4 makes `target` and `recorded_decision` mutually exclusive, enforced first in `__init__`. It raises `TypeError` instead of using `assert` so the check survives `python -O`. A unit test is in `runs/tests.py`. |
