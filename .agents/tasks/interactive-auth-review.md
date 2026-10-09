# Align the interactive-auth test stand-ins with the "done 5" InteractiveExec contract

The 17 failures in `cli/tests/test_interactive_auth.py` came from test fakes written against an earlier, uncommitted iteration of the `--interactive-auth` path. Commit `11a6e5c` "done 5" shipped a production contract that had already moved on: `exec_interactive(..., timeout_s=)`, a `timed_out` field, a single-pass `read_output()`, and redaction armed inside `send_line`. The fix edits only the two fakes (`_Sandbox.exec_interactive` and `_FakeInteractiveExec`) so they model that contract. It changes no assertion and no production file. The earlier `resolve_diff_base` monkeypatch is retained, and it is correct.

Watch for: no blocking issues. Two coverage gaps are out of scope and were flagged by the diagnosis. The `timed_out` AuthError path has no unit test (confirmed). The Docker-gated `test_a_tty_echoes_stdin_and_redaction_removes_it` still calls `read_output(redact=...)` and is stale (confirmed). It is always skipped here.

**Verdict**: APPROVED

## High-level view

The diff resolves all five divergences D1-D5 from the diagnosis. `_Sandbox.exec_interactive` now accepts `timeout_s`. `_FakeInteractiveExec` has a `timed_out = False` attribute and a `_secret` that `send_line` sets. `read_output()` and `flush_remaining()` take no parameters. `read_output` is a single lazy pass that yields the TTY echo and `after_send` lines once something has been sent. Because the generator is lazy, the code that cli.py sends from inside the loop is already armed for scrubbing by the time the echo is produced. That matches `InteractiveExec` (sandbox.py:293-400). Because the fakes no longer accept `redact=`, any future call that passes it fails in the unit tests, as it would in production.

No assertions were touched. `test_it_is_never_echoed_to_the_terminal` still requires `***` to appear and the raw code to be absent. It now passes because the fake reproduces the real echo-plus-redaction behaviour, not because the check was relaxed. The docstring was rewritten in the file's prose style. Test fakes in this file carried no Requirement tags before the change and none were added. The diff contains no refactors beyond D1-D5.

The production diff in `cli/vaultshield/` is only the previously approved work. In sandbox.py that is `ExecResult.stdout_bytes`, plus the cli.py `resolve_diff_base` wiring and git_capture.py. It contains no lines that touch interactive, timeout, redact, read_output, send_line or timed_out. Nothing is staged and HEAD is still `11a6e5c`. `.DS_Store`, both READMEs, the backend serializers/models/tests and the other CLI test files are still modified and uncommitted.

On-disk evidence shows tests ran after the edit. The test file was modified at 11:34:37. Pytest's `lastfailed` cache was rewritten at 11:34:43 and is `{}`. `nodeids` was rewritten at 11:35:48 with 684 IDs, which is 662 + 22 skipped and matches the expected full-suite count. 34 of those IDs are from this file. This supports the plan's expected 34/34 targeted run and the 662 passed / 22 skipped full run, so no spot-check was needed. The coder's own written test report was not available in the task directory. Only the cache artifacts and the diagnosis's /tmp prototype result (34 passed) were.

<details>
<summary>Issues (2)</summary>

1. **Timeout path untested** (confirmed, non-blocking): no test sets `timed_out = True` to check the "produced no further output for 75s" AuthError. Add one in a follow-up. The fake now supports it.
2. **Stale Docker-gated TTY test** (confirmed, non-blocking): `tests/test_sandbox_execution.py::test_a_tty_echoes_stdin_and_redaction_removes_it` calls `read_output(redact=...)` and expects a raw echo after `send_line`. It will fail once Docker is available. Update it to the arm-on-send contract in a follow-up.

</details>

<details>
<summary>Details</summary>

### Single-pass read and arm-on-send in `_FakeInteractiveExec`

The old fake split output across two `read_output` calls using a `_phase` counter. It scrubbed only when the caller passed `redact`, and cli.py never passes it. The new fake makes one pass:

```python
def read_output(self):
    for line in self.prompt_lines:
        yield _scrub(line, self._secret)
    if self.sent:
        for sent in self.sent:
            yield _scrub(sent, self._secret)
        ...
def send_line(self, text):
    self._secret = text
    self.sent.append(text)
```

cli.py:915 calls `send_line` while it handles the marker line. The `if self.sent` check runs only after the generator resumes, so the echo appears in the same iteration and is already scrubbed. One small fidelity difference remains. The fake emits the echo only after all `prompt_lines`, while a real TTY echoes as soon as it is written. All current fixtures put the marker last, so no test is affected. The test-local `_scrub` uses `"***"`, the same as `sandbox._REDACTED`.

### `resolve_diff_base` monkeypatch

`lambda sandbox, branch: f"{branch}~1"` matches `git_capture.resolve_diff_base(sandbox, branch) -> str` and the call site `base_ref = resolve_diff_base(sandbox, branch)` in cli.py. It is patched on `cli_module`, which is where cli.py imports the name. Without it, the real function would call `sandbox.exec` on a fake that has no `exec`. These tests now run past step 8, so the patch is exercised for the first time.

</details>

<details>
<summary>File map</summary>

- `cli/tests/test_interactive_auth.py`: `_FakeInteractiveExec` changed to single-pass read, arm-on-send redaction, a `timed_out` field and no-arg `flush_remaining`. `_Sandbox.exec_interactive` accepts `timeout_s`. Adds the `resolve_diff_base` monkeypatch.

Full diff: `git diff -- cli/tests/test_interactive_auth.py`.

</details>
