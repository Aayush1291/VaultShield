# Per-file `patch` passthrough from CLI to security scan, plus root-commit diff base (pass 3)

The CLI's `build_structured_diff` runs one hardened `git diff -U0` per text file and attaches the output to that file's `diff.json` entry as `patch`. Each patch is capped at 200 KiB on line boundaries, and the whole body is held under a 2 MiB budget so it stays below Django's unchanged 2.5 MiB `DATA_UPLOAD_MAX_MEMORY_SIZE`. `resolve_diff_base` returns `<branch>~1`, or the empty-tree SHA from `git hash-object` (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`, not the truncated hash in the request) when the capture commit is a root. The backend accepts and stores optional `patch`/`patch_truncated`, and `rules.py` is unchanged. The blocking finding from pass 2, where a NUL past byte 8000 made the whole diff POST fail with a 400, is fixed: NUL is now replaced with U+FFFD before truncation and costing, and a test covers it.

Watch for: a file whose name is not valid UTF-8 reaches the per-file `git diff` as a lossy U+FFFD path. That pathspec matches nothing, so the entry gets `patch: ""` with real `additions` and no `patch_truncated` flag, and the scanner sees nothing (confirmed, non-blocking).

**Verdict**: APPROVED

## High-level view

The NUL replacement runs before `_truncate_patch`, so the budget is spent on the text that is actually sent. The pass-2 non-blocking items (`.gitattributes -diff` bypass, full-stdout buffering, one exec per file) are now documented as known gaps in the module docstring and `cli/README.md` instead of being changed in code. That respects the task's definition of binary as "numstat shows `-`".

One more scanner-coverage gap of the same kind remains. The task's "raw `-z` paths" holds only for UTF-8 names. `Sandbox._decode` decodes with `errors="replace"`, so the path passed back to git is not the path git printed. Unlike the attributes case, the entry gives no sign of the problem: it looks like a complete, empty patch.

The two scanner follow-ups outside this diff (`splitlines()` line splitting and the stale "gap" docstring in `rules.py`) still stand, because the task froze `rules.py`.

<details>
<summary>Issues (3)</summary>

1. **Non-UTF-8 filename silently gets an empty patch** (confirmed, non-blocking): the U+FFFD-substituted path matches no file, so `git diff` exits 0 with no output and the entry is sent as a complete `patch: ""`. When `stat.path` contains U+FFFD, emit `patch: ""` with `patch_truncated: true` and skip the exec, so the entry admits the scanner did not see it. Document it next to the `.gitattributes` gap and add a test that builds the path with `update-index --cacheinfo`.
2. **Scanner `splitlines` vs git's `\n` lines** (confirmed, follow-up outside this diff): `_iter_added_lines` also splits on `\x0b`, `\x0c`, `\x1c`–`\x1e`, `\x85`, `\u2028` and `\u2029`, so parts of such lines go unscanned and later line numbers shift. Track it as a scanner follow-up.
3. **Stale "gap" docstring in `rules.py`** (confirmed, follow-up): it still says the CLI sends no `patch`. Update it once the scanner is unfrozen.

</details>

<details>
<summary>Details</summary>

### Lossy paths and the per-file pathspec

Numstat paths come back through `Sandbox._decode`, which uses `chunk.decode("utf-8", errors="replace")`. A Linux sandbox allows any byte except `/` and NUL in a filename, so an agent can write `sec\xff.py`. Numstat then reports `1	0	sec�.py`. The per-file command passes `sec\ufffd.py` back as a UTF-8 argv element under `--literal-pathspecs`, and that path does not exist in either tree. A local repro on git 2.x (the path committed through `update-index --cacheinfo`) gives 0 bytes of patch output and exit 0, so `_exec_or_raise` does not raise either.

`build_structured_diff` then records `{"path": "sec�.py", "additions": 1, "deletions": 0, "patch": ""}`. The entry carries no `patch_truncated`, so a reader of the stored diff cannot tell it apart from a file whose patch really is empty. `scan_diff()` finds no added lines and reports nothing. This is the same class of gap as the `.gitattributes -diff` bypass, which is a filename trick that hides content from the automated rule. It is less visible, though: the dashboard shows a normal entry with a non-zero count instead of "binary".

Lossy path decoding already existed before this diff, so stored paths were never exact. What this diff adds is the reuse of those paths as git input. The cheapest fix that keeps the prescribed per-path command is to check for U+FFFD in `stat.path` and mark the entry truncated without running git. That keeps `patch_truncated`'s documented meaning ("`patch` is a strict prefix of the real per-file patch"). A broader guard would flag any text entry whose patch is empty while `additions + deletions > 0`. A non-UTF-8 path is not a binary file, so this gap is separate from the binary rule.

### Test coverage

Iteration 2 evidence in the plan's VERIFICATION section:

- `test_git_capture.py`: 48 passed, including the new late-NUL test.
- Full CLI suite: 17 failed, 640 passed, 22 skipped. All 17 failures are the pre-existing `test_interactive_auth.py` failures (`exec_interactive() got an unexpected keyword argument 'timeout_s'`), the same as with the changes stashed. As a result, no passing test exercises the one-line `resolve_diff_base` monkeypatch in that file.
- `runs/tests.py`: 63 passed.
- MongoDB-backed suites: not re-run in iteration 2, which is justified because no backend file changed. Iteration 1 recorded 285 passed, including the `secret-literal` integration test at line 11.

Not tested:

- a non-UTF-8 filename;
- a file classified as binary because of `.gitattributes` (documented, not tested);
- CRLF or other `Cc` characters in the Hypothesis alphabet;
- the root-commit path under the Hypothesis property, which always starts from a base commit.

</details>

<details>
<summary>File map</summary>

- `cli/vaultshield/git_capture.py`: per-file patch capture, NUL replacement, `MAX_PATCH_BYTES`/`MAX_DIFF_REQUEST_BYTES` budget, `_NumstatRecord`, `_truncate_patch`, `_entry_json`, `resolve_diff_base`, docstrings including the known-gaps notes.
- `cli/vaultshield/cli.py`: step 8 calls `resolve_diff_base`; corrected base-ref comment.
- `cli/tests/test_git_capture.py`: `_init_repo` extraction, numstat-only `CannedSandbox`, and tests for patches, binary files, the budget, root commits, late NULs, and the Hypothesis property.
- `cli/tests/test_cli.py`, `cli/tests/test_interactive_auth.py`: monkeypatch `resolve_diff_base`.
- `cli/README.md`: patches, size limits, NUL replacement, known gaps.
- `backend/runs/serializers.py`: optional `patch`/`patch_truncated` fields and docstrings.
- `backend/runs/models.py`: comment on the `RunDiff.files` shape.
- `backend/runs/tests.py`: database-free serializer cases.
- `backend/vaultshield_backend/test_mongodb.py`: endpoint round-trips, malformed-diff 400, `secret-literal` integration test.
- `backend/README.md`: request-size note.
- `.DS_Store`: unrelated worktree change; do not stage.

Full diff: `git diff` in `/Users/aayush/Codes/VaultShield`.

</details>
