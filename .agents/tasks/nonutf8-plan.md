# Implementation Plan: non-UTF-8 paths in patch capture + loud total-size failure

Work in place in `/Users/aayush/Codes/VaultShield` (no worktree). Leave
everything uncommitted. Never touch or stage `.DS_Store`. Do not edit
`backend/security_scan/rules.py`.

## Findings from exploration

- `cli/vaultshield/sandbox.py`: `Sandbox.exec` calls
  `container.exec_run(cmd, workdir=_REPO_DIR, demux=True)` and returns
  `ExecResult(exit_code, stdout=_decode(stdout), stderr=_decode(stderr))`.
  `_decode` is `chunk.decode("utf-8", errors="replace")`. `ExecResult` is a
  frozen dataclass with exactly `exit_code`, `stdout`, `stderr`. The raw bytes
  are thrown away, so `git_capture` currently has no way to see them.
- `cli/vaultshield/git_capture.py`: `build_structured_diff` parses
  `result.stdout` (already lossy) with `_parse_numstat(output: str)`. A path
  like `b"\xff.txt"` becomes `"\ufffd.txt"` and is passed back as the
  pathspec of the per-file `git --literal-pathspecs diff ... -U0 -- <path>`.
  Reproduced locally (git 2.52, macOS): that command exits 0 with empty
  stdout, so the entry is written as `{"path": "\ufffd.txt", "additions": 1,
  "deletions": 0, "patch": ""}` with no flag. That is the silent gap.
- Real-git check on this machine: `open(b"<repo>/\xff.txt", "wb")` fails with
  `OSError` errno 92 (`EILSEQ`) on APFS. `git hash-object -w --stdin` +
  `git update-index --add --cacheinfo 100644,<blob>,\xff.txt` (bytes argv)
  works, and `git diff --numstat -z` then emits `b"1\t0\t\xff.txt\x00"`.
- Only `cli/tests/test_git_capture.py` builds `ExecResult` objects that reach
  `build_structured_diff`: `GitRepo.git`, `LocalGitSandbox.exec` (both use
  `subprocess.run(..., text=True)`, which decodes strictly and would raise
  `UnicodeDecodeError` on `\xff`), and `CannedSandbox.exec` (hand-built str
  stdout). `test_cli.py`/`test_interactive_auth.py` monkeypatch
  `build_structured_diff` away entirely.
- Backend (read, no change needed): `backend/runs/serializers.py`
  `FileDiffStatSerializer` has `patch = CharField(required=False,
  allow_blank=True, trim_whitespace=False)` and `patch_truncated =
  BooleanField(required=False)`. Absent optional keys stay absent in
  `validated_data`. So `{path, additions, deletions, "patch_truncated": true}`
  with no `patch` validates and is stored as-is. `rules.py`
  `_iter_file_entries` skips an entry whose `patch` is missing, so such an
  entry yields no findings and no error. The serializer docstring still says
  `patch_truncated` means "patch is only a prefix", which is narrower than the
  new meaning. It is a docstring only and is out of scope here; report it as a
  follow-up in the final summary rather than editing it.
- Error convention: modules raise their own exception type and `cli.py` maps
  it to `click.ClickException` at the command boundary (`GitCaptureError`
  docstring says so). `cli.py` lines ~797-806: `except GitCaptureError as exc:
  raise click.ClickException(f"Run {run_id} failed: {exc} (Event_Log
  preserved at ...)")`. `test_cli.py::test_run_reports_a_git_capture_failure_and_still_tears_the_sandbox_down`
  already proves that mapping (non-zero exit, message shown, teardown runs).
- Baseline: `cd cli && .venv/bin/python -m pytest tests/test_git_capture.py -q`
  gives 48 passed. The full CLI suite has 17 pre-existing failures in
  `tests/test_interactive_auth.py` (`exec_interactive() got an unexpected
  keyword argument 'timeout_s'`), unrelated.

## Total-size behaviour: today vs required (item 2)

