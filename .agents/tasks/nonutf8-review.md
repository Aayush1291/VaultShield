# Bytes-level non-UTF-8 path flagging and loud metadata-overflow failure in diff capture

The change closes the one silent blind spot left in patch capture. A changed file whose name is not valid UTF-8 used to round-trip through `Sandbox.exec`'s lossy decode, get handed back to git as a U+FFFD pathspec, match nothing, and land in `diff.json` as `"patch": ""` with no flag. Now `ExecResult` carries the raw stdout bytes, the numstat parser strict-decodes each path, and a lossy path gets `{path, additions, deletions, "patch_truncated": true}` with no `patch` and no `git diff` call. Separately, a diff whose per-file metadata alone exceeds `MAX_DIFF_REQUEST_BYTES` now raises `GitCaptureError` (surfaced as a `click.ClickException` by `cli.py`) before any per-file git call and before `diff.json` is written, instead of writing an oversized file that would fail later at upload.

Watch for: two different non-UTF-8 names collapse to the same U+FFFD `path`, producing duplicate entries (likely, non-blocking); the backend serializer docstring still describes `patch_truncated` with the old, narrower meaning (confirmed, non-blocking).

**Verdict**: APPROVED

## High-level view

Detection is at the bytes level. `ExecResult` gains one defaulted, `repr=False` field, `stdout_bytes`, filled by `Sandbox.exec` from the same chunk it decodes. `_parse_numstat` now splits raw bytes and strict-decodes each path, so a legitimate name containing U+FFFD (bytes `EF BF BD`) is still patched while `\xff.txt` is flagged. That was the only Sandbox change.

The lossy-path branch runs before the binary branch, so a non-UTF-8 binary file also gets the flag. `patch_truncated` now means "the scan did not see this file's whole patch", and a patch-less entry without the flag still means binary. The module docstring, `FileDiffStat`, `GitCaptureError` and `cli/README.md` all carry the new meaning. The backend serializer docstring was not updated.

The total-size behavior before and after is stated in the plan's "Total-size behaviour: today vs required" section. Before: no entry was ever dropped, budget-shortened patches were already flagged, the non-UTF-8 case was the only silent loss, and metadata-only overflow wrote an oversized `diff.json` that failed late at upload. After: the first two are unchanged, the non-UTF-8 case is flagged, and metadata overflow raises at capture time with a message giving the file count, byte size, cap, and constant name. The check reuses `_metadata_cost`, which costs each entry the way it would be written (lossy entries as patch-less plus the flag), so the check and the output cannot drift apart.

`FileDiffStatSerializer` accepts the new entry shape as-is. `patch` is `required=False`, `patch_truncated` is an optional `BooleanField`, and a new backend test checks this exact entry. `backend/security_scan/rules.py` is untouched.

The verification evidence is recorded in `nonutf8-plan.md` and is enough on its own: 53 passed in `test_git_capture.py`, the full CLI suite at 645 passed / 17 failed / 22 skipped (all 17 failures are the known `test_interactive_auth.py` `timeout_s` failures), backend `runs/tests.py` at 64 passed, and a mutation check where disabling the lossy branch fails 3 tests. No spot-check was needed. Not run: the MongoDB-backed backend suites and the Docker-dependent CLI tests. The Linux on-disk branch of the non-UTF-8 fixture was also not exercised, because macOS took the `update-index --cacheinfo` fallback.

<details>
<summary>Issues (3)</summary>

1. **Lossy-path collisions** — `b"\xff.txt"` and `b"\xfe.txt"` both store `path: "\ufffd.txt"`, so `diff.json` can hold two entries with the same path. They can also collide with a real file named `\ufffd.txt`. Non-blocking. Consider a follow-up to keep the paths distinct, for example with a backslash-escaped spelling.
2. **Stale serializer docstring** — `FileDiffStatSerializer`'s docstring still says `patch_truncated` means "`patch` is only a prefix". Update it to the wider meaning so backend readers don't take a patch-less flagged entry for a malformed one.
3. **Overflow → ClickException not tested end to end** — the overflow test asserts `GitCaptureError`. The mapping to `ClickException` is covered only by the generic `test_run_reports_a_git_capture_failure_and_still_tears_the_sandbox_down`. That is enough for now, and an optional CLI-level test would lock in the user-facing message.

</details>

<details>
<summary>Details</summary>

### U+FFFD spelling as the stored path

`_decode_path` returns `raw.decode("utf-8", errors="replace")` for a lossy name (confirmed, `git_capture.py` `_decode_path`). Each invalid byte becomes one U+FFFD, so different invalid byte sequences of the same shape map to the same string. Two such files in one Run produce two `diff.json` entries with identical `path` and both flagged. The backend stores `files` as a JSON list, so nothing rejects the duplicates. Any consumer that keys by path (dashboard Diff tab, a future per-path finding join) would merge or shadow one of them (likely; the dashboard was not read). A valid file really named `\ufffd.txt` can collide the same way, but its entry has a `patch` and no flag, so the two can still be told apart. This is rare in practice and the blind spot stays visible through the flag, so it does not block. A distinguishable spelling (for example `backslashreplace`) would remove the ambiguity.

### Overflow check placement

The raise comes right after `_parse_numstat` and before the loop. So no per-file `docker exec` runs and `diff.json` is not written, which matches how the existing parse-failure path behaves (confirmed, test `..._metadata_alone_is_over_the_budget` asserts both). Strictly-greater-than is used, and `_metadata_cost` over-counts by at most 2 bytes of separator. That makes the check conservative and never lets an oversized body through.

### Test fixture portability

`_commit_non_utf8_file` first tries a real on-disk file. It re-raises any errno except `EILSEQ`, and on `EILSEQ` falls back to `hash-object -w` + `update-index --cacheinfo`, with no `skipif`. It commits with `commit_as_developer` so that `git add --all` doesn't stage the deletion of the index-only entry. The parametrized case asserts the exact entry dict (so no `patch` key) and that only one `-U0` call ran, for `ok.txt`. The `_RawContainer` test drives the real `Sandbox.exec`, so the `stdout_bytes` plumbing is covered on its own and not only through the subprocess stand-in. Not exercised on this machine: the Linux on-disk + `git add` branch.

</details>

<details>
<summary>File map</summary>

- `cli/vaultshield/sandbox.py`: `ExecResult.stdout_bytes` field, filled in `Sandbox.exec`.
- `cli/vaultshield/git_capture.py`: bytes-level numstat parsing, `_decode_path`, `_raw_stdout`, `lossy_path` record flag and branch, metadata-overflow `GitCaptureError`, docstrings.
- `cli/vaultshield/cli.py`: comment on the `GitCaptureError` handler lists the new cause.
- `cli/tests/test_git_capture.py`: bytes-mode `LocalGitSandbox`, non-UTF-8 fixture and tests, U+FFFD-name test, raw `Sandbox.exec` test, overflow test, stronger budget test.
- `backend/runs/tests.py`: flagged patch-less entry validates unchanged.
- `cli/README.md`: "Very many files" and non-UTF-8 known-gap bullets.

Full diff: `git diff` in the repository root. Other files in the working tree belong to the earlier patch-passthrough task, plus the unrelated `.DS_Store`.

</details>
