# Implementation Plan: per-file `patch` passthrough + root-commit diff fix

Repo root: `/Users/aayush/Codes/VaultShield` (use absolute paths). Do not touch
`backend/security_scan/rules.py` or `backend/security_scan/tasks.py`, and do not
make unrelated refactors. Every new or changed docstring follows the existing
style: prose that names the Requirement it covers (for example "Requirement
10.2", "Requirement 13.2", "Requirement 17.1") and explains *why*, plus
`Raises:` sections where the module already uses them.

## Findings from exploration (what the plan is built on)

- **How `<base>` is computed today.** `build_structured_diff(sandbox, base_ref,
  branch, run_dir)` in `cli/vaultshield/git_capture.py` takes `base_ref` as an
  argument and never computes it. The caller is `cli/vaultshield/cli.py`, step 8
  of the `run` pipeline (around line 741):
  `build_structured_diff(sandbox, f"{branch}~1", branch, run_dir)`. The comment
  block at roughly lines 398-412 of `cli.py` documents the gap and says it was
  "Left as-is".
- **Where the root-commit bug is.** `f"{branch}~1"` in `cli.py`. `commit_run_branch` always
  adds exactly one commit (`--allow-empty`), so `<branch>~1` only fails to
  resolve when that capture commit is itself the root commit. That happens
  when the Sandbox's clone has **no commits at all** (an empty repository:
  `git checkout -b` works on an unborn branch, then the capture commit becomes
  the root). The `cli.py` comment says "a repository with a single commit",
  but that is slightly wrong: a one-commit clone gets a two-commit branch, and
  `~1` resolves. In practice the failure is "the branch being diffed is a single
  root commit", which is exactly the condition `git rev-parse --verify --quiet
  <branch>~1` detects. Correct the comment as part of the fix.
- `git rev-parse --verify --quiet X~1` exits 1 with no output when X is a root
  commit, and exits 128 outside a repository (verified locally on git 2.52).
- **Empty tree hash.** `git hash-object -t tree /dev/null` prints
  `4b825dc642cb6eb9a060e54bf8d69288fbee4904` (SHA-1). Both hashes in the task
  text are wrong: `...fbd8` and `...fbd5404` are not the empty-tree SHA. Use
  `hash-object` (it also gives the right answer in SHA-256 repositories). Git
  resolves the empty tree without it being written to the object database, so
  `git diff <empty-tree> <branch>` works with no `-w` (verified).
- `Sandbox.exec` returns `stdout` already decoded with
  `utf-8, errors="replace"` (`cli/vaultshield/sandbox.py::_decode`), so a patch
  reaches `git_capture` as a valid `str`.
- `uploader._upload_diff` sends `json=diff` through `httpx`. httpx `<0.28` uses
  `json.dumps` defaults (`ensure_ascii=True`, `", "`/`": "` separators), and
  `>=0.28` uses compact `ensure_ascii=False`. `pyproject` allows both, so any
  size budget must be measured with the larger form: plain `json.dumps(...)`
  defaults.
- `DATA_UPLOAD_MAX_MEMORY_SIZE` is not set in
  `backend/vaultshield_backend/settings.py`, so Django's default of 2.5 MiB
  (2,621,440 bytes) applies. `uploader.py` already designs event batches to fit
  under this default and documents that the setting is "not raised in
  `settings.py`".
- `security_scan/rules.py::_iter_added_lines` skips everything before the
  first `@@`, uses the `+c` start of each hunk header, and walks
  `patch.splitlines()`. A `-U0` patch truncated at a `\n` boundary is
  therefore still read correctly: line numbers for the lines that are kept
  stay correct.
- `backend/runs/tests.py` is **database-free by documented design** (module
  docstring): serializer and URL-conf checks only. Endpoint and database tests
  (`APIClient` POST, `RunDetailSerializer` via GET, Celery task against
  MongoDB) live in `backend/vaultshield_backend/test_mongodb.py`, which has
  `_authenticated_client()`, `_run()`, `TestRestApiAgainstMongoDB`,
  `TestSecurityScanOrdering`. The established way to run the scan in-process
  is `patch("runs.views.run_security_scan.delay", ...)`, because the view
  calls `.delay` from `django_mongodb_backend.transaction.on_commit`. So the
  backend tests split as follows: serializer-level round-trip and malformed
  cases go in `runs/tests.py`, and the POST→GET round trip, the endpoint
  400, and the scan integration test go in `test_mongodb.py`.
- CLI pipeline tests (`cli/tests/test_cli.py::_wire_run_pipeline`,
  `cli/tests/test_interactive_auth.py` around line 213) monkeypatch
  `cli_module.commit_run_branch` and `cli_module.build_structured_diff`, and
  `test_cli.py` asserts `base_ref == f"{branch}~1"` (around line 515). Any new
  git helper called from `cli.py` has to be monkeypatched there too.
- The existing `CannedSandbox` in `test_git_capture.py` returns the same
  stdout for **every** command, so after this change it would also return
  numstat text as each file's patch. It needs to answer only the `--numstat`
  call.
- Hypothesis is a CLI dev dependency. `cli/tests/conftest.py` loads a profile
  with `max_examples=100, deadline=None`.

## Decisions

1. **Base resolution lives in `git_capture.py` as a new public
   `resolve_diff_base(sandbox, branch) -> str`, called from `cli.py`.**
   `cli.py`'s comment says `git_capture.py` "deliberately owns every" git
   command, so the `rev-parse`/`hash-object` calls belong there.
   `build_structured_diff`'s signature stays the same. The new function
   returns `f"{branch}~1"` when it resolves, which keeps error messages
   readable. Otherwise it returns the hex SHA printed by `git hash-object -t
   tree /dev/null`.
2. **Patch command:** `["git", "--literal-pathspecs", "diff", "--no-renames",
   "--no-color", "--no-ext-diff", "--no-textconv", "-U0", base_ref, branch,
   "--", path]`, run through `_exec_or_raise`. What each flag is for:
   - `--` together with `--literal-pathspecs` means a path that starts with
     `-` or `:`, or contains `*?[`, matches only itself and never acts as
     pathspec magic or a glob.
   - `--no-color`, `--no-ext-diff` and `--no-textconv` stop the agent-writable
     `.git/config` (or the image's gitconfig) from changing what the patch
     contains. This matches the module's existing reasoning for passing
     `--no-renames` explicitly.
   - `-U0` is what the task asked for: added and removed lines only, so the
     cap is spent on scannable text.
   - Paths come straight from the `-z` numstat parse (raw, unquoted) and go in
     as one argv element each, with no shell.
3. **Binary detection.** A record whose numstat count is `-` is binary. It
   keeps 0/0 counts and has **no** `patch` key. `_parse_numstat` returns that
   flag alongside each stat through a private frozen dataclass
   `_NumstatRecord(stat: FileDiffStat, binary: bool)`.
4. **`FileDiffStat` gains `patch: str | None = None` and `patch_truncated:
   bool = False`.** Defaults keep `FileDiffStat(path=..., additions=...,
   deletions=...)` and the frozen/hashable test working. `_write_diff_json`
   stops using raw `asdict`. It builds each entry as `path, additions,
   deletions`, then adds `patch` only when it is not `None`, then adds
   `"patch_truncated": true` only when it is true. Keys are omitted rather
   than written as null or false.
5. **Per-file cap and truncation strategy.** The constant goes in
   `cli/vaultshield/git_capture.py` next to `DIFF_FILES_KEY`:
   `MAX_PATCH_BYTES = 200 * 1024`, measured in UTF-8 bytes of the patch text.
   - **Strategy: truncate at a line (`\n`) boundary.** Split on `"\n"` only
     (not `splitlines`) and keep whole lines, terminator included, while the
     running total stays within the limit. The result is always a valid
     `str`, so it can never be invalid UTF-8.
   - Line-boundary cutting beats a byte cut because the scanner reads whole
     lines. A half line could create a false `secret-literal` match on a
     clipped literal, or hide a real one. Line numbers of the kept lines
     also stay correct.
   - If the very first line is already over the budget, the kept patch is
     `""`. It is still emitted, with `patch_truncated: true`.
6. **`DATA_UPLOAD_MAX_MEMORY_SIZE`: leave Django's 2.5 MiB default and add a
   total budget in the CLI.**
   - **Why not raise the setting:** it is process-global, so it would also
     raise the in-memory body limit for every endpoint, including the
     unauthenticated `POST /api/auth/login/`. And no finite value is enough
     on its own: with 200 KiB per file and no limit on the number of files, a
     diff of 13 or more large files would exceed 2.5 MiB, and a diff of 50 or
     more would exceed 10 MiB. The uploader already treats the default as
     fixed.
   - **The budget:** `MAX_DIFF_REQUEST_BYTES = 2 * 1024 * 1024` in
     `git_capture.py`, leaving about 0.5 MiB under the default for
     serializer differences.
   - **First pass, no git:** sum the metadata cost of every entry. The cost is
     `len(json.dumps(<entry with path/additions/deletions, "patch": "",
     "patch_truncated": True>)) + 2`, plus `len('{"files": []}')`. Default
     `json.dumps` gives an upper bound for both httpx encodings.
   - **Second pass, in git order:** each text file's patch gets
     `min(MAX_PATCH_BYTES raw bytes, remaining budget in JSON-encoded
     bytes)`. A kept line's JSON cost is `len(json.dumps(line)) - 2`.
   - **When the budget runs out:** later text files get `patch: ""` with
     `patch_truncated: true`, and **no `git diff` is executed for them**
     (this also saves a container exec per file).
   - **Meaning of the flag:** `patch_truncated` keeps one meaning throughout:
     "`patch` is a strict prefix of the real per-file patch".
   - **Residual limit:** if metadata alone exceeds the budget (thousands of
     files), every patch becomes `""`/truncated and the body can still go
     over 2.5 MiB. The backend then rejects it with 400. Document this; do
     not engineer around it.
   - **Docs:** document the caps in `cli/README.md` and `backend/README.md`.
     `uploader.py` code is unchanged.
7. **Backend serializer:** in `FileDiffStatSerializer`, add
   `patch = serializers.CharField(required=False, allow_blank=True,
   trim_whitespace=False)` and `patch_truncated =
   serializers.BooleanField(required=False)`.
   - DRF leaves absent optional fields without defaults out of
     `validated_data`, so a stored entry carries the keys only when the
     client sent them. That follows Decision 4's "omit" rule and needs no
     code in `RunDiffIngestSerializer.create`.
   - `allow_null` stays False, so `"patch": null` is a 400, consistent with
     the other fields.
   - Unknown keys are still dropped (the existing test stays green).
8. **No FEAT restructuring.** The work has a CLI half and a backend half, but
   they share one data contract and are small. The workflow's existing
   build/review loop (gate file `/Users/aayush/Codes/VaultShield/.agents/tasks/review.json`)
   implements this plan as-is.

## Environment / commands

- No virtualenvs exist yet. Use Python 3.11 (`/opt/homebrew/bin/python3.11`).
  The default `python3` is 3.14, which Django 5.2 and the pins may not
  support.
- **CLI setup (once):** `cd /Users/aayush/Codes/VaultShield/cli && python3.11
  -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"` (per
  `cli/README.md`).