Today (`build_structured_diff`, read and traced):

1. No file entry is ever dropped. Every numstat record is written.
2. Patches shortened by the per-file cap or by the 2 MiB budget always carry
   `"patch_truncated": true`. Once the budget is spent, later text files get
   `"patch": ""` plus the flag, and their `git diff` is not run. Flagged, not
   silent.
3. The one silent loss is the non-UTF-8 path above (`patch: ""`, no flag).
   Item 1 fixes it.
4. If the metadata alone (pass 1, `_metadata_cost`) exceeds
   `MAX_DIFF_REQUEST_BYTES`, `remaining <= 0`. Every text entry gets
   `patch: ""` + flag, and `diff.json` is still written although it is over
   the cap. Nothing fails at capture time. The upload then either goes
   through (2 to 2.5 MiB) or Django rejects it with a 400, which the Uploader
   reports later. So it neither raises a ClickException nor drops anything
   silently. It fails late, at upload, not loudly at capture. The module
   docstring and README call this "documented rather than worked around".

Required (decided):

- Keep 1 and 2 unchanged. Every entry whose patch is shortened or left out
  because of the budget keeps `patch_truncated: true`.
- Replace 4: when `_metadata_cost(records) > MAX_DIFF_REQUEST_BYTES`, raise
  `GitCaptureError` right after parsing numstat, before any per-file
  `git diff` runs and before `diff.json` is written. `cli.py`'s existing
  `except GitCaptureError` turns it into a `click.ClickException`. This is
  the project's pattern: `git_capture` never imports click. The check uses
  `_metadata_cost` because that is the smallest body this module can write
  (each text entry as `"patch": ""` + flag, the shape it already emits once
  the budget is spent). It is conservative by at most 2 bytes (separator
  over-count). Strictly greater raises; equal still fits.
- Not writing `diff.json` on overflow matches the existing parse-failure
  behaviour ("a diff we could not produce is not a diff",
  `test_build_structured_diff_rejects_unparseable_output`).

## Decisions

