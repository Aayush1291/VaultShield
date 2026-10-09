# Batch 2 verification evidence

## 2026-10-08 23:26 IST — convergence iteration 1 (no batch2-review.md existed)

Environment: Docker up; `vs-test-mongo` (mongo:7, replica set rs0, PRIMARY, port 27018) reused from FEAT-001 and left running for the final step; image `vaultshield-sandbox:local` present (built in FEAT-002). URLs exported:
`VAULTSHIELD_MONGODB_URL=VAULTSHIELD_TEST_MONGODB_URL='mongodb://localhost:27018/vaultshield?directConnection=true'`.

### Commands and results

Backend (`cd backend`):
- `.venv/bin/python manage.py makemigrations --check --dry-run` -> `No changes detected` (run before and after the comment edit below).
- `.venv/bin/python manage.py check` -> `System check identified no issues (0 silenced).`
- `.venv/bin/pytest -q` (full, replica set) -> **326 passed**, 0 failed, 0 skipped (before and after the edit below).
- `VAULTSHIELD_MONGODB_URL=mongodb://localhost:1/vaultshield .venv/bin/pytest -q runs/tests.py` -> **72 passed**.
- `git diff --quiet backend/security_scan/rules.py` -> unchanged. `cli/vaultshield/sandbox.py` diff vs HEAD is still only the earlier `stdout_bytes` edit (11 lines). Nothing staged (`git diff --cached` empty).

CLI (`cd cli`):
- `.venv/bin/pytest -q -rfEs --deselect tests/test_sandbox_execution.py::test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever` -> **728 passed, 1 failed, 1 deselected, 0 skipped** (60.5 s). The failure and the deselected test are below.
- An earlier undeselected full run gave 728 passed / 2 failed, but only because the tool's 120 s limit sent SIGINT into the hanging test (it then reported "sign-in was cancelled"). That run's count is not used.
- `.venv/bin/pytest -q -rA tests/test_sandbox_execution.py -k "<the 4 run tests + 6(c)>"` -> 5 PASSED: `test_agent_executes_inside_the_sandbox_and_cannot_reach_the_host`, `test_off_limits_violation_is_detected_inside_the_sandbox`, `test_a_crashing_agent_inside_the_sandbox_fails_the_run`, `test_an_adapter_without_sandbox_support_is_refused`, `test_a_tty_echoes_stdin_and_redaction_removes_it` (6(c)).

