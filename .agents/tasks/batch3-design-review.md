# Batch 3 design review — round 4 (whole design, focus on trusted capture D9 / §9 item 12)

Reviewed: `.agents/tasks/batch3-design.md` (all sections, including §10.3), against
`.agents/tasks/batch3-investigation.md` and the cited source. Nothing was run; claims
were checked by reading code.

Verdict: **CHANGES_REQUESTED** (1 HIGH, 4 MEDIUM, 7 NIT).

## Trusted-capture checklist (what the design settles)

| Item | Settled? | Where |
|---|---|---|
| Mechanism chosen: (b) clean helper container, (a) host git rejected with code evidence (APFS EILSEQ, case folding) | Yes | D9, §5.4.1, §9 item 12 |
| How the branch reaches its destination today (`commit_run_branch` in Sandbox, dies at teardown; `submit` pushes from host `repo_root`, named gap) | Yes, verified in code | §5.4.1 step 7 |
| Safe tar handling: absolute names, `..`, NUL, empty components, duplicates, devices/FIFO/sparse, hardlinks, symlinked parents, root `repo` and `.git` rules, malformed stream | Yes | §5.4.1 step 2 |
| Container `.git` untrusted; branch rebuilt from base oid + pre-agent object snapshot | Yes | 5-base, steps 1, 4, 5 |
| Size limits, per stream, fail loudly | Yes (work tree / objects); **no** for the credential snapshot (finding 2) | §5.4.1 step 3, §5.5 |
| Credential scan on host / helper-produced bytes | Yes | §5.4.1 step 8, §5.5 |
| Egress policy, phase separation, teardown order | Coherent; one port-collision gap (finding 4) | D1-D5, D10, §5.1-5.2 |
| Token hygiene | Needle sourcing gap (finding 1) | D9, §5.5 |
| §8 test plan | Test 6 is out of step with D9 (finding 5) | §8.2 |

## Findings

### 1. HIGH — Post-agent credential snapshots turn agent session data into needles, so legitimate Antigravity Runs can fail token hygiene (§5.5 `snapshot`, D9 #2a/#2)

`snapshot(sandbox, ("/root/.gemini",))` takes, from **every** regular file ≤ 1 MiB under the root, the whole file (32 B–64 KiB) and every `[A-Za-z0-9._~+/=-]{32,}` run with a digit, upper and lower letter. Snapshots #2a and #2 run *after the agent*. What `agy` writes under `/root/.gemini` during a Run is **[unknown]** (inv §3: only `jetski_state.pbtxt`, `mcp_oauth_tokens.json`, etc. are known names from `strings`). Agent CLIs in this family keep session state, logs and conversation transcripts under `~/.gemini` (Gemini CLI: `~/.gemini/tmp/<hash>/logs.json`, checkpoints). If `agy` does the same, the prompt, the agent's own narration and code it wrote become needles. Any of them appearing in `diff.json` (the code itself), in `events.jsonl` (the agent's terminal output) or in a changed blob produces a `credential_bytes` finding and fails the Run, in **every** egress mode (token hygiene is always on, §9 item 4). Example: the identifier/URL run `//github.com/OrgName/repo2/blob/main/src` qualifies as a token-shaped run. Mock tests cannot reveal this, because the mock writes nothing under the root.

Fix (choose this rule and state it in D9 / §5.5):
- Snapshot #1 (post-sign-in, pre-agent) defines the **credential file set**: the relative paths of the regular files under each root at #1.
- Snapshots #2a and #2 re-read only those paths, plus any new file whose basename matches `(?i)(token|oauth|credential|cred|secret|auth|session|\.pbtxt$|\.json$)` **and** that is not under a directory named `tmp`, `logs`, `history`, `brain`, `conversations` or `cache` (case-insensitive). Other new files contribute no needles.
- Whole-file needles are taken only from files ≤ 8 KiB (a credential, not a state dump); token-shaped runs from all selected files.
- Add §9 item 14 (sign-off): "needles come from the credential file set fixed at sign-in; the developer probe's `/root/.gemini` listing (§3.2) must be checked against this rule before the first real Run, and `Antigravity_Adapter.credential_paths()` narrows to the confirmed file once known."
- Unit test in `test_token_hygiene.py`: snapshot #1 holds `antigravity-cli/token` (sentinel); #2 additionally holds `antigravity-cli/brain/conv.json` containing a 40-char mixed run that also appears in a `diff.json` patch → **no** finding; the sentinel copied into the patch → finding.