- **CLI tests:** `cd /Users/aayush/Codes/VaultShield/cli && .venv/bin/pytest`
  for the full suite, or the targeted run `.venv/bin/pytest
  tests/test_git_capture.py tests/test_cli.py tests/test_interactive_auth.py
  tests/test_uploader.py`. They need git only; Docker-dependent tests in
  other files may skip.
- **Backend setup (once):** `cd /Users/aayush/Codes/VaultShield/backend &&
  python3.11 -m venv .venv && . .venv/bin/activate && pip install -e
  ".[dev]"` (per `backend/README.md`).
- **Backend, database-free tests:** settings import requires the URL env var
  but opens no connection:
  `cd /Users/aayush/Codes/VaultShield/backend &&
  VAULTSHIELD_MONGODB_URL=mongodb://localhost:27017/vaultshield .venv/bin/pytest
  runs/tests.py`, plus `.venv/bin/python manage.py check` with the same env.
- **Backend, full suite:** needs a MongoDB **replica set** (see
  `backend/README.md`, "The deployment must be a replica set"). The Docker
  daemon was not running during planning. If Docker can be started:
  `docker run -d --rm --name vs-test-mongo -p 27017:27017 mongo:7 --replSet
  rs0 --bind_ip_all`, then `docker exec vs-test-mongo mongosh --quiet --eval
  'rs.initiate()'`, then
  `VAULTSHIELD_MONGODB_URL='mongodb://localhost:27017/vaultshield?directConnection=true'
  VAULTSHIELD_TEST_MONGODB_URL='mongodb://localhost:27017/vaultshield?directConnection=true'
  .venv/bin/pytest`. Stop the container afterwards (`docker stop
  vs-test-mongo`). If no replica set can be reached, **say explicitly** that
  `test_mongodb.py` and the `django_db` tests in `security_scan/tests.py` were
  not run. Do not use credentials from anywhere else (no `.env` exists at the
  repo root).
