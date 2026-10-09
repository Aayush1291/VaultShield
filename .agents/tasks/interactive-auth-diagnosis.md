# Diagnosis: 17 failures in `cli/tests/test_interactive_auth.py`

## Verdict

The production code is internally consistent and correct; the test stand-ins are
stale. Fix the stand-ins in `cli/tests/test_interactive_auth.py` only. Do not touch
`cli/vaultshield/cli.py` or `cli/vaultshield/sandbox.py`.

Baseline (working tree, `cd cli && .venv/bin/python -m pytest -q`):
17 failed, 645 passed, 22 skipped. All 17 failures are in this file, all
`TypeError: _Sandbox.exec_interactive() got an unexpected keyword argument 'timeout_s'`
raised at `cli.py:903` (`_authenticate_in_sandbox`).

## Which change introduced it: commit `11a6e5c` "done 5"

`git log --oneline --all`: `0dc3a55 first commit`, `6dab1a8 done 1` ... `11a6e5c done 5` (HEAD,
= origin/main). There are no `.kiro/specs` or task files mentioning this feature.

"done 5" is the commit that added the entire `--interactive-auth` feature at once:
`Sandbox.exec_interactive` + `InteractiveExec` (sandbox.py, +590 lines),
`_authenticate_in_sandbox` (cli.py, +206), `ANTIGRAVITY_AUTH_TIMEOUT_S = 75.0`
(adapters/antigravity.py), and `tests/test_interactive_auth.py` (+614, new file).
`git log -S timeout_s` lists only `0dc3a55` (the unrelated `ready_timeout_s` /
`ReadinessTimeout.timeout_s` in browser_capture.py) and `11a6e5c`. So the stand-in
never matched any committed contract. It was written against an earlier,
uncommitted iteration of the auth path, and production moved on inside the same
commit. The docstrings in cli.py and sandbox.py describe that earlier iteration.
They mention a "two-pass read" that "lost the exchange", an "earlier version
[that] slept here", and redaction that is now armed "inside `send_line`" rather
than passed as `redact=`.

The working tree's uncommitted changes do not touch any of this. `git diff` on
cli.py/sandbox.py contains no interactive/timeout/redact/read_output lines.

## Real contract (production, what the fakes must model)

`Sandbox.exec_interactive` (sandbox.py:803):

```python
def exec_interactive(self, cmd: list[str], *, workdir: str | None = None,
                     timeout_s: float | None = None) -> "InteractiveExec"
```

- No `env`/`environment` parameter, by design. `test_exec_interactive_accepts_no_environment_parameter` asserts this on the real class.
- `timeout_s`, if not None, is applied with `socket.settimeout(timeout_s)` on the raw exec socket. It bounds each read, not the total time.
- Raises `SandboxError` if no container exists or the Docker API refuses the exec.
- Returns `InteractiveExec(api, exec_id, socket, tty=True)`.

`InteractiveExec` (sandbox.py:293, mutable dataclass):

- `send_line(text)` sets `self._secret = text` (redaction arms itself on send) and then writes `text + "\n"` to the socket.
- `read_output()` takes no arguments. It is a single-pass generator that yields scrubbed lines (`_scrub` replaces `_secret` with `_REDACTED = "***"`) and also flushes a pending tail ending in `:`. On `socket.timeout` it sets `self.timed_out = True` and stops without raising. On `OSError` it stops quietly. The docstring says to iterate it exactly once and to send from inside the loop.
- `flush_remaining()` takes no arguments and returns the scrubbed leftover buffer.
- `timed_out: bool = False` is a field.
- `wait()` returns `int | None`. `close()` never raises.

## How `cli.py` drives it (`_authenticate_in_sandbox`, cli.py:831-982)