### 2. MEDIUM — Credential snapshot limits drop data silently (§5.5 `snapshot`)

"From each regular tar member of ≤ 1 MiB" and "The total is capped at 8 MiB" do not say what happens to a larger member or to data past the cap. As written, a credential file > 1 MiB (or one archived after 8 MiB of other files) contributes no needles and nothing records it. That contradicts the design's "fail loudly, nothing skipped silently" rule (§5.4.1 step 3) and leaves an unspecified error path.

Fix: specify — needles are extracted in archive order until the 8 MiB total; every skipped member (over 1 MiB or past the cap) is counted, and if the count is non-zero one non-failing `violation` event is appended: `token hygiene: <n> credential-root files not scanned for needles (over 1 MiB or past the 8 MiB cap)`. The snapshot tar stream itself is bounded by `_ChunkReader(stream, 64 MiB)` (fail loudly with the "credential snapshot failed" `GitCaptureError` above that). Unit cases: a 2 MiB member → event, no exception; 9 MiB of 1 MiB members → event naming the skipped count.

### 3. MEDIUM — Untrusted `.git/config` check 2 has no rule for a config git cannot parse (§5.4 "Check 2 runs as `git config --file /tmp/untrusted-config …`")

Only "exit 1 → no match, OK" and "any match → `UnsafeGitConfig`" are defined. The file is agent-controlled; a malformed config (e.g. `[filter "x"` with no closing bracket) makes `git config --file` exit 3 ("bad config line"), and exit 0 with empty output is also undefined. Today a malformed `.git/config` fails the Run (every in-Sandbox git command errors); under the helper it could silently pass or crash depending on the implementer's choice.

Fix: state the full rule —
- exit 0 and non-empty stdout → `UnsafeGitConfig("trusted capture: the Sandbox .git/config defines a filter or diff driver")`;
- exit 1 (or exit 0 with empty stdout) → OK;
- any other exit → `UnsafeGitConfig("trusted capture: the Sandbox .git/config could not be parsed (git config exit <n>)")` (fail closed, same clause as other hygiene failures).
Add a `test_trusted_capture.py` case: fake helper exec returning exit 3 → that `UnsafeGitConfig`.

### 4. MEDIUM — App port can collide with the proxy's own listeners (D4, §5.2 "The proxy listens on 3128 (CONNECT), on 443 (SNI) and on P (forward)")

In `allowlist`/`log-only` the proxy binds 3128, 443 and the app port P = `config.port`. Nothing validates P. With `port: 443` or `port: 3128` in `.vaultshield.yml` the proxy fails to start (or one listener shadows the other), surfacing as an opaque "missing ready" `EgressError` after networks were created.

Fix: in `resolve_policy(agent, config_egress, cli_mode, *, app_port)` (step 1, before anything exists): if the effective mode is not `off` and `app_port in (3128, 443)`, raise `EgressConfigError("egress: the app port <p> collides with the egress proxy's port <p>; change `port` in .vaultshield.yml or use --egress=off")` → `ClickException`. Add the parameter to the §6 egress row and two `resolve_policy` unit cases (443, 3128 rejected; 3000 accepted; off mode with 443 accepted).

### 5. MEDIUM — §8.2 test 6 no longer matches D9 and is under-specified

- Vector (2) claims to confirm "hardened capture … on the image's own git". Under D9 the helper initialises a **fresh** repo and never reads the Sandbox's `.git/config` (only the check-2 copy), so `core.worktree=/root` is discarded by construction; the image git never sees it with the `_git` prefix. The §1 `core.worktree` row ("the image's Debian git is re-checked by the Docker-gated test (§8)") has the same stale claim.
- Test 6 does not say how capture runs. `check_run_outputs(capture, …)` needs a `TrustedCapture`, but the test only says "hardened capture"; an implementer could call today's `commit_run_branch(sandbox)` and test the wrong path.
- The counter-case needs a served repo that already tracks `docs/latest -> /usr/share/doc/x`. The existing `served_repo` fixture (`test_sandbox_execution.py` L181-230) commits fixed content only (`AGENT_EDIT_TARGET`, `README.md`, `protected.txt`), so a new fixture is required and is not named.

