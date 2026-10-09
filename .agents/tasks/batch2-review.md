# Run registered at `vaultshield run` start, gated `failed`/`awaiting_review` lifecycle

Batch 2 moves Run creation from "never" to step 2b of `vaultshield run`: the CLI resolves a project (`--project` > `.vaultshield.yml: project` > one-line error), loads the token and POSTs the Run before any Docker call. Everything after that sits inside one outer `except BaseException` that makes exactly one best-effort `PATCH status=failed` after the inner teardown `finally`, then re-raises the original exception. On the backend, `failed` is the sixth status, and PATCH to `failed` or `awaiting_review` is a conditional atomic update allowed only from `running`, the same pattern as `apply_decision`. Duplicate `run_id` returns 400 in both the sequential and race paths, and the CLI sends the completion signal (`awaiting_review`) as an explicit PATCH after the last screenshot rather than on diff ingest. The dashboard gets a grey `failed` pill, and carried items 6(a)–(d) are in.

Watch for: nothing blocking. There are four NITs: a misleading "Cannot register run" guard message reused by the completion and failed-mark paths (confirmed); one stale "four statuses" comment in the dashboard (confirmed); a pre-existing Docker-gated CLI test that fails plus one that hangs, both of which the final report must name (confirmed); and a slate-vs-gray contradiction inside the design's acceptance criteria (confirmed).

**Verdict**: APPROVED

## High-level view

Registration comes before any side effect except the Run_Directory. `docker.from_env` and `Sandbox` both sit inside the outer `try`, which opens on the line right after the 2b echo. So a missing token, an unreachable backend, a 401 or a refused project aborts with `Cannot start run … (no Sandbox was created)`, and no container exists. Re-run tests confirm that `Sandbox` is never constructed and `docker.from_env` is never called on those paths.

The failure handler fits the "exactly one, after teardown, never masking" contract. The inner `finally: sandbox.teardown()` finishes before the outer `except BaseException` runs. That handler calls `_mark_run_failed_best_effort`, which swallows everything, including a `KeyboardInterrupt` during the PATCH, and adds at most one `Warning:` line. Then a bare `raise` re-raises the original. A 14-stage parametrized test plus the Abort-at-prompt test cover every stage in acceptance 9.

On the backend, both gated transitions share one `apply_status_transition`, a copy of `apply_decision`'s shape: inside `transaction.atomic()` a `filter(pk, status__in=allowed).update(...)` runs, and zero rows updated becomes a 409 `StateTransitionError`. The non-gated PATCH now saves `update_fields` only, so a `pr_url` PATCH can't write a stale status back over a concurrent transition. `submitted`, `approved` and `rejected` are excluded by the filter and can't become `failed`. The migration matches §4.1 byte for byte after the header.

Known residual gaps are documented in `cli.py`, `cli/README.md` and `backend/README.md` and signed off in design §7: a SIGKILL, a lost `create_run` response, or Ctrl-C during 2b leaves a Run in `running`, and preflight failures after 2b (bad `--env-file`, Docker down) leave a `failed` Run (D8).

<details>
<summary>Issues (4)</summary>

1. **[NIT] Guard message says "register" on every path.** `_guard_run_id` raises `Cannot register run …` for `complete_run` and `mark_run_failed` too, so an invalid id at completion reads as a registration error. Make the message neutral (`Invalid run id …`) or pass the verb in.
2. **[NIT] Stale status count in `DecisionControls` doc.** `RunDetailView.jsx:443` still says "the other four statuses". Change it to five, as the two other comments in that file already were.
3. **[NIT] Docker-gated CLI suite is not green, and the final report must say so.** `test_exec_interactive_really_delivers_stdin_to_a_container_process` fails (`RECEIVED:[***]`) for the same `send_line`-arms-redaction reason 6(c) fixed. `test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever` hangs, because docker-py's `poll()` ignores `settimeout` (a `sandbox.py` product bug). Both are pre-existing and unchanged by Batch 2. Report them by name with the full-suite counts. Optionally apply the 6(c) raw-socket fix to the first one, if the user agrees to the scope.
4. **[NIT] Design acceptance 12 contradicts §4.6.** Acceptance 12 says "the slate style", but §4.6 pins `gray` (and the code and test follow §4.6 and the user's "neutral/grey"). State the discrepancy in the final report so nobody "fixes" the badge back to slate, which would make it indistinguishable from the unknown-status fallback.

</details>

<details>
<summary>Details</summary>

### Evidence re-checked in this review

These results were re-run by this review, not copied from `batch2-verification.md`:

- Backend, against the live `vs-test-mongo` replica set: `makemigrations --check --dry-run` reports "No changes detected", and `pytest -q` gives 326 passed.
- CLI: `test_cli.py`, `test_interactive_auth.py`, `test_uploader.py`, `test_config.py` and `test_git_capture.py` give 267 passed.
- Frontend: `CI=true npm test -- --ci` gives 60 passed in 7 of 7 suites.
- Two Docker-gated tests, run directly: 6(c) `test_a_tty_echoes_stdin_and_redaction_removes_it` PASSED, and `test_exec_interactive_really_delivers_stdin_to_a_container_process` FAILED (confirmed, see issue 3).
- Repository state: `git diff --cached --quiet` passes (nothing staged), `security_scan/rules.py` is unchanged, and the only diff in `sandbox.py` is the earlier task's 11-line `stdout_bytes` edit.

Not re-run here: the hanging sign-in test and the four Docker `run` tests. The verification log reports the four `run` tests as passed.

### Step 2b and the outer handler in `run`

```
step 1  load_config → _resolve_project          (no project → abort, no run dir)
step 2  create_run_dir
step 2b load_token + uploader.create_run        (UploadError → "Cannot start run … (no Sandbox was created)")
        ── outer try ─────────────────────────────────────────────
        step 3 credential file, step 4 adapter, step 5 docker.from_env, Sandbox(...)
          ── inner try ── create / auth / supervise / capture / git ──
          ── finally: sandbox.teardown() ──
        step 10 _upload_run_artifacts(token)
        step 11 _complete_run  → PATCH awaiting_review
        except BaseException: _mark_run_failed_best_effort(); raise
```

The ordering is structural, so "teardown first, then one failed PATCH" holds for every exception type, including the ones the inner clauses don't map (KeyboardInterrupt, Click's `Abort` from `click.prompt`). The tests assert it by order, not just presence: `calls.index("sandbox.teardown") < calls.index("backend.failed")`, `calls[-1] == "backend.failed"` and `calls.count(...) == 1`, for all 14 stages. `test_a_failed_mark_that_fails_does_not_change_the_original_error` compares the normalised error line against a baseline run, for both a returned warning and a raising stand-in. That is a stronger check than looking for a substring.