- `.venv/` is git-ignored (root and `backend/.gitignore`), so the venvs can
  stay. `.DS_Store` is already modified in the worktree and is unrelated, so
  never stage it.

## Plan

- [x] 1. Add patch capture, caps, and binary handling to `build_structured_diff`.
      Files: `cli/vaultshield/git_capture.py`
      - Constants (with comments in the existing style, explaining Decisions 5/6):
        `MAX_PATCH_BYTES = 200 * 1024`, `MAX_DIFF_REQUEST_BYTES = 2 * 1024 * 1024`,
        plus private `PATCH_KEY = "patch"`/`PATCH_TRUNCATED_KEY = "patch_truncated"`
        (public names are fine too, so tests can import them; pick public, matching
        `DIFF_FILES_KEY`).
      - `FileDiffStat`: add `patch: str | None = None`, `patch_truncated: bool = False`;
        update its docstring (field order, the binary/no-patch rule, Requirement 10.2
        and 17.1 as the reason the text is carried).
      - `_NumstatRecord` frozen dataclass; `_parse_numstat` returns
        `list[_NumstatRecord]` (`binary = raw_additions == "-" or raw_deletions == "-"`);
        `_parse_count` unchanged.
      - In `build_structured_diff`, validate both refs once up front with
        `_checked_ref` (before any exec, preserving the existing "no command ran"
        test). Then run numstat as today, parse it, compute the metadata budget
        (pass 1), and walk the records (pass 2). For a binary record, keep the stat
        with no patch. For a text record whose remaining budget is `<= 0`, set
        `patch="", patch_truncated=True` without running git. Otherwise exec the
        Decision 2 argv with `step=f"diffing {path!r} between {base_ref}..{branch}"`
        and pass the stdout to `_truncate_patch(text, max_raw_bytes, max_json_bytes)
        -> tuple[str, bool, int]`, which returns the kept text, the truncated flag
        and the JSON cost spent. Use `dataclasses.replace` to attach the result.
      - `_write_diff_json`: build entries per Decision 4 (`_entry_json(stat) -> dict`,
        which pass 1 reuses to measure metadata so the two cannot drift).
      - Update the module docstring with new sections in the same voice:
        "### Per-file patches (`git diff -U0`)", the binary rule, the
        truncation strategy, the request budget (and why the Django setting
        is left alone), and why `--literal-pathspecs`/`--no-ext-diff`/
        `--no-textconv` are used. Edit the `diff.json` shape example to show
        `patch`/`patch_truncated`. Update the `build_structured_diff`
        docstring (written shape, Raises).
      Verify: `cd cli && .venv/bin/pytest tests/test_git_capture.py`. The existing
      tests pass after the `CannedSandbox` change in item 3. If running before
      item 3, only `test_build_structured_diff_attributes_a_rename_record_to_the_new_path`
      may differ, and only in its patch content; its stats assertion must still
      pass.