Fix:
- Test 6 runs every vector through a shared helper `_run_trusted_step8(sandbox, client, run_dir, secrets, base) -> (branch, base_ref)` that performs exactly the §7 step-8 sequence (also used by test 7).
- Vector (2) asserts: the helper branch's `ls-tree -r --name-only` has no path outside the repo, no finding, and the helper repo's `git config --get core.worktree` exits 1. Reword it as "the Sandbox's `core.worktree` has no effect on trusted capture"; drop the "image's own git" claim here and in the §1 row (the `_git` prefix is verified on host git by `test_git_capture.py` (a) and is defence in depth inside the helper).
- Add fixture `served_repo_with_tracked_symlink` (same daemon pattern on a second port, or a `served_repo` factory taking an `extra_setup(source)` callback) that commits `docs/latest` via `os.symlink("/usr/share/doc/x", source/"docs/latest")` (works on APFS).

### 6. NIT — `GIT_CONFIG_GLOBAL` needs git ≥ 2.32, the gate is 2.29 (§5.4 `_GIT_ENV`, §5.4.1 step 4)

On git 2.29–2.31 `GIT_CONFIG_GLOBAL=/dev/null` is ignored, so the stated guarantee "cannot read global/system config" is false there. Harmless in the helper (its `$HOME` is the trusted image), but the claim is wrong. Fix: raise the floor to 2.32 (`TrustedCapture.start()` message "older than 2.32"; unit case `git version 2.31.8` → error), or add `"HOME": "/nonexistent"` to `_GIT_ENV`. Prefer the floor change.

### 7. NIT — Pre-agent trusted-capture failures claim an Event_Log that does not exist (§7 notes)

`GitCaptureError` from `record_trusted_base` (5-base) or `token_hygiene.snapshot` at 5c reaches the existing `except GitCaptureError` clause (`cli.py` L863), whose message ends "(Event_Log preserved at …)". At 5-base/5c no Event_Log has been written. The `AuthError` clause (`cli.py` L821-828) already avoids this. Fix: in that clause append the suffix only when `event_log_path(run_dir).exists()`; otherwise end with "(the agent never started)". One `test_cli.py` case.

### 8. NIT — The untrusted-config spool must be a tar for `put_archive` (§5.4.1 step 1 "copied as plain bytes", step 4 `put_archive(/tmp, untrusted-config)`)

`put_archive` takes a tar stream; "plain bytes" can be read as a raw file. Fix: `SplitArchive.untrusted_config` is a PAX tar spool with exactly one member `untrusted-config` (regular, mode `0o644`, uid/gid 0, the config bytes), written with the same `tarfile.open(..., format=PAX_FORMAT)` call; unit case reads it back.

### 9. NIT — Which host spelling goes into egress violation payloads (§5.3)

D5 normalises (lowercases) the host; §5.3 says `<host>` "comes from the agent". If the lowercased form is logged, `SecretSet.scrub` (case-sensitive) misses a token the agent placed in a hostname (`<Token>.example.com`), and a lowercased copy reaches the dashboard. Fix: the proxy logs the raw requested host (pre-normalisation) in a `raw_host` field, §5.3 payloads use the sanitised `raw_host`, and a `test_egress.py` case asserts scrub replaces a needle embedded in `raw_host`.

### 10. NIT — `vaultshield teardown <id>` cannot find the proxy staging dir (D6, D10, §6 teardown row)

`tempfile.mkdtemp(prefix=f"vaultshield-egress-{run_id}-")` has a random suffix; after a hard kill only `guard.teardown` knew it. No secrets, but it leaks. Fix: `remove_egress_resources` also removes `glob(f"{tempfile.gettempdir()}/vaultshield-egress-{run_id}-*")` with `shutil.rmtree(ignore_errors=True)`; one `test_teardown.py` case.

### 11. NIT — Two requirement statements contradict the detailed design

- FR5 "Repo symlinks pointing outside the repo are refused" vs §5.5 (d) / §9 item 12 (only links the Run **added or retargeted** fail; tracked unchanged ones pass). Fix FR5: "Symlinks the Run adds or retargets to a path outside the repo fail the Run."
- NFR1 lists the helper's calls as the only off-mode Docker additions, but off mode also adds Sandbox `get_archive` calls (5-base, snapshots #1/#2a/#2, step-8 work tree) and `container.kill()`. Fix NFR1 to list them.