Frontend (`cd frontend`, node_modules from FEAT-003's `npm ci`):
- `CI=true npm test -- --ci` -> Test Suites **7 passed / 7**, Tests **60 passed / 60**.
- `npm run build` -> built OK (36 modules; `bg-gray-100` present in the CSS). The generated `dist/` (git-ignored) was deleted afterwards.

### Pre-existing CLI failures (not caused by Batch 2; not fixed)

Both are Docker-and-image-gated. They were skipped in every earlier baseline (the image was absent) and only surfaced once FEAT-002 built `vaultshield-sandbox:local`. Neither the tests nor the code they exercise were changed in Batch 2, and the user's items name only the 6(c) test.
1. `test_sandbox_execution.py::test_exec_interactive_really_delivers_stdin_to_a_container_process` -> FAILS: it writes with `send_line`, which arms redaction (committed `sandbox.py` behaviour), so the process's own `RECEIVED:[<sentinel>]` comes back as `RECEIVED:[***]`. Same root cause as 6(c). It could be fixed in the test only, by writing the sentinel to the raw socket as the 6(c) hazard pass does. That is outside the stated scope, so it's left for the reviewer or user.
2. `test_sandbox_execution.py::test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever` -> **HANGS** (NOT RUN to completion). Run alone with a 75 s watchdog, it was still blocked after 75 s even though the deadline is patched to 3 s; it was then terminated. A faulthandler dump shows it blocked in `docker/utils/socket.py:read` -> `poll.poll()` with no timeout. docker-py polls before `recv`, so the `settimeout(timeout_s)` in `Sandbox.exec_interactive` never fires. This is a real product bug: a silent in-Sandbox sign-in can hang `vaultshield run` forever. It lives in `cli/vaultshield/sandbox.py` (off-limits for this batch) or in docker-py, so it is reported, not fixed. It leaves a `vaultshield-run-20250614-091530-authhang000001` container behind when killed; I removed that one with `docker rm -f`. The 6(d) unit test (fake `timed_out=True`) still covers the CLI's reporting of a timeout.

### Cross-FEAT seam check against the live backend (temporary, cleaned up)

`manage.py migrate` into a scratch DB `vaultshield_seam` on vs-test-mongo, with a scratch user, token and Project, and `runserver 127.0.0.1:8765` with `CELERY_BROKER_URL=memory://`. Then the real CLI was run against it:
- `uploader.create_run` -> 201. Duplicate -> `UploadError` quoting `{"run_id":["run with this run id already exists."]}`. Unknown project -> quotes `{"project":[...]}`. Bad token -> 401 message naming `vaultshield login --backend-url`. Unreachable -> `Cannot reach the VaultShield backend at ...`.
- `complete_run` -> 200, then 409 `...no longer running (backend reports `awaiting_review`)`. `mark_run_failed` -> None, then a returned 409 warning naming `failed`. `complete_run` on a failed Run -> 409 naming `failed`. Unknown run / bad token -> returned warnings, no raise.
- approve on `awaiting_review` -> 200. approve on `failed` -> 409 `{"detail": "...this Run is `failed`.", "current_status": "failed"}`.
- Full `vaultshield run --agent=mock` (real Sandbox, git daemon repo, HOME pointed at a scratch token) -> exit 0. Server log: `POST /api/runs/` 201 -> events 201 -> diff 201 -> `PATCH` 200. Stored status `awaiting_review`, diff entry has `patch`, 7 events.
- `--env-file=/nonexistent/.env` -> registered, `Error: --env-file not found`, and the Run is stored `failed` (D8 as designed).
- `off_limits_paths` violation -> Sandbox torn down, original error printed, then one `PATCH` 200 (failed).
- Backend down / bad project / bad token / no token -> one-line `Cannot start run ... (no Sandbox was created)`. The Docker container count was unchanged (7 -> 7) and the Run was never stored (GET 404). No project -> the D7 message and no Run_Directory.
- Cleanup: server and git daemon stopped, scratch DB dropped, temp dirs removed, no `vaultshield-*` containers left.

No mismatch was found between the CLI messages and paths and the backend's real responses.

### Acceptance criteria (design §2) and plan items

- 1: migration matches §4.1 (FEAT-001 diffed it). `makemigrations --check` clean. **Holds.**
- 2–7: backend DB tests in the 326 (FEAT-001 `-rA` list), plus the live seam check. **Hold.**
- 8–11: test_cli / test_interactive_auth / test_uploader tests in the 728, plus the live seam check. **Hold.**
- 12: frontend tests in the 60. §4.6 pins gray, not slate (FEAT-003 note). **Holds.**
- 13: 6(a) docstring; 6(b) `test_run_reports_diff_metadata_over_the_upload_cap`; 6(c) PASSED with Docker; 6(d) `test_a_sign_in_that_goes_silent_is_reported_as_a_timeout`. **Hold.**
- Plan items 1–11: implemented per the FEAT findings. Item 12 (stop vs-test-mongo) is left to the final step, as instructed.

### Fixes made in this iteration (docs/comments only, no behaviour change)

- `backend/README.md` "Run lifecycle": the bullet "`failed` is terminal: nothing moves a `failed` Run on to review" was false, because an unrestricted `PATCH status=running` followed by the completion PATCH would. It now says terminal *for the CLI* and names the hand-PATCH exception and the follow-up.
- `backend/runs/models.py` `STATUS_CHOICES` comment: the same correction. Comment only; `makemigrations --check` is still clean and the backend suite still gives 326 passed.

### Not run

- `test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever`: hangs indefinitely (see above).
- Nothing else. Every other CLI test ran, with 0 skips.