- [x] 2. Add `resolve_diff_base` and wire it into the run pipeline (root-commit fix).
      Files: `cli/vaultshield/git_capture.py`, `cli/vaultshield/cli.py`
      - `git_capture.resolve_diff_base(sandbox, branch) -> str`:
        - Validate `branch` with `_checked_ref(branch, "branch")`, which keeps
          the reject-empty and reject-leading-`-` checks.
        - Run `["git", "rev-parse", "--verify", "--quiet", f"{branch}~1"]`
          directly with `sandbox.exec`, not `_exec_or_raise`, because exit 1
          is an expected outcome.
        - On exit 0, return `f"{branch}~1"`.
        - On exit 1, run `_exec_or_raise(sandbox, ["git", "hash-object",
          "-t", "tree", "/dev/null"], step="computing the empty tree")`. Take
          `stdout.strip()`, require it to match `\A[0-9a-f]{40}([0-9a-f]{24})?\Z`
          (otherwise raise `GitCaptureError`), and return it.
        - On any other exit code, raise `GitCaptureError` in the
          `_exec_or_raise` message shape, with
          `step=f"resolving the parent of {branch}"`.
        - Write the docstring with Requirement 10.2 and the root-commit
          reasoning (an empty clone gives a root capture commit), plus a
          `Raises:` section that includes `SandboxError` propagating.
        - Extend the module docstring's step list / diff-command section so
          it explains that the base is the parent commit, or the empty tree
          for a root commit.
      - `cli.py` step 8:
        - Import `resolve_diff_base`.
        - Replace `f"{branch}~1"` with
          `base_ref = resolve_diff_base(sandbox, branch)` and pass `base_ref`
          on.
        - Rewrite the comment block at about lines 398-412: the base is the
          capture commit's first parent, or the empty tree when the capture
          commit is a root (an empty clone). Correct the "single commit"
          wording and remove "Left as-is". Update the step-8 inline comment.
      Verify: `cd cli && .venv/bin/pytest tests/test_git_capture.py` passes, and
      `tests/test_cli.py tests/test_interactive_auth.py` pass after item 3's
      monkeypatch updates.