1. `invocation = adapter.auth_invocation()`. If it is `None`, the function echoes "needs no in-Sandbox sign-in" and returns.
2. It prints the banner (scopes, "broad, not narrow", "read its own authentication session", "host Antigravity login is not read").
3. It calls `sandbox.exec_interactive(invocation.argv, workdir=Sandbox.WORKDIR, timeout_s=ANTIGRAVITY_AUTH_TIMEOUT_S)`. This call is outside the `try`, so a `KeyboardInterrupt` raised here bypasses the `AuthError` conversion, and `run`'s `finally` still tears down.
4. It makes one pass: `for line in exec_handle.read_output():` (no args). It echoes each line. On the first line containing `"paste the authorization code"` it calls `click.prompt(hidden)`. An empty code raises `AuthError`. Otherwise it calls `exec_handle.send_line(code)` inside the loop.
5. It calls `exec_handle.flush_remaining()` (no args).
6. If `exec_handle.timed_out` is set, it raises `AuthError` ("produced no further output for 75s").
7. If the marker was never seen (`not asked`), it raises `AuthError` ("ended before asking for an authorization code").
8. It calls `exec_handle.wait()`. If the result is not in `(0, None)`, it raises `AuthError` ("sign-in failed ...").
9. `KeyboardInterrupt` becomes `AuthError` ("cancelled"). The `finally` calls `exec_handle.close()`.

There are no other callers of `exec_interactive` in `vaultshield/`. adapters/base.py and antigravity.py only reference it in docstrings. Production passes only the kwargs the real method accepts, so production is not wrong.

## Stand-in as it is today, and every divergence the real path exercises

The `_Sandbox` fake (inside `_wire`, line 167) has `def exec_interactive(self, cmd, *, workdir=None)`. The `_FakeInteractiveExec` fake (line 86) has `read_output(self, *, redact=None)` with a two-phase `_phase` counter, `send_line` that only appends, `flush_remaining(self, *, redact=None)`, and no `timed_out`.

| # | Divergence | Real | Stand-in | Effect once the previous one is fixed |
|---|---|---|---|---|
| D1 | `exec_interactive` kwargs | `*, workdir=None, timeout_s=None` | `*, workdir=None` | All 17 failures today (TypeError). |
| D2 | `timed_out` attribute | dataclass field, default `False` | absent | Measured with only D1 fixed: 16 fail with `AttributeError: '_FakeInteractiveExec' object has no attribute 'timed_out'` at cli.py:949. The 17th (`test_no_code_entered…`) passes because it raises before that line. |
| D3 | Read protocol | ONE pass over `read_output()`; the post-send echo arrives later in the same iteration | The prompt lines are yielded on the first call. The echo and `after_send` are yielded only on a SECOND call, which is the old two-pass protocol | With D1 and D2 fixed, the echo is never yielded. Measured: `test_it_is_never_echoed_to_the_terminal` fails on `assert "***" in result.output`. |
| D4 | Where redaction is armed | `send_line` sets `_secret`, and `read_output()` scrubs with it | `read_output(redact=...)` scrubs only when the caller passes `redact`, and cli.py never does | The same test would print the raw code once D3 is fixed. D3 and D4 have to be fixed together. |
| D5 | `read_output` / `flush_remaining` signatures | no parameters | keyword-only `redact` | No runtime failure, because cli calls them without args. It is still a divergence. Remove it so a future `redact=` call fails in the unit tests, as it would in production. |

Things that already match and need no change: `create(...)` kwargs, `teardown()`, `WORKDIR`/`REPO_DIR`, `wait()`, `close()`, and `send_line` recording `sent`. The other stage fakes also match their real call sites:

- `_supervise(sandbox, adapter, prompt, run_dir, *, off_limits_paths, **kwargs)` absorbs `agent_workdir`.
- `_capture(sandbox, config, run_dir, ready_timeout_s)` is called positionally.
- `_commit(sandbox, run_id)`.
- `_diff(sandbox, base_ref, branch, run_dir)`.

Prototype check: I applied D1-D5 to a throwaway copy in `/tmp` (since deleted, no repo edits). Result: 34 passed, 0 failed.

`test_teardown_after_a_cancelled_signin` passes today only by accident. The TypeError gives a non-zero exit, teardown still runs, and supervise is never called. With D1 fixed, it passes for the intended reason. `auth_raises=KeyboardInterrupt()` is raised from `exec_interactive`, outside cli's `try`. Click turns it into "Aborted!" with exit 1, and `run`'s `finally` tears down.

The real timeout path (`timed_out=True`, then AuthError "produced no further output") has no unit test. I am not adding one, because that is out of scope for "fix the stand-in". This gap goes in the report.

## The earlier `resolve_diff_base` monkeypatch: correct