1. **Detect invalid UTF-8 at the bytes level, through one additive field on
   `ExecResult`.** Add `stdout_bytes: bytes = field(default=b"", repr=False)`
   and have `Sandbox.exec` fill it with the raw stdout chunk. `_parse_numstat`
   then works on bytes and strict-decodes each path. Reason: the U+FFFD check
   cannot tell a non-UTF-8 name from a valid name that really contains U+FFFD
   (bytes `EF BF BD`). Such a file would lose its scan for no reason. The
   field is additive: one field, one keyword in `exec`. It is defaulted, so
   every existing `ExecResult(...)` construction still works, and `repr=False`
   keeps large outputs out of reprs. Rejected: a new `Sandbox.exec_bytes`
   method (duplicates `exec`'s error wrapping), reaching into
   `sandbox.container` from `git_capture` (bypasses `SandboxError` wrapping
   and the test stand-ins), and re-running numstat with `core.quotePath` and
   C-unquoting (the module docstring already rejects that parsing). No other
   Sandbox changes.
2. **Fallback for hand-built results.** `git_capture._raw_stdout(result)`
   returns `result.stdout_bytes` when it is non-empty or when `result.stdout`
   is empty. Otherwise it returns `result.stdout.encode("utf-8")`.
   `Sandbox.exec` always fills both from the same chunk (non-empty bytes
   always decode to a non-empty str), so the fallback only applies to a
   test stand-in that builds `ExecResult` by hand (`CannedSandbox`). Without
   it, such a stand-in would silently parse as "no changes", which is exactly
   the kind of silent drop this task forbids. `CannedSandbox` stays unchanged
   and keeps exercising this path.
3. **Lossy path takes precedence over binary.** The user's wording covers
   every changed file whose path is not valid UTF-8. So such an entry is
   always `{path, additions, deletions, "patch_truncated": true}` with no
   `patch` key, whether numstat said text or `-`. For a binary one,
   additions/deletions stay 0/0. The stored `path` stays the
   `errors="replace"` decoding (what is stored today, and valid for the
   backend `CharField`).
4. **`patch_truncated` gets one widened meaning:** "the security scan did not
   see this file's whole patch". Either `patch` is a strict prefix of it, or
   `patch` is absent because the path is not valid UTF-8 and could not be
   handed back to git. A patch-less entry *without* the flag still means
   "binary".
5. **The overflow error stays a `GitCaptureError`.** It is not a new subclass
   or a direct `click.ClickException`; see the total-size section.
   Message (plain integers so tests can match them):
   `f"Git capture failed while sizing diff.json: the entries for
   {len(records)} changed files take {metadata_cost} bytes even with every
   patch left out, over the {MAX_DIFF_REQUEST_BYTES}-byte upload cap
   (MAX_DIFF_REQUEST_BYTES), so the backend would reject the diff"`.
   Read `MAX_DIFF_REQUEST_BYTES` at call time (module global, as today) so
   tests can monkeypatch it.
6. **Test stand-in mirrors the real Sandbox.** `LocalGitSandbox.exec` switches
   from `text=True` to bytes and builds `ExecResult(stdout=out.decode("utf-8",
   errors="replace"), stderr=err.decode("utf-8", errors="replace"),
   stdout_bytes=out)`, the same decoding as `sandbox._decode`. This also stops
   universal-newline translation, which the real Sandbox never did. No
   existing test depends on it: the Hypothesis alphabet excludes `Cc`, and no
   fixture writes `\r`.
7. **Non-UTF-8 test fixture works on every OS.** Try writing the file on disk
   via `os.fsencode` (works on Linux ext4/overlayfs). On `EILSEQ` (macOS
   APFS), fall back to `git hash-object -w --stdin` +
   `git update-index --add --cacheinfo 100644,<blob>,<bytes path>`. The test
   commits with `repo.commit_as_developer`, not `commit_run_branch`, because
   `git add --all` would stage the deletion of an index-only entry that has
   no worktree file. `build_structured_diff` only reads the two commits, so
   this does not weaken the test. No `skipif`.

## Environment / commands

- CLI venv exists: `/Users/aayush/Codes/VaultShield/cli/.venv`.
  - Targeted: `cd /Users/aayush/Codes/VaultShield/cli && .venv/bin/python -m pytest tests/test_git_capture.py -q`
  - Full: `cd /Users/aayush/Codes/VaultShield/cli && .venv/bin/python -m pytest -q`
    (expect only the 17 pre-existing `test_interactive_auth.py` failures;
    Docker-dependent tests skip).
- Backend venv exists: `/Users/aayush/Codes/VaultShield/backend/.venv`.
  DB-free: `cd /Users/aayush/Codes/VaultShield/backend && VAULTSHIELD_MONGODB_URL=mongodb://localhost:27017/vaultshield .venv/bin/pytest -q runs/tests.py`.
  The MongoDB-backed suites need a replica set (see
  `patch-passthrough-plan.md` "Environment / commands"). No backend source
  changes here, so they are optional. If not run, say so.
- `.agents/tasks/review.json` is a stale artifact from the previous task with
  `"verdict": "APPROVED"`. Do not treat it as a review of this task. If this
  loop's reviewer writes to the same path, it overwrites it.

## Plan

- [x] 1. Add the raw-stdout field to `ExecResult` and fill it in `Sandbox.exec`.
      In `cli/vaultshield/sandbox.py`: change `from dataclasses import
      dataclass` to `from dataclasses import dataclass, field`; add
      `stdout_bytes: bytes = field(default=b"", repr=False)` after `stderr`;
      in `Sandbox.exec` pass `stdout_bytes=stdout or b""` (docker's `demux`
      gives `None` for an empty stream). Extend the `ExecResult` docstring
      with one paragraph: `stdout_bytes` is stdout exactly as the process
      wrote it, before `_decode`'s lossy `errors="replace"`. `git_capture`
      needs it to tell a file name that is not valid UTF-8 from one that
      really contains U+FFFD (Requirement 17.1: the scan must not be blinded
      silently). It defaults to `b""` so hand-built results still construct,
      and `repr=False` keeps big outputs out of error reprs. No other Sandbox
      edits.
      Files: `cli/vaultshield/sandbox.py`
      Verify: `cd cli && .venv/bin/python -m pytest tests/test_git_capture.py -q`
      still gives 48 passed (pure addition).

- [x] 2. Parse numstat as bytes, flag non-UTF-8 paths, and fail loudly on
      metadata overflow, in `cli/vaultshield/git_capture.py`. Depends on 1.
      a. Constants: `_FIELD_SEPARATOR = b"\t"`, `_RECORD_TERMINATOR = b"\0"`
         (only `_parse_numstat` uses them). Update their comment to say the
         parser works on raw bytes.
      b. `_NumstatRecord`: add `lossy_path: bool`. Docstring: True when git's
         path bytes are not valid UTF-8, so `stat.path` is a lossy
         (U+FFFD) spelling that cannot be passed back to git as a pathspec.
      c. New `_raw_stdout(result: ExecResult) -> bytes` per Decision 2, with
         a docstring explaining the hand-built fallback.
      d. `_parse_numstat(output: bytes)`: `fields = output.split(b"\0")`, pop
         the trailing `b""`. Per record, `parts = record.split(b"\t", 2)`.
         Decode the count fields with `.decode("ascii", errors="replace")`,
         so a non-ASCII byte becomes U+FFFD and fails `_COUNT_PATTERN`, then
         pass them to the unchanged `_parse_count`. Check the empty path with
         `path == b""` (rename shape; take `fields[index + 1]` as now).
         Decode the final path with a new `_decode_path(raw: bytes) ->
         tuple[str, bool]`: strict `raw.decode("utf-8")` returns `(text,
         False)`; on `UnicodeDecodeError` return `(raw.decode("utf-8",
         errors="replace"), True)`. For error messages, decode the record
         with `record.decode("utf-8", errors="replace")` before calling the
         unchanged `_unparseable_message(str)`. Binary detection is
         unchanged (`"-"` in the decoded counts). Update the docstring
         (input is raw bytes, which paths count as lossy).
      e. `_metadata_cost`: a `lossy_path` record is costed as written,
         `replace(stat, patch_truncated=True)` with no patch, before the
         binary/text branches. Update the docstring.
      f. `build_structured_diff`:
         `records = _parse_numstat(_raw_stdout(result))`;
         `metadata_cost = _metadata_cost(records)`;
         `if metadata_cost > MAX_DIFF_REQUEST_BYTES: raise GitCaptureError(<Decision 5 message>)`;
         `remaining = MAX_DIFF_REQUEST_BYTES - metadata_cost`. In the loop,
         first `if record.lossy_path: stats.append(replace(stat,
         patch_truncated=True)); continue` (no exec), then the existing
         binary branch and the rest unchanged. `_entry_json` already writes
         the flag without a patch, so no change there.
      g. Docstrings, in the existing Requirement-tagged style:
         - `build_structured_diff`: describe the lossy-path entry
           (Requirement 17.1). Add the over-budget case under `Raises:
           GitCaptureError` (Requirement 10.2: never silently understate the
           change set; Requirement 14.4: fail while the artifacts are still
           local).
         - `GitCaptureError`: "Three causes" becomes four. Add "the diff's
           per-file metadata alone is over `MAX_DIFF_REQUEST_BYTES`, so no
           uploadable `diff.json` exists".
         - `FileDiffStat`: `patch` is `None` for a binary file *or* a
           non-UTF-8 path. `patch_truncated` has the widened meaning
           (Decision 4).
         - Module docstring, "Reading `--numstat -z` output": numstat stdout
           is read from `ExecResult.stdout_bytes`, and each path is decoded
           strictly.
         - Module docstring, "Per-file patches": change the `<path>` bullet
           ("the raw path ... intact" holds only for valid UTF-8). Add a
           paragraph **Paths that are not valid UTF-8.**, placed after the
           `.gitattributes` known gap: a Linux sandbox allows any byte but
           `/` and NUL in a name; `Sandbox.exec`'s text is decoded with
           `errors="replace"`; a lossy name cannot be handed back to git
           (the pathspec matches nothing and git exits 0 with an empty
           patch, which would look like a complete empty patch); so no
           `git diff` runs and the entry carries `"patch_truncated": true`
           with no `patch`, so the scan's blind spot is visible
           (Requirement 17.1). Detection is on bytes, so a valid name that
           really contains U+FFFD is still scanned. The stored path is the
           U+FFFD spelling.
         - Module docstring, "Total budget": replace "If metadata alone is
           over the budget ... documented rather than worked around." with:
           metadata is checked first, and if it alone exceeds the budget,
           capture raises `GitCaptureError` (shown by `cli.py` as a
           `click.ClickException`) before any per-file `git diff` runs and
           before `diff.json` is written, because no uploadable diff exists
           and sending one anyway would fail late at upload. Change "has one
           meaning throughout: `patch` is a strict prefix" to the widened
           meaning.
         - Module docstring, `diff.json`'s shape: add an example entry
           `{"path": "\ufffd.txt", "additions": 1, "deletions": 0,
           "patch_truncated": true}` and note it.
      Files: `cli/vaultshield/git_capture.py`
      Verify: `cd cli && .venv/bin/python -m pytest tests/test_git_capture.py -q`.
      All 48 pass. The not-yet-updated `LocalGitSandbox` leaves
      `stdout_bytes` empty, so these tests go through the `_raw_stdout`
      fallback. If any fail, fix them before step 3.

- [x] 3. Update the `cli.py` comment on the `GitCaptureError` handler. Add
      "or the diff's metadata alone is over the upload cap" to the list of
      causes. Comment only, no behaviour change.
      Files: `cli/vaultshield/cli.py`
      Verify: `cd cli && .venv/bin/python -m pytest tests/test_cli.py -q`
      shows no new failures.

- [x] 4. Tests in `cli/tests/test_git_capture.py`. Depends on 1 and 2.
      a. `import errno`. Switch `LocalGitSandbox.exec` to bytes per
         Decision 6 (`subprocess.run(cmd, cwd=..., env=...,
         capture_output=True)`, then decode with `errors="replace"`, and set
         `stdout_bytes=completed.stdout`). Note in its docstring that it
         decodes like `Sandbox.exec`. Leave `GitRepo.git` and
         `CannedSandbox` unchanged. `CannedSandbox` covers the
         `_raw_stdout` fallback; say so in its docstring.
      b. Helper `_commit_non_utf8_file(repo, name: bytes, content: bytes)`
         per Decision 7. `target = os.path.join(os.fsencode(repo.path),
         name)`. Try `open(target, "wb")`, then `subprocess.run([b"git",
         b"add", b"--", name], cwd=repo.path, env=repo.env,
         capture_output=True, check=True)`. On `OSError` with `errno ==
         errno.EILSEQ`, run `git hash-object -w --stdin` (input=content,
         check=True), then `[b"git", b"update-index", b"--add",
         b"--cacheinfo", b"100644," + blob + b"," + name]` (check=True). Any
         other errno is re-raised. Finish with
         `repo.commit_as_developer(...)`. The docstring explains the APFS
         `EILSEQ` fallback and why `commit_run_branch` is not used.
      c. `@pytest.mark.parametrize("content, additions", [(b'API_KEY =
         "sk-abcdefghij123456"\n', 1), (b"\x00binary\x00", 0)])`
         `test_build_structured_diff_flags_a_non_utf8_path_without_running_git_for_it`:
         `base = repo.git_ok("rev-parse", "HEAD")`; `repo.write("ok.txt",
         "fine\n")`; `repo.git_ok("add", "ok.txt")`;
         `_commit_non_utf8_file(repo, b"\xff.txt", content)`;
         `sandbox = LocalGitSandbox(repo)`; `build_structured_diff(sandbox,
         base, "HEAD", run_dir)`. Assert
         `_entry(run_dir, "\ufffd.txt") == {"path": "\ufffd.txt",
         "additions": additions, "deletions": 0, PATCH_TRUNCATED_KEY: True}`
         (so no `PATCH_KEY`); `"+fine" in _entry(run_dir,
         "ok.txt")[PATCH_KEY]` and `ok.txt` has no flag; the `-U0` commands
         in `sandbox.commands` are exactly one, ending with `"ok.txt"` (no
         git run for the lossy path).
      d. `test_build_structured_diff_still_patches_a_valid_name_containing_u_fffd`:
         `repo.write("\ufffd-real.txt", "x\n")`, then `_capture`. The entry
         has `"+x"` in its patch and no `PATCH_TRUNCATED_KEY`. This proves
         detection is bytes-level, not a U+FFFD heuristic.
      e. `test_sandbox_exec_keeps_stdout_bytes_and_flags_a_non_utf8_numstat_path`.
         It runs through the real `Sandbox.exec`: a tiny `_RawContainer`
         with `exec_run(cmd, **kwargs)` that records `cmd` and returns
         `(0, (b"1\t0\t\xff.txt\x00", None))` for a `--numstat` command,
         else `(0, (None, None))`. `sandbox = Sandbox(RUN_ID, client=None);
         sandbox.container = _RawContainer()`. Assert that
         `sandbox.exec(["git", "diff", "--numstat"])` has `stdout_bytes ==
         b"1\t0\t\xff.txt\x00"`, `stdout == "1\t0\t\ufffd.txt\x00"` and
         `stderr == ""`. Then `build_structured_diff(sandbox, "main",
         "branch", tmp_path)` writes exactly `[{"path": "\ufffd.txt",
         "additions": 1, "deletions": 0, PATCH_TRUNCATED_KEY: True}]`, and
         no recorded command contains `"-U0"`.
      f. `test_build_structured_diff_refuses_a_diff_whose_metadata_alone_is_over_the_budget`:
         `monkeypatch.setattr(git_capture, "MAX_DIFF_REQUEST_BYTES", 256)`.
         Write 10 files `f"file-{n:02d}.txt"` with `"x\n"` (about 95 bytes
         of metadata each, so about 950 > 256). `commit_run_branch`, then
         `sandbox.commands.clear()`. `pytest.raises(GitCaptureError)` around
         `build_structured_diff`. The message contains `"256"`, `"10 changed
         files"` and `"MAX_DIFF_REQUEST_BYTES"`. `diff_path(run_dir)` does
         not exist. No command in `sandbox.commands` contains `"-U0"`.
      g. Strengthen the existing
         `test_build_structured_diff_keeps_the_whole_diff_within_the_request_budget`
         to prove nothing is dropped silently. Assert that the written
         paths equal `{f"f{n}.txt" for n in range(10)}`. For every unflagged
         entry, `entry[PATCH_KEY] == repo.git(*_patch_argv(base_branch,
         branch, entry["path"])).stdout`. Every flagged entry's patch is a
         prefix of that full patch.
      h. Update the test module docstring's coverage paragraph: non-UTF-8
         paths, the bytes-level detection, metadata overflow raising.
      Files: `cli/tests/test_git_capture.py`
      Verify: `cd cli && .venv/bin/python -m pytest tests/test_git_capture.py -q`.
      All pass (48 existing + 2 parametrized + 3 new = 53). Then
      `cd cli && .venv/bin/python -m pytest -q`: only the 17 pre-existing
      `test_interactive_auth.py` failures. Sanity-check the tests by
      reverting step 2f's lossy branch locally (do not leave it reverted):
      test 4c must fail.

- [x] 5. Backend acceptance test only, no source change. In
      `backend/runs/tests.py`, class `TestRunDiffIngestSerializer`, next to
      `test_an_empty_patch_is_accepted`, add
      `test_a_flagged_entry_without_a_patch_is_accepted`. The CLI's
      non-UTF-8 entry `{"path": "\ufffd.txt", "additions": 1, "deletions":
      0, "patch_truncated": True}` validates, and `validated_data["files"]`
      equals it exactly (no `patch` key added). Docstring: Requirements 13.2
      and 17.1, a patch-less flagged entry is what the CLI sends for a path
      it could not hand back to git.
      Files: `backend/runs/tests.py`
      Verify: `cd backend && VAULTSHIELD_MONGODB_URL=mongodb://localhost:27017/vaultshield .venv/bin/pytest -q runs/tests.py`.
      All pass (63 before + 1).

- [x] 6. README. In `cli/README.md`, "Diff capture: patches and size limits":
      - Replace the **Very many files** bullet: if the per-file metadata alone
        (thousands of changed files) is over `MAX_DIFF_REQUEST_BYTES`, `vaultshield
        run` fails with a clear error naming the size and the cap. It writes no
        `diff.json`. It never drops entries or uploads an oversized body.
      - Add **Non-UTF-8 file names:** git reports the raw name, but it cannot be
        passed back to git, so the entry has no `patch` and has
        `"patch_truncated": true`. The scan does not read it, and the flag shows
        that. The stored path spells the bad bytes as U+FFFD.
      - In the **Whole diff** bullet, keep "later text files get `"patch": ""`
        with `"patch_truncated": true`". It is still true.
      Files: `cli/README.md`
      Verify: re-read the section against the code. Then run
      `cd cli && .venv/bin/python -m pytest tests/test_git_capture.py -q`
      one more time: all pass.

## Verification results (iteration 1)

Run on macOS (APFS), git 2.52:

- `cd cli && .venv/bin/pytest -q tests/test_git_capture.py`: **53 passed**
  (48 existing + 2 parametrized non-UTF-8 + U+FFFD-name + Sandbox.exec bytes
  + metadata overflow).
- `cd cli && .venv/bin/pytest -q`: **17 failed, 645 passed, 22 skipped**. All
  17 failures are in `tests/test_interactive_auth.py`, each
  `TypeError: ... exec_interactive() got an unexpected keyword argument
  'timeout_s'` (checked with `-rf` + `grep | uniq -c`). Count unchanged.
- `cd backend && VAULTSHIELD_MONGODB_URL=mongodb://localhost:27017/vaultshield .venv/bin/pytest -q runs/tests.py`:
  **64 passed** (63 + 1 new).
- Mutation check: with the `lossy_path` branch in `build_structured_diff`
  disabled, 3 tests fail (both parametrized non-UTF-8 cases and the
  `Sandbox.exec` bytes test). Restored afterwards.
- On this machine `open(b"...\xff.txt", "wb")` fails with errno 92
  (`EILSEQ`), so the `git update-index --cacheinfo` fallback is what ran. The
  on-disk + `git add` branch of `_commit_non_utf8_file` (Linux) was not
  exercised here.

Not run: MongoDB-backed backend suites (need a replica set; no backend
source changed), Docker-dependent CLI tests (the 22 skips).

Deviation from plan: `_NumstatRecord.lossy_path` has a default of `False`
(plan said plain `bool`). Behaviour is identical; the parser always sets it.

## Final report requirements

State which tests ran (with counts) and which did not. The MongoDB-backed
backend suites are likely not run, since no backend source changed. Name the
follow-ups that were not done: the backend `FileDiffStatSerializer`
docstring's narrower `patch_truncated` wording, and the `rules.py` items from
`patch-passthrough-review.md` (frozen). Restate the total-size answer: today
nothing is dropped silently except the non-UTF-8 case. A metadata-only
overflow is written anyway and fails late at upload. After this change it
raises `GitCaptureError`, which the CLI shows as a `click.ClickException`.