- [x] 3. CLI tests. Depends on items 1-2.
      Files: `cli/tests/test_git_capture.py`, `cli/tests/test_cli.py`,
      `cli/tests/test_interactive_auth.py`
      - **`test_git_capture.py` refactors (minimal):**
        - Pull the body of the `repo` fixture into `_init_repo(root: Path, *,
          with_base_commit: bool = True) -> GitRepo`, and have the fixture
          call it. The Hypothesis test cannot use function-scoped fixtures,
          and the root-commit test needs a repository with no commits.
        - Change `CannedSandbox.exec` to return `self.stdout` only when
          `"--numstat" in cmd`, and `ExecResult(0, "", "")` otherwise.
        - Update the module docstring to list the new scenarios.
        - Add a helper `_payload(run_dir) -> list[dict]` and a helper
          `_hunk_line_counts(patch) -> tuple[int, int]` that counts `+` and
          `-` lines only after the first `@@` header, mirroring the scanner's
          preamble rule so `+++`/`---` headers are never counted.
      - **New tests in `test_git_capture.py`:**
        - `test_build_structured_diff_records_a_patch_for_each_text_file`:
          after `_make_agent_changes`, `added.py`'s patch contains
          `"+print('new')"` and an `@@ -0,0 +1 @@` header, `kept.txt`'s
          contains `"+agent edit"`, and `doomed.txt`'s contains
          `"-delete me"`. No entry has `patch_truncated`.
        - `test_build_structured_diff_omits_the_patch_for_binary_files`:
          `logo.bin` (the NUL bytes, as in the existing binary test) has
          neither a `patch` nor a `patch_truncated` key and keeps 0/0, while
          a text file changed alongside it does have a `patch`.
        - `test_build_structured_diff_truncates_an_oversized_patch_at_a_line_boundary`:
          write a file of about 300 KiB, built from 100-character lines that
          mix ASCII with multi-byte characters such as `"é"` and `"€"`.
          Assert:
          - `patch_truncated is True`;
          - `len(patch.encode("utf-8")) <= MAX_PATCH_BYTES`;
          - the patch ends with `"\n"`;
          - the patch is a prefix of `repo.git("--literal-pathspecs", "diff",
            "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
            "-U0", base, branch, "--", path).stdout` (use the unstripped
            stdout);
          - `additions` is still the full numstat count.
        - `test_build_structured_diff_does_not_flag_a_patch_that_fits`:
          covered by the first test; assert the key is absent there rather
          than adding a separate test.
        - `test_build_structured_diff_keeps_the_whole_diff_within_the_request_budget`:
          monkeypatch `git_capture.MAX_DIFF_REQUEST_BYTES` to about 4096 and
          create about 10 text files of about 1 KiB each. Assert that
          `len(json.dumps(payload).encode()) <= 4096` (default separators,
          `ensure_ascii=True`), that later entries carry
          `patch_truncated: True`, and that at least one carries `patch ==
          ""`. Also assert, by counting `LocalGitSandbox.commands` entries
          that contain `-U0`, that no per-file `git diff` ran for
          entries whose budget was already exhausted.
        - `test_build_structured_diff_scopes_each_patch_to_its_own_path`:
          files `a*.txt`, `ab.txt` and `:(glob)x.txt` all added. Each patch
          contains only its own content: `a*.txt`'s patch must not contain
          `ab.txt`'s line. This exercises `--` and `--literal-pathspecs`.
          Extend the existing spaces/quotes/tabs test to assert each of
          those entries has a non-empty `patch` containing its own `+` line.
        - `test_resolve_diff_base_returns_the_parent_when_there_is_one`:
          `commit_run_branch` on the `repo` fixture, then
          `resolve_diff_base(sandbox, branch) == f"{branch}~1"`.
        - `test_resolve_diff_base_falls_back_to_the_empty_tree_for_a_root_commit`:
          on the fixture's own root base commit, `resolve_diff_base(sandbox,
          base_branch) == repo.git_ok("hash-object", "-t", "tree",
          "/dev/null")`, and that value equals
          `"4b825dc642cb6eb9a060e54bf8d69288fbee4904"` for this SHA-1
          repository.
        - `test_build_structured_diff_against_a_root_commit_reports_every_file_as_added`:
          an end-to-end root-commit case.
          - Use `_init_repo(..., with_base_commit=False)`, write
            `settings.py` containing `API_KEY = "sk-abcdefghij123456"\n` and
            a second file, then call `commit_run_branch`.
          - Compute `base = resolve_diff_base(...)` and run
            `build_structured_diff(sandbox, base, branch, run_dir)`.
          - Assert `settings.py`'s stats are (1, 0) and its patch contains
            `"@@ -0,0 +1 @@"` and `'+API_KEY = "sk-abcdefghij123456"'`.
          - Assert that the old base `f"{branch}~1"` raises `GitCaptureError`
            from `build_structured_diff`, documenting the bug being fixed.
        - `@pytest.mark.parametrize("bad_ref", ["", "-p",
          "--output=/tmp/pwned"]) test_resolve_diff_base_rejects_a_ref_git_would_read_as_an_option`:
          raises `GitCaptureError` and `sandbox.commands == []`.
        - `test_resolve_diff_base_reports_a_non_repository`: a plain directory
          gives exit 128 and raises `GitCaptureError`.
        - `test_resolve_diff_base_propagates_sandbox_failure`:
          `Sandbox(RUN_ID, client=None)` raises `SandboxError`.
        - **Hypothesis property** `test_additions_and_deletions_match_the_patch_lines`:
          - Decorate with `@settings(max_examples=30, deadline=None,
            suppress_health_check=[HealthCheck.too_slow])` and `@given`.
            Inputs are two lists of lines, `before` and `after`, each a list
            of `st.text(alphabet=st.characters(blacklist_categories=("Cs",
            "Cc")), max_size=30)` with `max_size=15`, plus
            `st.booleans()` for a trailing newline on each.
          - Inside, use `tempfile.TemporaryDirectory()`, then
            `_init_repo(root)` and write `prop.txt = before` in the base
            commit (use `write_bytes` of the UTF-8 encoding to avoid
            newline translation). Then write `after` and run
            `commit_run_branch`, `resolve_diff_base` and
            `build_structured_diff`.
          - If `prop.txt` is in the payload, assert `"patch" in entry` and
            `not entry.get("patch_truncated")`, and that
            `_hunk_line_counts(entry["patch"]) == (entry["additions"],
            entry["deletions"])`.
          - `RUN_ID` can be reused, since each example is a fresh repository.
      - **`test_cli.py`:**
        - In `_wire_run_pipeline`, add
          `monkeypatch.setattr(cli_module, "resolve_diff_base", _resolve)`,
          where `_resolve` records `seen["base_args"] = (sandbox, branch)`
          and returns `f"{branch}~1"`. Do NOT add it to `calls`, so the
          existing pipeline-order lists stay valid.
        - In `test_run_captures_the_branch_and_diff_after_browser_capture`,
          assert `seen["base_args"] == (seen["sandbox"], branch)` and that
          `base_ref` is that return value.
        - Update the comment there ("diffed against the capture commit's
          first parent, or the empty tree for a root commit").
      - **`test_interactive_auth.py`:** add the same `resolve_diff_base`
        monkeypatch next to line 213 (`lambda sandbox, branch:
        f"{branch}~1"`).
      Verify: `cd cli && .venv/bin/pytest tests/test_git_capture.py tests/test_cli.py
      tests/test_interactive_auth.py tests/test_uploader.py`, all passing, then the full
      `cd cli && .venv/bin/pytest` with no new failures (report any pre-existing or
      Docker-dependent skips/failures separately).

- [x] 4. Backend: accept and persist `patch`/`patch_truncated`.
      Files: `backend/runs/serializers.py`, `backend/runs/models.py` (comment only)
      - `FileDiffStatSerializer`: add the two fields from Decision 7.
        - Rewrite the "exactly these three keys" paragraph of its docstring.
          The validated result now carries `path`, `additions` and
          `deletions`, plus `patch` and `patch_truncated` only when sent;
          other keys are still dropped.
        - Explain `trim_whitespace=False` (diff text is content: a
          context/blank line or trailing whitespace must survive byte for
          byte, or the scanner's line walk changes).
        - Explain `allow_blank` (a mode-only change or a budget-exhausted
          entry has `""`).
        - Explain why there is no `max_length`: the request body is already
          bounded by `DATA_UPLOAD_MAX_MEMORY_SIZE`, and the CLI keeps under
          it.
        - Cite Requirements 13.2 and 17.1.
      - Update the `RunDiffSerializer` and `RunDiffIngestSerializer` docstrings'
        shape descriptions to include the optional keys.
      - Update the inline comment on `RunDiff.files` in `runs/models.py` to
        `[{"path", "additions", "deletions", "patch"?, "patch_truncated"?}, ...]`.
        This is a comment only, so no migration is needed. Confirm with
        `manage.py makemigrations --check --dry-run` if it can run without a
        DB; otherwise skip that check and say so.
      - Leave `rules.py`/`tasks.py` untouched. Their module docstring
        describing "the gap" goes stale; mention this in the final report as
        a follow-up rather than editing the scanner.
      Verify: `cd backend && VAULTSHIELD_MONGODB_URL=mongodb://localhost:27017/vaultshield
      .venv/bin/python manage.py check` reports no issues, and
      `VAULTSHIELD_MONGODB_URL=... .venv/bin/pytest runs/tests.py` passes, including
      the existing malformed-`files` cases.

- [x] 5. Backend tests. Depends on item 4.
      Files: `backend/runs/tests.py`, `backend/vaultshield_backend/test_mongodb.py`
      - **`runs/tests.py`, `TestRunDiffIngestSerializer`** (database-free;
        update the module docstring's Task 16.7 paragraph to mention patch
        passthrough):
        - `test_a_patch_and_truncation_flag_are_kept_verbatim`: an entry
          whose patch has leading spaces, a trailing-space context line,
          `\n` endings and `"\\ No newline at end of file"`, with
          `patch_truncated: True`. `validated_data["files"]` equals the
          input exactly.
        - `test_entries_without_a_patch_validate_to_the_three_stat_keys`:
          `WELL_FORMED` gives entries whose key set is exactly
          `{"path", "additions", "deletions"}`. No `patch`/`patch_truncated`
          keys appear, not even as None or False.
        - `test_an_empty_patch_is_accepted`.
        - `test_a_malformed_patch_or_flag_is_rejected`: `patch` in
          `(None, ["x"], {"a": 1}, True)` and `patch_truncated` in
          `("maybe", [], None)`. Each fails `is_valid()` with `"files"` in
          the errors. Note that DRF's `CharField` coerces ints, so ints are
          deliberately not in the list.
        - `test_the_rendered_diff_round_trips_the_patch`:
          `RunDiffSerializer(RunDiff(files=validated)).data["files"] ==
          posted["files"]`, using an unsaved instance so no database is
          touched.
        - Keep every existing malformed-`files` test unchanged; they are
          the evidence that 400 behaviour is preserved at serializer level.
      - **`test_mongodb.py`, `TestRestApiAgainstMongoDB`:**
        - `test_a_diff_with_a_patch_round_trips_through_run_detail`: POST
          `/api/runs/<id>/diff/` with a text entry (patch plus
          `patch_truncated: True`) and a binary entry (no patch), inside
          `patch("runs.views.run_security_scan.delay")`. Expect 201, then a
          GET whose `response.data["diff"]["files"]` equals the posted list
          exactly. This goes through `RunDetailSerializer`.
        - `test_a_diff_without_a_patch_still_round_trips`: the same with
          only stat keys. Expect 201, and the detail `files` equals the
          posted list with no added keys.
        - `test_a_malformed_diff_is_rejected_with_400_and_nothing_stored`:
          bodies `{"files": "nope"}`,
          `{"files": [{"path": "a.py", "additions": 1, "deletions": 0,
          "patch": ["x"]}]}` and `{"files": [{"path": "a.py"}]}`. Each gets
          400 with `"files"` in `response.data`,
          `RunDiff.objects.filter(run=run).exists()` is False, and the
          patched `delay` is not called.
      - **`test_mongodb.py`, new class `TestDiffIngestProducesSecurityFindings`**
        (`@pytest.mark.django_db`, placed after `TestSecurityScanOrdering`;
        docstring cites Requirements 13.2, 17.1, 17.2 and 18.1):
        - `test_a_posted_secret_becomes_a_secret_literal_finding_on_its_line`:
          POST a `-U0`-shaped patch like the CLI produces:
          ```
          diff --git a/settings.py b/settings.py
          index 1111111..2222222 100644
          --- a/settings.py
          +++ b/settings.py
          @@ -10 +10,2 @@
          -DEBUG = True
          +DEBUG = False
          +API_KEY = "sk-abcdefghij123456"
          ```
          as `{"path": "settings.py", "additions": 2, "deletions": 1,
          "patch": ...}`, inside
          `patch("runs.views.run_security_scan.delay",
          side_effect=run_security_scan)`, so the real Celery task body runs
          synchronously from the backend's `on_commit`, after the diff
          commits.
        - Assert a 201 and that
          `list(SecurityFinding.objects.filter(run=run).values_list("rule_id",
          "file_path", "line_number"))` is
          `[("secret-literal", "settings.py", 11)]`. Line 11 is right
          because the hunk starts at 10, the removed line does not advance
          the position, `DEBUG` sits at 10, and so the key is at 11.
        - Also assert that the GET detail's `security_findings` contains
          that finding.
      Verify:
      - `cd backend && VAULTSHIELD_MONGODB_URL=... .venv/bin/pytest
        runs/tests.py` passes.
      - With a replica set (see Environment), run `.venv/bin/pytest
        vaultshield_backend/test_mongodb.py security_scan/tests.py runs/tests.py`
        and confirm the new tests pass, then run the full `.venv/bin/pytest`.
      - If no replica set is reachable, report precisely which tests were not
        run: the three new `TestRestApiAgainstMongoDB` tests, the
        integration test, and every other `django_db` test.

- [x] 6. Document the caps and the size decision.
      Files: `cli/README.md`, `backend/README.md`
      - `cli/README.md`: add a section "## Diff capture: patches and size
        limits" covering:
        - `diff.json` per-file `patch` (`git diff -U0`), and that binary
          files have no `patch`;
        - `MAX_PATCH_BYTES` (200 KiB per file, line-boundary truncation,
          `patch_truncated: true`);
        - `MAX_DIFF_REQUEST_BYTES` (2 MiB total, sized under Django's
          2.5 MiB default, and that later files get `patch: ""` once it is
          spent);
        - the residual very-many-files limit;
        - the root-commit base (the empty tree when the capture commit has
          no parent).
      - `backend/README.md`: add a short note, for example under
        Configuration or a new "Request size" paragraph:
        `DATA_UPLOAD_MAX_MEMORY_SIZE` is deliberately left at Django's
        2.5 MiB default because it is global, including for the
        unauthenticated login. The CLI keeps event batches and `diff.json`
        under it. A larger body is rejected with 400. Also note the
        `RunDiff.files` entries can carry `patch`/`patch_truncated`, which
        the security scan reads.
      Verify: docs only. Re-run `cd cli && .venv/bin/pytest
      tests/test_git_capture.py` to confirm the constants the docs name are
      the ones in code (the tests import `MAX_PATCH_BYTES`).

- [x] 7. Final cross-check.
      - Re-run the full CLI suite and the backend suites from the
        Environment section.
      - Run `git status` and `git diff --stat`. Only the files named above
        (plus the plan/review artifacts under `.agents/`) should change.
        Do not stage `.venv/`, `.DS_Store` or `.vaultshield/`.
      - Report:
        - every test that could not be run, and why (expected: the MongoDB
          replica-set tests if Docker/Atlas is unavailable);
        - the stale "gap" docstring in `security_scan/rules.py`, which was
          intentionally not edited;
        - the corrected empty-tree hash.
      Verify: both suite commands are green, or each non-run test is
      explicitly listed with its reason.

## VERIFICATION

Iteration 1 (no `review.json` existed). Changes are uncommitted.

Environment: Python 3.11 venvs created at `cli/.venv` and `backend/.venv`
(`pip install -e ".[dev]"` in each; hypothesis was already a CLI dev dep, so
no pyproject change). For MongoDB, Docker Desktop was started (it was not
running). Port 27017 was already taken by an unrelated `ipsakti-mongo`
container that Docker auto-started, so the test replica set ran on **27018**:
`docker run -d --rm --name vs-test-mongo -p 27018:27017 mongo:7 --replSet rs0
--bind_ip_all`, then `rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]})`.
The container was stopped afterwards and Docker Desktop was quit again, which
also stops the auto-started `ipsakti-*` containers, restoring the pre-run
state.

CLI (`cd cli`):
- `.venv/bin/pytest -q tests/test_git_capture.py` → **47 passed** (includes
  the Hypothesis property, 30 examples).
- `.venv/bin/pytest -q` (full) → **17 failed, 639 passed, 22 skipped**.
  All 17 failures are in `tests/test_interactive_auth.py` and are
  pre-existing: on a `git stash` of all CLI changes the same command gives
  17 failed, 625 passed, 22 skipped. Cause:
  `_Sandbox.exec_interactive() got an unexpected keyword argument 'timeout_s'`
  (the test double is out of date with `cli.py`), unrelated to this change.
  No failure outside that file. Skips are the existing Docker-dependent tests.

Backend (`cd backend`):
- `VAULTSHIELD_MONGODB_URL=mongodb://localhost:27017/vaultshield .venv/bin/python manage.py check`
  → no issues; `.venv/bin/pytest -q runs/tests.py` (DB-free) → **63 passed**.