`monkeypatch.setattr(cli_module, "resolve_diff_base", lambda sandbox, branch: f"{branch}~1")`
matches `cli.py:745` `base_ref = resolve_diff_base(sandbox, branch)` and the real
signature `git_capture.resolve_diff_base(sandbox: Sandbox, branch: str) -> str`.
cli.py imports the name into its own namespace (`from vaultshield.git_capture import ... resolve_diff_base`),
so patching `cli_module` is the right target. Without it, the real function would
call `sandbox.exec` on the fake `_Sandbox`, which has no `exec`, and raise AttributeError.
The patch also keeps step 8 off the real code path, which matters because
`_diff` writes a static `{"files": []}`. Confirmed by the prototype: all
`--interactive-auth` tests reach `git.diff` and pass with it in place.

## Related stale code outside the requested scope (report, do not fix)

`tests/test_sandbox_execution.py::test_a_tty_echoes_stdin_and_redaction_removes_it`
(line ~1180) is Docker-gated and always SKIPPED here (no daemon or image). It is
stale against the same "done 5" contract in two ways:

- It calls `guarded.read_output(redact=_STDIN_SENTINEL)`, which would raise TypeError.
- Its "bare" half expects the raw echo after `send_line`, but the real `send_line` now always arms redaction, so that assertion would fail too.

The comment at line ~1131 also still mentions `read_output(redact=...)`. The user's task names only `test_interactive_auth.py`, and these tests cannot run in this environment, so the fix leaves them alone. They must be called out in the final report.

# Implementation Plan

- [ ] 1. Bring the two stand-ins in `cli/tests/test_interactive_auth.py` in line with the real contract (D1-D5). Change no assertion and no production file.
      - `_Sandbox.exec_interactive` (line 167): change the signature to `def exec_interactive(self, cmd, *, workdir=None, timeout_s=None):` and keep the body. The `calls`/`seen` recording and `auth_raises` stay as they are.
      - `_FakeInteractiveExec.__init__`: replace `self._phase = 0` with `self._secret: str | None = None` and `self.timed_out = False`, mirroring the real dataclass fields.
      - `send_line`: set `self._secret = text`, then append to `self.sent`. This mirrors the real arm-on-send.
      - `read_output(self)` (no params): make it a single pass. Yield each `prompt_lines` entry scrubbed with `self._secret`. Then, if `self.sent` is non-empty, yield each sent value (the TTY echo) and each `after_send` line, all scrubbed with `self._secret`. Because the generator is lazy, cli's `send_line` call made while the last prompt line is being handled has already armed `_secret` before the echo is produced. This matches real behaviour.
      - `flush_remaining(self) -> str`: drop the `redact` param and return `""`.
      - Keep the module-level `_scrub` helper (`"***"`, the same value as `sandbox._REDACTED`).
      - Update the `_FakeInteractiveExec` docstring in the file's existing prose style. It should say that redaction is armed by `send_line`, as in `InteractiveExec.send_line`, that `read_output` is iterated exactly once with the code sent from inside the loop, and that `timed_out` mirrors the real field. Remove the stale "replays the code it was sent" two-call wording.
      Files: cli/tests/test_interactive_auth.py
      Verify: `cd /Users/aayush/Codes/VaultShield/cli && .venv/bin/python -m pytest tests/test_interactive_auth.py -q`. Expect 34 passed, 0 failed. Also run `git diff --stat -- cli/vaultshield` and confirm it is unchanged from the pre-task state: the stand-in fix adds no production diff, and the existing approved diffs to cli.py, git_capture.py and sandbox.py stay as they are.

- [ ] 2. Run the full CLI suite and record the result.
      Files: none
      Verify: `cd /Users/aayush/Codes/VaultShield/cli && .venv/bin/python -m pytest -q -rs`. Expect 662 passed, 0 failed, 22 skipped (baseline was 17 failed + 645 passed + 22 skipped). The 22 skips are the Docker/image-gated `test_sandbox_execution.py` tests. The report must state:
      - the D1-D5 mismatch, and that it was introduced in commit `11a6e5c` "done 5";
      - that production was not changed;
      - that the stale Docker-gated `test_a_tty_echoes_stdin_and_redaction_removes_it` (`read_output(redact=...)`) exists and could not be run here;
      - that the `timed_out` path has no unit test.

Constraints: do not use `git stash`, `git checkout --`, or anything else that could disturb the uncommitted approved work. Do not commit unless the workflow's finalize step does so. No unrelated refactors.
