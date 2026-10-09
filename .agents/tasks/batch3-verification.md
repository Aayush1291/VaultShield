# Batch 3 verification (iteration 1, updated after the real-agy log-only probe)

Implemented per `.agents/tasks/batch3-design.md` (§6 in dependency order, §7, §8, §5.4.1 option b) with the round-4 findings / §10.4 responses. Nothing staged or committed (`git diff --cached --name-only` → empty). `git diff --quiet backend/security_scan/rules.py` → passes. Backend and frontend not touched, so their suites were not run.

Environment: macOS, Docker Desktop 29.2.1, `vaultshield-sandbox:local` (git 2.39.5 in the image, so the helper's ≥ 2.32 floor holds), proxy image `python:3.11.16-slim@sha256:e41613d4…b41e` pulled with `docker pull` before the run.

## Files

New: `cli/vaultshield/egress_proxy.py`, `egress.py`, `trusted_capture.py`, `token_hygiene.py`, `cli/isolation_probe_net.py`, `cli/tests/test_egress.py`, `test_trusted_capture.py`, `test_token_hygiene.py`, `test_egress_docker.py`.
Changed: `cli/vaultshield/{cli,config,sandbox,git_capture,uploader,browser_capture,teardown}.py`, `adapters/{base,antigravity}.py`, `cli/isolation_probe.py`, `cli/README.md`, `cli/tests/{test_cli,test_config,test_git_capture,test_browser_capture,test_teardown,test_interactive_auth,test_uploader}.py`.

Existing-test edits (no assertion weakened): stand-ins gained `env=None` / `published_by=None` / `**egress_kwargs` / trusted-capture stubs; `test_run_captures_the_branch_and_diff_after_browser_capture` now asserts git capture runs on the capture helper and NOT the Sandbox (stricter); `test_removes_the_sandbox…` asserts the extra helper/proxy/network lookups by name; the `_wire_run_pipeline` supervise stub appends instead of truncating `events.jsonl` (matches the real `_append_event_log`).

## Suites run

Full CLI suite, exact command:
```
cd cli && .venv/bin/python -m pytest -q -rs --deselect tests/test_sandbox_execution.py::test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever
```
Result after the log-only follow-up changes: **1 failed, 961 passed, 1 deselected** (95.7 s); before them: 1 failed, 956 passed. Baseline before this batch: 1 failed, 728 passed. The one failure is the pre-existing `test_sandbox_execution.py::test_exec_interactive_really_delivers_stdin_to_a_container_process` (echo redaction, out of scope). No skips reported by `-rs`: every Docker-gated test ran.

Per file (collected = passed, unless noted): test_egress 75, test_trusted_capture 82, test_token_hygiene 20, test_egress_docker 14, test_cli 91, test_git_capture 59, test_config 63, test_browser_capture 36, test_teardown 12, test_interactive_auth 37, test_uploader 50, test_sandbox_execution 24 (22 passed, 1 pre-existing failure, 1 deselected). New tests in total: 233 (961 − 728); +5 in the follow-up (file-root literals, symlinked credential path, real-listing item-14 rule, real proxy log → would_deny event, antigravity list decision). `test_egress_docker.py` re-run after the follow-up: 14 passed.

### Docker-gated `tests/test_egress_docker.py` — all 14 RAN and PASSED (`-v`, none skipped)

1. `test_a_blocked_connection_is_blocked_and_recorded` — measured in-Sandbox report: CONNECT example.org / 1.1.1.1 / host.docker.internal:8000 / random label → `403`; direct 1.1.1.1:443, UDP 8.8.8.8:53, IPv6 → `OSError` (ENETUNREACH); random label → `gaierror`; int gateway :8000, :27018 and a live host listener port → `ConnectionRefusedError`; raw ICMP → `PermissionError`; loopback `curl http://127.0.0.1:8765/` → `200` with no proxy log line; probe name was `example.net`; exact `example.org` violation event written; no leftovers.
2. `test_an_allowlisted_host_is_reachable` (internet) — CONNECT example.com → 200 + TLS handshake.
3. `test_dns_exfil_is_blocked` — label unresolvable; CONNECT denied `not_in_allowlist`, no allow line.
4. `test_the_app_port_is_reachable_through_the_proxy` — host GET via the proxy's published port → 200.
5. `test_run_with_egress_allowlist_end_to_end` — real `vaultshield run --agent=mock --egress=allowlist` (upload stubbed, backend offline) → exit 0, Event_Log + `egress.jsonl` + `diff.json`, no leftovers.
6. `test_token_hygiene_vectors_in_a_real_container[copy|core_worktree|symlink|gitignored|tracked_symlink]` — all through `_run_trusted_step8` (the §7 step-8 sequence). copy → `credential_bytes`; symlink → `symlink_outside_repo`; core_worktree=/root → only in-repo paths, no finding, helper `config --get core.worktree` exit 1; .gitignored copy → not committed, not in diff.json, no finding; base-tracked escaping `docs/latest` → no finding and no deletion.
7. `test_a_tampered_in_container_git_cannot_hide_a_file_from_the_branch_or_diff[True|False]` — replaced `/usr/bin/git`, assume-unchanged, info/exclude, alternates, refs/replace forged commit, deleted base pack, post-checkout hook, nested `git init sub`, background writer. Sandbox `exited` after kill, `get_archive` on it worked; `/tmp/fake-git-ran` and hook marker absent; branch has hidden.txt, README.md, sub/secret.txt (100644, no 160000), .cache/grow.log; parent == recorded base oid; forged object absent; nested-git violation event; [True] `credential_bytes` for hidden.txt, sentinel in no message/event; [False] no finding, file still in branch and diff.
8. `test_trusted_capture_preserves_non_utf8_and_case_variant_names[A|B]` — real `run` via CliRunner on the APFS host. A: exit 0, `A.txt`, `a.txt`, `\ufffd.txt` (patch_truncated, no patch). B: exit 1, `token hygiene violation: credential_bytes in branch (\ufffd.txt)`, `diff.quarantined.json`, one failed mark, sentinel nowhere.

Cleanup after all runs: `docker ps -a --filter label=vaultshield.run_id` → 0, `docker network ls --filter label=vaultshield.run_id` → 0, no `vaultshield-run-*`/`vaultshield-capture-*`/`vaultshield-egress-*` containers, no `vaultshield-egress-*` staging dirs in `$TMPDIR`. (Six `vaultshield-*-1` containers present are the developer's docker-compose stack, started separately; untouched.)

## Reachability, measured with the probe tooling (no `agy` sign-in)

`isolation_probe.py --agent antigravity --egress off|allowlist` (same `EgressGuard`+`create`+`isolate` path as `run`; no agent code ran, so these are the container's network facts). Results in `~/.vaultshield/probes/20261009T0806*Z-egress-{off,allowlist}.json`.

| Target | off (today) | allowlist |
|---|---|---|
| docker.sock / host paths | absent / absent | absent / absent |
| 172.17.0.1, host.docker.internal :5050/8000/27017/27018 | reachable (RST: nothing listening at the time) | name unresolvable; via proxy 403 |
| int-network gateway :5050/8000/27017/27018 | n/a | refused; a VM-published port there times out (measured separately) |
| gateway.docker.internal | resolves 192.168.65.1 | unresolvable |
| LAN gateway 192.168.43.1:80 | timeout (routed) | ENETUNREACH |
| 10.0.0.1, 172.16.0.1, 169.254.169.254 :80 | timeout (routed) | ENETUNREACH |
| example.com:443 TLS, 1.1.1.1:443 | OPEN | unresolvable / ENETUNREACH; CONNECT → 403 |
| oauth2.googleapis.com via CONNECT / via SNI | (direct) | 200 + TLS / TLS ok |
| DNS random label via resolver | NXDOMAIN (forwarded) | unresolvable |
| UDP 8.8.8.8:53 | answered | ENETUNREACH |
| IPv6 | unreachable | unreachable |
| NET_RAW (raw ICMP, AF_PACKET) | allowed | PermissionError |
| NET_ADMIN (SIOCSIFMTU) | denied | denied |

## Design gaps fixed (noted, no §9 decision changed)

- Proxy ignores a connection that sends zero bytes (the isolation check's reachability connect produced a spurious `bad_request` violation on every Run).
- Proxy logs `decision:"error"` for an allowed-but-unreachable upstream; no event (not a policy violation).
- Path names in hygiene findings are scrubbed (a filename containing the token would otherwise reach the event payload).
- `credential_path` literals are checked in diff.json and changed blobs, not in `events.jsonl` payloads (agent output may name its own config dir; only credential bytes there are a leak).
- `UnsafeGitConfig` also appends an `unsafe_git_config` violation event.
- `served_repo_with_tracked_symlink` is a separate fixture in `test_egress_docker.py` (own daemon port 9421) rather than a refactor of `served_repo`, which is unchanged.
- In-container tar streams are read with a strict header class: tarfile's stream mode otherwise treats a garbage header after the first member as a silent end of archive.

## Real `agy` log-only probe (developer-run, 2026-10-09)

Command: `isolation_probe.py --agent antigravity --interactive-auth --egress log-only`. Files: `~/.vaultshield/probes/20261009T083128Z-egress-log-only.json` and `…-proxy.jsonl` (read for names/decisions only; no contents copied). Sign-in and the monitored agent run completed; teardown OK.

### Hosts the proxy saw, and the decision for each

| Host:443 | Count | Decision |
|---|---|---|
| oauth2.googleapis.com | 2 CONNECT + 1 SNI | **allowed** (sign-in / token refresh) |
| daily-cloudcode-pa.googleapis.com | 11 | **added** to the built-in list: the model host this agy build uses. The previous list would have broken agy. |
| cloudcode-pa.googleapis.com | 0 | **kept**: not contacted by this build, but in the binary strings [inv §6] as the prod endpoint of the same API |
| www.googleapis.com | 2 | denied (Cloud Storage JSON upload API: exfil path) |
| storage.googleapis.com | 1 | denied (Cloud Storage: exfil path) |
| play.googleapis.com | 2 | denied (telemetry) |
| antigravity-unleash.goog | 2 | denied (feature flags) |
| antigravity-cli-auto-updater-974169037036.us-central1.run.app | 1 | denied (self-update) |
| lh3.googleusercontent.com | 1 | denied (avatar images) |
| playwright{,-akamai,-verizon}.azureedge.net | 1 each | denied (browser download CDNs) |
| example.com, 1.1.1.1, host.docker.internal:8000 | probe's own | example.com `would_deny`; 1.1.1.1 `ip_literal`; host.docker.internal `private_address` — the last two denied even in log-only |

Mapping the real log through `proxy_lines_to_events` gives the `egress violation (log-only, allowed): CONNECT example.com:443 not in allowlist for antigravity` event (unit test `test_the_real_log_only_probe_log_maps_to_would_deny_events`).

### Reachability from the signed-in agy container (log-only)

host.docker.internal and gateway.docker.internal unresolvable; LAN 192.168.43.1, 10.0.0.1, 172.16.0.1, 169.254.169.254, direct 1.1.1.1 and IPv6 ENETUNREACH; resolver and direct-UDP DNS exfil blocked; raw ICMP, AF_PACKET and SIOCSIFMTU denied (CapEff 00000000a80405fb); no docker.sock, no /host, no /Users; CONNECT 1.1.1.1 and host.docker.internal:8000 → 403. Int-network gateway 172.20.0.1: 5050/27017/27018 refused, 8000 timeout.

### Int-network gateway → Docker VM path (§9 item 10, now measured on macOS)

Full 1-65535 TCP scan of the gateway of a fresh `--internal` network, NET_RAW dropped (the Sandbox's position):
- 65531 ports refused, **2 open: 111 and 63289**, 2 timeout: **5173 and 8000**.
- `docker run --network host alpine netstat -tlnp` (VM namespace, diagnosis only): 111 (rpcbind), 63289 (unknown Docker Desktop VM daemon), 8000 and 5173 (the published ports of the developer's compose stack: backend and frontend) all listen on 0.0.0.0 in the VM.
- So **published host services (backend 8000, frontend 5173) are NOT reachable from the int network**: Docker drops that traffic (timeout, not RST). The earlier 8000 "timeout" in the agy probe was the backend's published port being filtered, not an open path.
- The two VM-internal listeners 111 and 63289 **are reachable** (TCP connect succeeds; an HTTP request is reset). From the default bridge (`egress: off`, today) all four ports are open, so this is not new exposure; the int network removes 8000/5173 and leaves 111/63289.
- ~~Fix attempts, all with the same result: `inhibit_ipv4=true`, `gateway_mode_ipv4=isolated` … Reported as a residual finding.~~ **Superseded by the re-measurement below ("Iteration 1, continued" → 3): `inhibit_ipv4=true` DOES close the path, and it is now applied.** With it the bridge has no IPv4 address, so `.1` is handed to the first container on the network; scanning `.1` from a lone container hits the container itself, which is why the earlier attempt looked unchanged (refused everywhere, or the proxy's ports when the proxy is there). Not measured on Linux.

### Credential files (§9 items 11, 14)

Listing (names, sizes, modes only): token `antigravity-cli/antigravity-oauth-token` 497 B 0600 — confirmed. Other 0600 files: `jetski_state.pbtxt` 518 B, `config/config.json` 82 B, `cache/last_conversations.json`, `cache/onboarding.json`, and empty lock files. `antigravity-cli/cli.log` is a 27-byte symlink. Transcripts under `brain/*/…/transcript*.jsonl` and `conversations/*.db`.

Changes:
- `Antigravity_Adapter.credential_paths()` narrowed from `("/root/.gemini",)` to the token file and `jetski_state.pbtxt`. `config/config.json` and the `cache/*` files are not needle sources. Structure evidence for both files: see "Iteration 1, continued" → 2.
- Path literals still name `/root/.gemini` and `~/.gemini/antigravity-cli` (derived from the file paths), so §9 item 5 is unchanged.
- Symlinks: `snapshot` reads only regular members' bytes inside the Docker archive; a symlink (`cli.log`, or a credential path the agent replaced with a link to `/etc/passwd`) contributes nothing and is never followed (unit test).
- With a whole-root snapshot (the design's fallback), the item-14 rule excludes every brain/transcript, conversations/*.db, cache/*, log/* and `installation_id` file from the real listing (unit test on those names).
- README and the Antigravity docstring now state the confirmed filename (§9 item 11).

## Iteration 1, continued (resumed step: verified the above against the working tree, filled gaps)

Verified on disk: the allowlist, `credential_paths()`, the snapshot's regular-members-only rule and `test_the_real_log_only_probe_log_maps_to_would_deny_events` match the sections above. The design doc had not been updated; it now has §9.1 (host-by-host table, credential decision, gateway, violation event) and a revised §5.6.

### 1. Allowlist

Unchanged: `oauth2.googleapis.com`, `daily-cloudcode-pa.googleapis.com`, `cloudcode-pa.googleapis.com`. `cloudcode-pa` is kept on binary-strings evidence only (never contacted); the orchestrator said not to revert the earlier decision, so it stays and is raised with the developer at the allowlist pause as a least-privilege option. `test_the_antigravity_list_matches_the_probe_decision` now asserts the exact tuple and that each denied host (www/storage/play googleapis, unleash, the auto-updater, lh3, the three playwright CDNs) is absent. README table now gives each listed host's role.

### 2. Credential files: structure inspected

The probe container is gone, so the developer's host copies (same 518-byte `jetski_state.pbtxt`) were mounted read-only into a throwaway `python:3.11.16-slim` container with `--network none`. Only field names and value shapes were printed (string length, an "OAuth/JWT prefix?" boolean); no value was printed.
- `jetski_state.pbtxt`: `post_onboarding.completed_steps` ×4, `installation_uuid`, `migrations{key,value}` ×3. No OAuth/JWT-shaped value: **not credential material**. It stays in `credential_paths()` as a conservative needle source (0600, written by the sign-in, already in the set; the cost is one whole-file needle that agent output does not reproduce).
- `config/config.json` (host copy is the full config): `plugins{5}`, `userSettings{globalPermissionGrants, permissionGrantsV2Migrated, remoteControlHostname, sandboxEnabledAtV2Migration, useAiCredits}`. 0 OAuth/JWT-shaped strings anywhere. **Excluded.**
- The `adapters/antigravity.py` comment and README now carry this evidence.
- New Docker-gated `test_credential_snapshot_never_follows_symlinks_out_of_the_root`: in a real Sandbox, `antigravity-cli/cli.log -> /srv/outside/secret.log` plus a directory symlink out of the root. Real `get_archive` of `/root/.gemini` and of `credential_paths()` yields the token needle and never the outside content, and the same holds when the token file itself is swapped for an escaping symlink. PASSED.

### 3. Int-network gateway: real path, now closed

Full 1-65535 TCP scan (0.3 s timeout, `vaultshield-sandbox:local`):

| Network | Open on `.1` | Timeout |
|---|---|---|
| default bridge (off mode, today) | 111, 5173, 8000, 63289 | — |
| plain `internal: true` (previous int network) | **111, 63289** (Docker VM) | 5173, 8000 |
| `internal: true` + `inhibit_ipv4=true` (lone container) | none (`.1` is the container itself) | — |

With `inhibit_ipv4`: `docker network inspect` IPAM has no `Gateway`; the first container gets `172.x.0.1` (`Gateway: invalid IP`); `172.17.0.1:111` and `192.168.65.x` → ENETUNREACH; a peer container is reachable by name.

Fix: `EgressGuard.start` creates `vaultshield-int-<id>` with `options={"com.docker.network.bridge.inhibit_ipv4": "true"}`. The open network is unchanged. No user-visible change: the Sandbox's only int-network peer was already the proxy.
Tests: the unit test asserts the option on the int network only. The Docker-gated `test_a_blocked_connection_is_blocked_and_recorded` scans every port at `.1` and asserts `.1` is the proxy, open ports == `[443, 3128]` (the proxy's own), `:111` and `:63289` are not open, and the int IPAM has no `Gateway`. Measured: `int gateway 8000/27018/111/63289/<host listener>` → `ConnectionRefusedError`, `int .1 is the proxy: true`. PASSED. Backend :8000 and Mongo :27017/27018 were never reachable through the int gateway.

### 4. Violation event

Confirmed: the probe's proxy log has `would_deny` for `example.com:443` (`not_in_allowlist`). `test_the_real_log_only_probe_log_maps_to_would_deny_events` reads the real file (it ran, not skipped) and asserts `egress violation (log-only, allowed): CONNECT example.com:443 not in allowlist for antigravity`. The probe writes the proxy log next to its JSON; in `run`, `guard.teardown` appends the same events to `events.jsonl` (Docker tests 1 and 5).

### 5. Suites (after all changes)

```
cd cli && .venv/bin/python -m pytest -q -rs --deselect tests/test_sandbox_execution.py::test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever
```
**1 failed, 962 passed, 1 deselected** (102.9 s). The only failure is the pre-existing `test_exec_interactive_really_delivers_stdin_to_a_container_process`. No skips.

```
cd cli && .venv/bin/python -m pytest -v -rs tests/test_egress_docker.py
```
**15 passed** (35.8 s), none skipped: the previous 14 plus the new symlink test.

Collected per file: test_egress 77, test_token_hygiene 23, test_trusted_capture 82, test_egress_docker 15.
Hygiene: `git diff --cached --name-only` is empty. `git diff --quiet backend/security_scan/rules.py` passes. 0 containers and 0 networks labelled `vaultshield.run_id`. Scratch networks and temp scripts removed. The run dir `.vaultshield/runs/20261009-084108-…` was not touched.

### 6. Allowlist real-agy probe (developer-run, run id 20261009-094445-f76062af624e76bd)

Command: `isolation_probe.py --agent antigravity --interactive-auth --egress allowlist`. Proxy log `~/.vaultshield/probes/20261009T094445Z-egress-allowlist-proxy.jsonl` (decisions read only). The results JSON was not written because the probe aborted at sign-in. Teardown completed ("Torn down").

- **Outcome: FAILED at sign-in.** agy printed `Error: Eligibility check failed: Get "https://www.googleapis.com/oauth2/v2/userinfo": Forbidden`. The probe raised `AuthError` (exit 1), and the monitored agent run never started.
- Hosts seen: antigravity-cli-auto-updater-…run.app, antigravity-unleash.goog, daily-cloudcode-pa.googleapis.com, oauth2.googleapis.com, play.googleapis.com, playwright{,-akamai,-verizon}.azureedge.net, www.googleapis.com.
- Allowed: oauth2.googleapis.com ×1, daily-cloudcode-pa.googleapis.com ×5.
- Denied (`not_in_allowlist`): www.googleapis.com ×6, play.googleapis.com ×13, antigravity-unleash.goog ×2, the three playwright CDNs ×1 each, the auto-updater ×1.
- Cause: agy's post-sign-in eligibility check calls `www.googleapis.com/oauth2/v2/userinfo`, and that host is not on the list. A CONNECT/SNI proxy sees only the host, not the path (no TLS interception, per Out of scope). It therefore cannot allow `/oauth2/v2/userinfo` while blocking `/upload/storage/v1/…` on the same host.
- Per design §9 item 9 this is the developer's decision: add `www.googleapis.com` (risk stated) or leave Antigravity unusable in allowlist mode. **Paused for that decision; no code change made.** The after-sign-in reachability rows for allowlist mode are NOT RUN, because the sign-in did not complete.

## Not run

- **Real `agy` probe runs (§3.2)** — workflow agents cannot complete Google OAuth. Log-only run DONE (above). The **allowlist** run is requested from the developer; its 'after' measurements and whether agy works with the 3-host list are NOT RUN yet.
- Backend and frontend suites — not touched.
- Linux host — the int-gateway → host path is measured on Docker Desktop only.
- Disclosed, not closed (§9 item 13): a token rendered into a screenshot is not detected.