- With `VAULTSHIELD_MONGODB_URL` and `VAULTSHIELD_TEST_MONGODB_URL` set to
  `mongodb://localhost:27018/vaultshield?directConnection=true`:
  - `.venv/bin/pytest -q vaultshield_backend/test_mongodb.py security_scan/tests.py runs/tests.py`
    → **215 passed**.
  - `.venv/bin/pytest -q` (full) → **285 passed**.
  - `.venv/bin/python manage.py makemigrations --check --dry-run` → No changes detected.
- New tests confirmed PASSED by name: the 3 new
  `TestRestApiAgainstMongoDB` tests (patch round-trip, no-patch round-trip,
  malformed 400 + nothing stored + no scan enqueued) and
  `TestDiffIngestProducesSecurityFindings::test_a_posted_secret_becomes_a_secret_literal_finding_on_its_line`
  (`[("secret-literal", "settings.py", 11)]`). With the old
  `serializers.py` swapped back in, the integration and patch round-trip
  tests fail (2 failed), so they test the change.
- One-off probe (temporary test file, deleted afterwards): a ~3 MiB JSON
  POST to `/diff/` returns Django's 400, so `DATA_UPLOAD_MAX_MEMORY_SIZE`
  does bound DRF JSON bodies on this stack, as the docs now say.