The upload and completion stages sit inside the outer `try`, so a transient upload error now ends in a terminal `failed` (D9). The completion-response-lost case comes out as a 409 warning on the failed mark rather than a second error. Both were signed off.

The `_guard_run_id` helper (`uploader.py:700-705`) is shared by all three Run-control calls but hard-codes the word "register" (issue 1, confirmed). It is only reachable with a hand-supplied invalid id, because `run` always passes a generated one, so it's cosmetic.

### Gated PATCH and the duplicate-`run_id` race

`partial_update` validates first, so 404 and 400 stay as before, then sends `awaiting_review` and `failed` to `apply_status_transition`. Any `pr_url` in the same body goes into the same conditional `update()`, so a refused transition writes nothing. `test_a_gated_status_is_refused_from_anything_but_running` asserts that across 10 combinations, with an aged `updated_at` so an accidental bump would show. The 20-iteration barrier race calls `apply_status_transition` directly rather than through HTTP. Because the view adds nothing around that call, this still covers acceptance 4.

`perform_create` maps `IntegrityError` to `{"run_id": [RUN_ID_UNIQUE_MESSAGE]}` only when the row now exists, and anything else stays a 500. The race test patches `UniqueValidator.__call__`, so the real Mongo unique index raises, and it asserts `bypassed.called`, so the 400 can't have come from the pre-check. `ATOMIC_REQUESTS` is not set, so the existence re-check isn't running inside an aborted transaction.

### Pre-existing Docker-gated failures (out of scope, must be reported)

`test_exec_interactive_really_delivers_stdin_to_a_container_process` (`test_sandbox_execution.py:1183`) asserts the raw `RECEIVED:[<sentinel>]` after a `send_line`, which at HEAD already arms redaction. So it fails as soon as the image exists (confirmed by this review's run). The hang at `:1552` lives in `sandbox.py`/docker-py, which is off-limits. Neither test nor its code is in the Batch 2 diff, but the user asked for full-suite counts, so the final report has to give the CLI result as N passed, 1 failed, 1 not run (hangs), not as a clean pass (issue 3).

</details>

<details>
<summary>File map</summary>

Batch 2 scope only. The patch passthrough, non-UTF-8 and interactive-auth stand-in edits come from earlier tasks.

- `backend/runs/models.py`: `failed` choice and lifecycle comment.
- `backend/runs/migrations/0002_alter_run_status.py`: generated AlterField (untracked).
- `backend/runs/serializers.py`: `RUN_ID_PATTERN`/`RUN_ID_UNIQUE_MESSAGE`, explicit run_id validators, `RunUpdateSerializer.update` with `update_fields`, docstrings, 6(a).
- `backend/runs/views.py`: `GATED_PATCH_TRANSITIONS`, `apply_status_transition`, `StateTransitionError(target, allowed)`, `partial_update`, `perform_create`.
- `backend/tasks/scheduled.py`: docstrings only.
- `backend/runs/tests.py`, `backend/vaultshield_backend/test_mongodb.py`: non-DB and replica-set tests.
- `backend/README.md`, `cli/README.md`: lifecycle and `--project` docs.
- `cli/vaultshield/config.py`: `ProjectConfig.project`.
- `cli/vaultshield/uploader.py`: `create_run`, `complete_run`, `mark_run_failed` and helpers.
- `cli/vaultshield/cli.py`: `--project`, `_resolve_project`, step 2b, outer handler, `_complete_run`, `_mark_run_failed_best_effort`, token passed to upload.
- `cli/tests/test_cli.py`, `test_interactive_auth.py`, `test_uploader.py`, `test_config.py`, `test_sandbox_execution.py`: wiring stubs, stage-failure matrix, 6(b), 6(c), 6(d), Abort test, `_offline_backend`.
- `frontend/src/RunHistoryView.jsx` (+test), `RunDetailView.jsx` (+test): `failed` pill, no decision controls.

Full diff: `git -C /Users/aayush/Codes/VaultShield diff` plus `backend/runs/migrations/0002_alter_run_status.py`.

</details>