### 12. NIT — Stale signature in §10.2 row 11

It records `check_run_outputs(capture, secrets, run_dir, branch, *, extra_findings=())`, superseded by §10.3 row 6 (`…, branch, base_ref`). Append "(superseded by §10.3 #6)".

## Verified assumptions

- `Sandbox.exec(self, cmd)` takes no env and calls `self.container.exec_run(cmd, workdir=_REPO_DIR, demux=True)`, filling `stdout_bytes` (`sandbox.py` L637-677); `ExecResult.stdout_bytes` exists (L460-484). `_KEEP_ALIVE_CMD = ["tail", "-f", "/dev/null"]` at L141; public `WORKDIR`/`REPO_DIR` aliases exist (L94-95).
- `git_capture` depends on the sandbox only through `.exec`: `_exec_or_raise` calls `sandbox.exec(cmd)` (L1006), the `rev-parse --verify --quiet` probe at L721, bare `["git", …]` argv for `checkout -b`, `add --all`, `diff --numstat -z --no-renames`, `hash-object -t tree /dev/null`. So D9's "unchanged bodies, new exec target" is feasible.
- `cli.py` step 8 calls `commit_run_branch(sandbox, run_id)`, `resolve_diff_base`, `build_structured_diff` at L815-817; `except SandboxError` L828; `except GitCaptureError` L863 (with the "Event_Log preserved" suffix); `sandbox.teardown()` in the inner `finally` L890; `_upload_run_artifacts` only on the success path L899; outer `except BaseException: _mark_run_failed_best_effort` L907. Matches §1 and §7.
- `submit()` pushes `vaultshield/run-<run_id>` from `repo_root` and documents the "branch is not in the host checkout" gap (`submit.py` L207-224); the branch does not leave the Sandbox today. §5.4.1 step 7 is accurate.
- `LocalGitSandbox` (L171) and `CannedSandbox` (L400) in `test_git_capture.py` have `exec(self, cmd)` without env, so the `env=None` addition is a needed, stated change. `_commit_non_utf8_file` (L869-895) documents APFS `EILSEQ`, supporting the rejection of option (a).
- `browser_capture.resolve_base_url(sandbox, port)` and `start_and_capture(sandbox, config, …)` have no `published_by` today (L369, L474): the D4 change is additive.
- `Mock_Adapter` is in `adapters/mock.py`, `Antigravity_Adapter` in `antigravity.py`, `Agent_Adapter` in `base.py`: test 8's monkeypatch seam targets exist.
- `teardown.teardown_sandbox` (L349) finds the container via `container_name(run_id)`: derived names for helper/proxy/networks fit the same pattern.
- Tests referenced as existing do exist: `served_repo`, `_offline_backend`, `test_agent_executes_inside_the_sandbox_and_cannot_reach_the_host`, `test_an_exec_environment_does_not_persist_into_the_container` (`test_sandbox_execution.py`), `test_event_type_is_not_restricted_to_a_choice_set` (`backend/runs/tests.py` L617).
- Investigation facts are carried correctly: default bridge + NET_RAW, no NET_ADMIN, `internal: true` drops external DNS and port publishing, failed Runs upload nothing, token path under `/root/.gemini` outside `REPO_DIR`, filename unknown.

## Unverified / wrong assumptions

- **Unverified, load-bearing:** what `agy` writes under `/root/.gemini` during a Run (finding 1). Only the developer probe listing can settle it.
- **Wrong:** §1 `core.worktree` row and §8.2 test 6 (2) say the image's git re-checks the `_git` prefix against `core.worktree`; under D9 the helper never reads the Sandbox config (finding 5).
- **Wrong for git 2.29–2.31:** `GIT_CONFIG_GLOBAL` is honoured (finding 6).
- **Wrong as specified:** the existing `served_repo` fixture can provide a base repo with a tracked symlink (finding 5).
- Deferred to Docker-gated tests, acceptable as stated: `get_archive` on an exited container (test 7), image git version (test 6 prints it), PAX non-UTF-8 round trip through Docker (test 8 Case A), proxy alias resolution on the int network (test 1), Linux int-gateway reachability (§5.6, disclosed).