Not run: nothing from the requested set was skipped.

Notes for the reviewer:
- Empty-tree hash: `4b825dc642cb6eb9a060e54bf8d69288fbee4904` (SHA-1); code
  uses `git hash-object -t tree /dev/null`, never a constant.
- Deviation from the plan's `_truncate_patch` signature: it returns a 4th
  value `budget_hit`. Without it, a file cut by the request budget left a
  few bytes of positive remainder, so every later file still ran `git diff`
  and contradicted Decision 6 ("no `git diff` is executed for them"). A cut
  forced by the budget now sets the remainder to 0.
- `security_scan/rules.py`'s module docstring describing "the gap" (CLI
  sending no `patch`) is now stale; intentionally not edited per the task.

### Iteration 2 (review.json present, verdict CHANGES_REQUESTED)

Findings addressed:
- `nul-in-text-patch` (blocking): `build_structured_diff` now replaces every
  `\x00` in a captured patch with U+FFFD (`_NUL`/`_NUL_REPLACEMENT`) before
  `_truncate_patch`, so truncation and budget costing see the text actually
  sent. Module docstring gained a "NUL characters" paragraph; `cli/README.md`
  notes it. New test
  `test_build_structured_diff_replaces_a_late_nul_in_a_text_patch`
  (`b"a\n" * 5000 + b"x\x00y\n"`): asserts git's own patch contains a NUL,
  numstat counts 5001 additions (text), and the written patch has no NUL and
  equals git's patch with NUL -> U+FFFD.
- `gitattributes-binary-bypass` (non-blocking): documented as a known gap in
  the module docstring (next to the `--no-textconv` reasoning) and in
  `cli/README.md`. Not fixed in code: `--attr-source` or a blob NUL check
  would change which files carry a patch, which is a behaviour change left
  for the user to decide.
- `full-patch-buffered` (non-blocking): recorded as "Known costs" in the
  module docstring (full stdout buffered before truncation; one exec per
  text file). No code change.
- `scanner-splitlines`, `stale-rules-docstring` (follow-ups): not changed;
  `rules.py` is frozen for this task.

Commands run:
- `cd cli && .venv/bin/pytest -q tests/test_git_capture.py` → **48 passed**.
- `cd cli && .venv/bin/pytest -q` → **17 failed, 640 passed, 22 skipped**;
  the 17 failures are the same pre-existing `tests/test_interactive_auth.py`
  failures (`exec_interactive() got an unexpected keyword argument
  'timeout_s'`) as in iteration 1.
- `cd backend && .venv/bin/pytest -q runs/tests.py` → **63 passed**.

Not re-run this iteration: the MongoDB-backed backend suites
(`vaultshield_backend/test_mongodb.py`, `security_scan/tests.py`, full
backend). No backend file changed in iteration 2, so the iteration 1 result
(285 passed) still applies; re-running needs the Docker replica set from
iteration 1.
