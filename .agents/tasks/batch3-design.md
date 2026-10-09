# Batch 3 — Isolation threat check, per-agent egress allowlist, token hygiene

Requirements + technical design. Design only: no repository source file is changed
by this step, nothing is staged or committed. Pre-existing uncommitted Batch 1/2 work
(patch passthrough, non-UTF-8 gap, interactive-auth stand-in, Run lifecycle/`failed`)
is built on, never reverted. `backend/security_scan/rules.py` is not touched.

Inputs: `.agents/tasks/batch3-investigation.md` (measured facts, cited as **[inv §n]**),
`.agents/tasks/batch3-design-review.md` (first review: findings 1-11, answered in §10; trusted-capture review: findings 1-13, answered in §10.2; round 3 whole-design review: findings 1-11, answered in §10.3; round 4 whole-design review: findings 1-12, answered in §10.4), and the code
cited inline. Facts newly measured for this design are marked **[measured-D]**.

## 1. What the code does today (evidence)

| Question | Answer today | Evidence |
|---|---|---|
| Sandbox container config | One `containers.create(base_image, command, name, working_dir, volumes={.env.sandbox: ro}, ports={port/tcp: None})`. No `network`, `cap_drop`, `sysctls`, `extra_hosts`, `user`. So: default `bridge`, root, Docker default caps **including NET_RAW**, no NET_ADMIN. | `cli/vaultshield/sandbox.py` `Sandbox.create` (~L606-624); [inv §1] |
| Phases inside `Sandbox.create` | image → create+start → `git clone` → `npm install`/`pip install`, all on the same network. | `sandbox.py` `create` body, `_clone_repo`, `_install_dependencies` |
| What the Sandbox can reach | host-published ports via 172.17.0.1 and `host.docker.internal` (OPEN), the developer's LAN router 192.168.1.1:80 (OPEN), the internet (OPEN), arbitrary UDP incl. 8.8.8.8:53, arbitrary-label DNS through 192.168.65.7. IPv6 unreachable. docker.sock absent. No host mount besides the dummy `.env.sandbox`. 169.254.169.254 times out (nothing on a laptop; not blocked). | [inv §2] |
| Can root in the container change networking? | No NET_ADMIN: `SIOCSIFMTU` → EPERM. NET_RAW present: raw ICMP socket succeeds. | [inv §2 "Capabilities"] |
| `internal: true` network | no default route, no external DNS (EAI_AGAIN), 1.1.1.1 ENETUNREACH, a peer on the same network reachable by name, **port publishing silently dropped**. | [inv §2] |
| Network switch on a running container | `network connect <int>` then `network disconnect <open>` on a running container works. Afterwards only the int subnet is in `/proc/net/route`, `example.com` and `host.docker.internal` fail to resolve, 1.1.1.1:443 is ENETUNREACH. With `--cap-drop NET_RAW`, `AF_INET/SOCK_RAW` and `AF_PACKET` sockets raise `PermissionError`. `--sysctl net.ipv4.ip_forward=0` is honoured. | **[measured-D]** throwaway `vs-b3-*` networks/container, removed |
| `ip_forward` inside a container | `1` by default in a container netns on this Docker Desktop (29.2.1). A dual-homed container would therefore route packets for a neighbour that can craft them. | **[measured-D]** `/proc/sys/net/ipv4/ip_forward` in a container on an internal network |
| Teardown | `Sandbox.teardown()` = `container.remove(v=True, force=True)`, re-raises non-NotFound errors. `_cleanup_after_failure` (sandbox.py L1141) swallows. Inner `finally` in `run` calls `sandbox.teardown()` (cli.py L890); the outer `except BaseException` calls `_mark_run_failed_best_effort` (L907). `teardown.py` `teardown_sandbox` looks the container up by `container_name(run_id)`. Nothing else is created per Run today. | `sandbox.py` L908-938, L1141; `cli.py` L880-909; `teardown.py` L349 |
| Upload on a failed Run | None. `_upload_run_artifacts` is only on the success path (cli.py L899), so the `violation` events of a failed Run (e.g. `OffLimitsViolation`) stay on disk only. | `cli.py` L895-909; [inv §5] |
| `violation` events | `EventLogRecord(event_type, timestamp, payload)`; `sandbox_monitor._collect_file_changes` emits `off_limits_paths violation: …`. Backend `RunEvent.event_type` is `CharField(max_length=32)` without choices; a `violation` event already round-trips (`backend/runs/tests.py` `test_event_type_is_not_restricted_to_a_choice_set`). `TimelineTab` renders every event generically and sorts with `sortEventsChronologically`. **No backend or frontend change is needed.** | `monitor.py` L442, L1193; `sandbox_monitor.py` L466; `backend/runs/models.py` L135; `frontend/src/RunDetailView.jsx` L278, L610 |
| Event_Log writer | `monitor._append_event_log(path, records)` is append-only; `_timestamp_sort_key` sorts by parsed ISO-8601. | `monitor.py` L1193-1281 |
| git capture | bare `["git","checkout","-b",branch]`, `["git","add","--all"]`, `git -c user.* commit --allow-empty --no-verify`, `git diff --numstat -z --no-renames`, per-file `git --literal-pathspecs diff … --no-ext-diff --no-textconv -U0`. No `--git-dir`/`--work-tree`, no env, no hook/fsmonitor overrides. `Sandbox.exec(cmd)` takes no env. | `git_capture.py` L530-580, L630-690; `sandbox.py` L637 |
| `core.worktree` vector | Real: with `core.worktree=<parent>` in `.git/config`, a bare `git add --all` run inside the repo staged `../outside/secret/tok`. The same add as `GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git --git-dir=.git --work-tree=. -c core.hooksPath=/dev/null -c core.fsmonitor=false add --all` staged only the in-repo file. Measured with host git 2.52. Under trusted capture (D9) the helper initialises a fresh repo and never reads the Sandbox's `.git/config` (only the check-2 copy via `--file`), so the Sandbox's `core.worktree` is discarded by construction; the `_git` prefix is verified on host git by `test_git_capture.py` (a) and is defence in depth inside the helper. §8.2 test 6 (2) asserts the Sandbox's `core.worktree` has no effect on trusted capture (review round 4 finding 5). | **[measured-D]** temp dirs |
| Does the capture branch leave the container? | No. Nothing calls `get_archive`, `git fetch` or `bundle` on the Sandbox; the branch dies with the container. `submit` pushes `vaultshield/run-<run_id>` from the host `repo_root`. What leaves the machine is `diff.json`, `events.jsonl` and screenshots. | `grep get_archive\|bundle\|git fetch cli/vaultshield` (only `put_archive` in `write_file`); `submit.py` L136 |
| Antigravity credential location | `ANTIGRAVITY_TOKEN_PATH = "/root/.gemini/antigravity-cli/antigravity-oauth-token"` (antigravity.py L308), but the docstring (L133) and `cli/README.md` L72 say `jetski_state.pbtxt`. The binary contains `jetski_state.pbtxt` and not the literal `antigravity-oauth-token`. Filename is **unknown** until a real sign-in; `/root/.gemini/` is certain and outside `/workspace/repo`. | [inv §3] |
| Agent output | The agent's terminal output becomes `output` events in `events.jsonl` and is uploaded. A `cat` of the token to stdout would reach the dashboard. This is a 4th leak vector not listed in the investigation. | `sandbox_monitor._stream_agent`, `uploader._upload_events` |
| Screenshots (5th leak vector, **not closed**) | Step 7 runs agent-written app code as root with read access to `/root/.gemini`, and on a clean scan the PNG screenshots are uploaded. A page that renders the credential file leaks it to the dashboard. The proxy blocks direct exfil in allowlist mode, but not this upload. Images are not scanned. Mitigations (OCR, or running step 7 with the credential root removed) are **out of scope** for this batch; disclosed in §9 item 13 and repeated in the final report. | `browser_capture.start_and_capture`, `uploader.upload_run` (screenshots); review round 3 finding 7 |
| Config | `load_config` reads known keys only and ignores unknown top-level keys; `ProjectConfig` has no egress field. | `config.py` L138-196 |
| Agent hosts | Antigravity: candidates from binary strings, core `oauth2.googleapis.com` + `cloudcode-pa.googleapis.com`; runtime use unconfirmed. Codex/Kiro: no host in adapter code. | [inv §6]; `adapters/codex.py`, `adapters/kiro.py` |
| Dockerfile comment | Says curl is "kept out of the final layer", but `/usr/bin/curl` is in the image. Relevant to the threat report only; not changed (no unrelated edits). | `cli/sandbox-image/Dockerfile`; [inv §1] |
| Isolation probe | `cli/isolation_probe.py`: 10 file/socket probes, **no network probes**, no `--interactive-auth`, no egress. | `cli/isolation_probe.py` |

## 2. Requirements

### Functional

- **FR1 Threat check (user item 1).** A network probe (`cli/isolation_probe_net.py`, stdlib only) runs inside the Sandbox via Docker exec. `cli/isolation_probe.py` gains `--agent`, `--interactive-auth`, `--egress` and `--lan-gateway`, so the developer runs one command per mode that signs real `agy` in and then probes that signed-in container. The before/after reachability matrix covers: host files, docker.sock, 172.17.0.1 and the int-network gateway, `host.docker.internal`, the LAN gateway, 10/8, 172.16/12, 192.168/16, 169.254.169.254, the internet, DNS exfil (resolver and direct UDP/53), IPv6, and capabilities.
- **FR2 Egress enforcement outside the agent (user item 2).** In `allowlist` and `log-only` modes, after clone + dependency install the Sandbox is attached only to a per-Run `internal: true` network. Its only peer there is a per-Run filtering proxy container, which is also on a per-Run egress network. The Sandbox runs with `cap_drop=["NET_RAW"]` and IPv6 disabled. The proxy runs with `ip_forward=0`. The proxy allows a connection only if the CONNECT host / TLS SNI is on the agent's allowlist, the port matches, and the resolved address is public.
- **FR3 Config switch (user item 3).** `.vaultshield.yml` `egress: {mode, allow}`, CLI override `run --egress off|log-only|allowlist`, default `off`. Built-in per-agent allowlists: antigravity enforced; codex and kiro defined and marked unverified.
- **FR4 Violations in the timeline (user item 3).** Every proxy denial becomes one `violation` event in `events.jsonl`, in the existing 3-field schema, written during teardown. Successful Runs upload it with the normal upload. Failed Runs upload it best-effort before the failed PATCH.
- **FR5 Token hygiene (user item 4).** In every egress mode:
  - git capture runs with a fixed safe git environment and command prefix, so agent-written git config cannot widen the work tree, run hooks/fsmonitor/filters, or read global/system config.
  - Symlinks the Run adds or retargets to a path outside the repo fail the Run. Escaping symlinks the base repo already tracks, left unchanged, do not (§5.5 (d), §9 item 12).
  - **Trusted capture (user-approved addition):** the run branch and `diff.json` are produced by git in a fresh capture helper container, from the pre-agent base commit and a safely filtered Docker-API copy of the work tree. The agent's `.git` and `git` binary are never used (D9, §5.4.1).
  - Before anything is uploaded, `diff.json`, `events.jsonl` and the changed files of the capture branch are scanned on the host for the credential bytes (snapshotted from the container, held in memory, never logged) and for credential-path literals.
  - On a hit: the Run fails, a `violation` event is recorded, and neither the diff nor the screenshots are uploaded.
- **FR6 Teardown.** The proxy container and both networks are removed on every path: success, every failure, Ctrl-C, `create()` failure, and `vaultshield teardown <run_id>`. This happens after the Sandbox removal and before the best-effort events upload and failed PATCH.
- **FR7 Tests (user item 5).** Unit tests cover allowlist parsing/matching, address classification, proxy decisions, log→event mapping, guard call order and token hygiene. Docker-gated integration tests show: a blocked connection is blocked and recorded, an allowlisted one is reachable, DNS exfil is blocked, host ports are unreachable, NET_RAW is gone, the published port still works via the proxy, and the three git vectors are closed.

### Non-functional

- **NFR1 Backward compatibility.** With `egress: off` (the default), every Docker call `run` makes is identical to today, except the always-on trusted-capture and token-hygiene calls (§5.4.1, §5.5), matching acceptance criterion 1. These are: the capture helper's `containers.create`/`start`/`put_archive`/`exec_run`/`remove`; on the Sandbox, `get_archive` at 5-base (HEAD, ref, `packed-refs`, `.git/config`, `.git/objects`), at credential snapshots #1, #2a and #2 (only when `credential_paths()` is non-empty), and of the work tree at step 8; and `container.kill()` at step 8 step 0. Otherwise: same Sandbox `containers.create` kwargs, no networks, no proxy. The only always-on changes are FR5 (git hardening + scan) and the failure-path events upload. Both are listed for sign-off (§9).
- **NFR2 Never mask the original error.** Every new teardown and upload step is best-effort. Each one writes at most one `Warning:` line on stderr and is never re-raised.
- **NFR3 Bounded.** Each proxy connection has a 5 s handshake/connect timeout and a 300 s idle timeout. The proxy must be ready within 10 s. The post-switch assertion uses 2 s connects. The failure-path events upload uses `httpx.Timeout(5.0, connect=2.0)`, as `mark_run_failed` does.
- **NFR4 Pinned proxy runtime.** `python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e` (multi-arch index digest; verified to exist with `docker buildx imagetools inspect python:3.11.16-slim` **[measured-D]**). The proxy is in-repo stdlib code, so there is no third-party proxy package to pin.
- **NFR5 Secrets.** Credential bytes are never written to disk by VaultShield, never logged, and never put in an exception message, event payload or warning. They live only in a host-memory `SecretSet` for the Run's lifetime.
- **NFR6 Style.** Requirement-tagged docstrings, citing `(Batch 3 FRn)` next to the nearest existing Requirement. No unrelated refactors. Everything stays uncommitted. `rules.py` is untouched.

### Acceptance criteria

1. `run` with no `egress` key and no `--egress` makes exactly today's Sandbox `containers.create` call and no `networks.create` call (unit test with a fake client). The only other container is the always-on capture helper (§5.4.1, §9 item 12).
2. `egress.mode: allowlist` (or `--egress=allowlist`) with `--agent=antigravity`:
   - Before `Sandbox.create`, two networks exist (`vaultshield-open-<id>`, `vaultshield-int-<id>` with `internal=True`, both `enable_ipv6=False`) and a proxy `vaultshield-egress-<id>` is ready.
   - After `create` returns and before `_authenticate_in_sandbox`, the Sandbox is on the int network only.
   - The post-switch check passed.
3. Docker-gated, from the isolated Sandbox:
   - These targets are not reachable: direct 1.1.1.1:443, UDP 8.8.8.8:53, a random-label `getaddrinfo`, `host.docker.internal:8000`, the int gateway on 8000/27018, and `[2001:4860:4860::8888]:443`.
   - CONNECT via the proxy to a non-allowlisted name, an IP literal, or `host.docker.internal:8000` → `403`.
   - CONNECT to an allowlisted name → `200` and a completed TLS handshake (skipped without internet).
   - `socket(AF_INET, SOCK_RAW, IPPROTO_ICMP)` raises `PermissionError`.
4. After teardown, `events.jsonl` contains, for each denial, one line `{"event_type":"violation","timestamp":<ISO-8601 UTC>,"payload":"egress violation: CONNECT <host>:<port> denied (<reason>; agent <agent>, egress allowlist)"}`. No container or network labelled with the run id remains.
5. A host HTTP GET to `resolve_base_url(sandbox, port, published_by=guard.port_publisher)` returns the app's 200 while the Sandbox is internal-only.
6. Token hygiene:
   - A token copied into a repo file, a symlink to `/root/.gemini/...`, or the token printed to stdout each fail the Run with a `violation` event. In each case `upload_run` is not called, events are uploaded with the token replaced by `***`, one failed PATCH is sent, and the token bytes appear in no message, event or warning.
   - `core.worktree=/root` set by the "agent" stages nothing outside `/workspace/repo`.
   - A filter driver in repo config fails capture before `git add` runs it.
   - A token copied into a `.gitignore`d file is not committed, not in `diff.json`, and does not fail the Run.
  - A replaced in-container `git` binary, `.git/info/exclude`, `assume-unchanged`, `refs/replace` or alternates cannot hide a file from the branch or `diff.json`. The Sandbox's `git` is never executed during capture (§8.2 test 7).
7. A failing proxy/network removal, or a failing events upload, leaves the original error's text and exit code unchanged and adds at most one warning line each.
8. CLI suite: 728 baseline + new tests pass. The only failure is the pre-existing `test_exec_interactive_really_delivers_stdin_to_a_container_process`. The hanging sign-in test stays deselected. `git diff --quiet backend/security_scan/rules.py` passes.

### Out of scope

- Enforcing egress for Codex/Kiro. Their lists are defined but unverified, and their env-carried API keys are not covered by the content scan.
- Detecting a credential rendered into a screenshot (no OCR), or running step 7 without the credential root present (§1 screenshots row, §9 item 13).
- Detecting deliberately encoded exfil (base64, split strings). Content scanning catches verbatim copies only. Encoded leaks are what the egress policy and human review are for.
- Fixing the docker-py `poll()` hang (`test_a_hanging_signin…`) or the pre-existing `send_line` redaction test failure.
- Making egress the default. Allowing the agent to install new dependencies in allowlist mode. A host-side L7 proxy with TLS interception.

## 3. Threat check (user item 1)

### 3.1 Reachability before and after

The "Before" column is measured [inv §2] on a replica of today's container config. The network facts do not depend on which process runs, since `agy` runs as root in the same netns via `exec_stream`. The "After (allowlist)" column is the design target. It is asserted by the Docker-gated test (§8) and re-measured by the developer's real-`agy` probe run (§3.2). Neither has run yet, so the final report must quote the measured values, not this table.

| Target | Before (`egress: off`, today) | After (`allowlist`) | Closed by |
|---|---|---|---|
| Host files (host HOME, host `.env`) | not mounted, unreadable | unchanged | no bind mounts besides dummy `.env.sandbox` |
| docker.sock | absent | absent | not mounted |
| 172.17.0.1 (default bridge gw) :5050/:27017 | OPEN | not attached to docker0 → unreachable | topology |
| int-network gateway :8000/:27018 | n/a | must not be OPEN (asserted) | internal network isolation |
| `host.docker.internal` :8000/:5050/:27017/:27018 | OPEN | name unresolvable; via proxy → 403 `not_in_allowlist`; allowlisted-but-private → 403 `private_address` | topology + proxy rules (D5) |
| LAN gateway 192.168.1.1:80 | OPEN | ENETUNREACH; via proxy: IP literal → 403 | topology + proxy |
| 10.0.0.1, 172.16.0.1 :80 | routed (timeout, nothing there) | ENETUNREACH | topology |
| 169.254.169.254:80 | routed (timeout on a laptop; would answer on a cloud VM) | ENETUNREACH; via proxy → 403 | topology + proxy |
| Internet, e.g. `example.com:443`, `1.1.1.1:443` | OPEN | direct ENETUNREACH; via proxy → 403 unless allowlisted | topology + allowlist |
| Allowlisted Google host via proxy | OPEN (directly) | 200 CONNECT, TLS end-to-end | allowlist |
| DNS: random label via configured resolver | forwarded upstream (NXDOMAIN) → exfil possible | EAI_AGAIN (no external resolver on internal net); proxy never resolves a non-allowlisted name | topology + D5 order |
| DNS: direct UDP/53 to 8.8.8.8 | answered | ENETUNREACH | topology |
| IPv6 `[2001:4860:4860::8888]:443` | ENETUNREACH | ENETUNREACH; `enable_ipv6=False` on both networks; `disable_ipv6=1` sysctl | config |
| NET_RAW (raw ICMP / AF_PACKET) | allowed | `PermissionError` | `cap_drop=["NET_RAW"]` |
| NET_ADMIN (`SIOCSIFMTU`) | EPERM | EPERM | Docker default |
| Other containers (ICC) | docker0 peers reachable | only the proxy | per-Run networks |

### 3.2 Real-`agy` probe the developer runs

Workflow agents cannot complete Google OAuth, so the developer runs these commands, once per mode, from `cli/`:

```
.venv/bin/python isolation_probe.py --agent antigravity --interactive-auth --egress off
.venv/bin/python isolation_probe.py --agent antigravity --interactive-auth --egress allowlist
# optional, to learn which hosts agy really uses (feeds D7's list):
.venv/bin/python isolation_probe.py --agent antigravity --interactive-auth --egress log-only
```

Each command does this:
- Base image `vaultshield-sandbox:local`, default repo `https://github.com/octocat/Hello-World.git`. A temporary dummy `.env.sandbox` is created, so no file needs to exist first.
- It builds the Sandbox through the same `EgressGuard` + `Sandbox.create` + `guard.isolate` path that `run` uses.
- It runs `cli._authenticate_in_sandbox(sandbox, Antigravity_Adapter(), "antigravity")`. The developer opens the URL and pastes the code. The sign-in prompt "Reply with exactly the word: ready" also exercises the model host.
- It copies `isolation_probe_net.py` in with `sandbox.write_file("/tmp/vs_probe_net.py", …)`.
- It runs the probe through Docker exec (`container.exec_run(["python3", "/tmp/vs_probe_net.py", "--json", "--lan-gateway", <gw>], environment=sandbox.exec_env_defaults)`) inside the signed-in container. The agent is not prompted to run anything. The script also runs the existing file probes.
- It prints the manual equivalent: `docker exec vaultshield-run-<id> python3 /tmp/vs_probe_net.py`.
- It tears everything down in `finally`, in the §7 order.

No flag is added to `vaultshield run`. The probe script owns the pause between sign-in and probing, so production behaviour does not change.

`--lan-gateway` defaults to the host's default route, read on the host (`route -n get default` on macOS, `ip route show default` on Linux), and can be passed explicitly. Without one, the LAN probe is skipped and reported as `skipped`.

**Output.** A table on stdout, plus files under `~/.vaultshield/probes/` (outside any repo, so never committable):
- `<UTC-ts>-egress-<mode>.json`: one record per probe, `{probe, target, result: open|refused|timeout|unreachable|denied|resolved|unresolvable|error|skipped, detail, ms}`.
- In non-off modes, `<UTC-ts>-egress-<mode>-proxy.jsonl`: the proxy's decision log, i.e. the hosts `agy` contacted during sign-in.
- The `/root/.gemini` listing (relative names, sizes, modes only). This settles the token filename [inv §3].

**Probes in `isolation_probe_net.py`.** All targets and ports are fixed in the script:
- TCP connect: default-route gateway (read from `/proc/net/route`) and `host.docker.internal` on 5050, 8000, 27017, 27018.
- Resolve `gateway.docker.internal`.
- TCP connect to `--lan-gateway`:80, 10.0.0.1:80, 172.16.0.1:80, 169.254.169.254:80.
- `example.com:443`: TCP connect, TLS handshake, `HEAD /`. Also TCP connect to `1.1.1.1:443`.
- Only if `HTTPS_PROXY` is set, CONNECT through the proxy to: `example.com:443` (expect 403), `oauth2.googleapis.com:443` (expect 200, then TLS handshake + `HEAD /`), `1.1.1.1:443` (403), and `host.docker.internal:8000` (403).
- SNI path: TLS to `oauth2.googleapis.com:443` by name, which `extra_hosts` maps to the proxy.
- DNS: `getaddrinfo("vs-probe-<16 random hex>.example.com")`, and one UDP/53 query for that same name to 8.8.8.8.
- IPv6: TCP connect to `[2001:4860:4860::8888]:443`.
- Capabilities: decode `CapEff` from `/proc/self/status`, then try a raw ICMP socket, an `AF_PACKET` socket and `ioctl(SIOCSIFMTU)`.
- docker.sock presence and the host-file checks.
- `/root/.gemini` walk with `os.lstat` only.

**Probe safety rules** (enforced by the script, stated in its module docstring):
- Payloads are sentinels only. The only bytes ever sent are a TLS ClientHello, `HEAD / HTTP/1.1` with `Host` and no other headers, and a DNS query for the random sentinel name. Nothing is read from the repo, the env, or `/root/.gemini` and sent anywhere.
- `/root/.gemini` files are never opened, only `lstat`ed. Contents and env values are never printed. The only env value printed is `HTTPS_PROXY`.
- Timeouts are 2 s for TCP connect and 5 s for TLS/HEAD. Each target gets one attempt and no retries.
- LAN and private targets get a TCP connect only, with no application bytes.
- The script writes only to `/tmp` in the container. The host side writes only `~/.vaultshield/probes/`.

**Known hang.** `exec_interactive` relies on docker-py `socket.read`, which calls `poll()` with no timeout. A sign-in that goes silent can therefore block past `ANTIGRAVITY_AUTH_TIMEOUT_S` [inv §8]. Ctrl-C works, and the `finally` still removes the Sandbox, proxy and networks. If the process is killed hard, the script has already printed its cleanup line: `docker rm -f vaultshield-run-<id> vaultshield-egress-<id>; docker network rm vaultshield-int-<id> vaultshield-open-<id>`.

## 4. Decisions

**D1 — Mechanism: per-Run Docker topology plus an in-repo filtering proxy sidecar. No iptables, no host packet filter.**

On macOS the containers run in the Docker Desktop VM. Host `pf` never sees their traffic, and the CLI cannot program the VM's iptables without a privileged `--net=host` helper, which would be a new and larger trust surface [inv §7]. Docker network topology is created through the daemon API and works the same on macOS and Linux.

Rejected options:
- iptables applied by the CLI: needs root or a privileged helper on the host, is Linux-only, and is fragile against Docker's own chains.
- `HTTPS_PROXY` alone: not enforcement. Any process can ignore it.
- A DNS-only allowlist: IP literals bypass it.

**D2 — Why a root process inside the Sandbox cannot undo it.** Enforcement is topological and lives outside the agent's network namespace:
1. After the switch, the Sandbox's netns has one interface, on the int network, and no default route (measured-D: `/proc/net/route` holds only the int subnet). Routes, interfaces, iptables/nft and sysctls in that netns need `CAP_NET_ADMIN`, which Docker does not grant (measured `SIOCSIFMTU` → EPERM). That stays true if root `apt install`s `iproute2`/`iptables`, and after the switch apt has no network anyway.
2. Attaching or detaching networks and changing capabilities are Docker API calls. There is no docker.sock in the container [inv §2], and Docker reads the container's capability set only at create.
3. The only other host on the int segment is the proxy. Without `NET_RAW` (dropped, measured-D `PermissionError` for `SOCK_RAW` and `AF_PACKET`), the Sandbox cannot craft frames that use the proxy as a router. The proxy is also created with `sysctls={"net.ipv4.ip_forward": "0", "net.ipv6.conf.all.forwarding": "0"}` and refuses to start if either reads non-zero. A container defaults to `ip_forward=1` (measured-D), so this sysctl is load-bearing.
4. The proxy is a separate container, a separate process and filesystem, and holds no secrets. The Sandbox can only talk to its listening sockets, and every request on those sockets goes through the allowlist check.
5. `HTTPS_PROXY`/`HTTP_PROXY` and the `/etc/hosts` entries are conveniences that point clients at the proxy. If root edits or removes them, the client just fails to connect, because no other path out exists.

**D3 — Phase separation: open network for create/clone/deps, then a host-side switch before sign-in.** Clone and dependency install need open network and run before any token exists. The switch is `int_net.connect(container)` first, then `open_net.disconnect(container)` (measured-D on a running container). It runs right after `sandbox.create(...)` returns and before `_authenticate_in_sandbox`. No exec is in flight at that point, which removes the [unknown] "switch during `exec_stream`". Details and the post-switch assertion are in §5.1. Justification for running `postinstall` scripts with open network: no agent token exists yet (the token appears only at 5b), and no agent has run. **Residual risk:** a hostile dependency of the target repo can still read the repo and phone home during install, or leave a background process or a modified binary behind. After the switch that process has no network, and anything it writes into the repo goes through the FR5 scan. This is listed in §9.

**D4 — Browser capture: the proxy publishes the app port and forwards it (review finding 2, option a).** The Sandbox stays internal-only through step 7, so agent-written app code never runs with open network. In non-off modes the Sandbox is created with `publish_port=None`. The proxy is created on the open network with `ports={f"{config.port}/tcp": None}` and runs a plain TCP forwarder from `0.0.0.0:<port>` to `vaultshield-run-<run_id>:<port>` over the int network. `resolve_base_url(sandbox, port, *, published_by=None)` reads the host port off `published_by` when given. `start_and_capture` gains `published_by=None` and passes it through. In off mode both are `None` and behaviour is today's. The app must listen on `0.0.0.0`, which today's published-port path already requires.

**D5 — Proxy match and address rules.** These are pure functions in `egress_proxy.py`, unit-tested on the host. Decision order, which is the DNS-exfil fix:
1. Parse the target and normalise it: lowercase, strip one trailing dot, IDNA-encode or reject.
2. IP-literal target → deny `ip_literal`, unless that exact global IP is listed.
3. Allowlist match (host + port) → otherwise deny `not_in_allowlist`. `log-only` records `would_deny` and continues. **No DNS lookup happens before this step**, so a non-allowlisted random label never reaches any resolver.
4. Resolve the name, with a 5 s timeout in a worker thread. If **any** resolved address is not public, deny `private_address`. Not public means `not ipaddress.ip_address(a).is_global` (which covers 10/8, 172.16/12, 192.168/16 including Docker Desktop's 192.168.65/24, 169.254/16, 127/8, 100.64/10, `::1`, `fc00::/7`, `fe80::/10`), with IPv4-mapped IPv6 unwrapped first. This closes DNS rebinding and `host.docker.internal`. Then connect to the first public address, preferring IPv4, with a 5 s timeout.
5. Port: entries default to 443. A port mismatch → deny `port_not_allowed`.

Ingress forms:
- `CONNECT host:port` on 3128. Any other method or an absolute-URI request → deny `plain_http`. Malformed requests, or headers over 8 KiB → `bad_request`.
- A TLS ClientHello on 443, matched by SNI. No SNI → `no_sni`.

Rules 2, 4 and the ingress rules apply in `log-only` too. Only rule 3 is relaxed there.

**D6 — Proxy implementation: in-repo stdlib Python, pinned runtime image by digest.** `cli/vaultshield/egress_proxy.py` is a self-contained stdlib file (threads, `socket`, `select`). It reaches the proxy container through a **read-only bind mount** of a per-Run host staging directory, not `put_archive`: the Docker daemon refuses archive extraction into a `read_only=True` rootfs (`ErrRootFSReadOnly`) unless the destination is a volume, and that applies before `start()` too (review 2 finding 1). `guard.start()` does `stage = Path(tempfile.mkdtemp(prefix=f"vaultshield-egress-{run_id}-"))`, writes `stage/"egress_proxy.py"` (the module's own source bytes) and `stage/"egress.json"`, and passes `volumes={str(stage): {"bind": "/opt/vaultshield", "mode": "ro"}}` to `containers.create`. Docker Desktop shares `/var/folders` and `/tmp` by default, and the Sandbox already relies on a bind mount for `.env.sandbox`. The staging dir holds no secrets and is removed by `guard.teardown` (D10 step 5). Its pure functions are imported by `egress.py`. It runs in `python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e`: an official, maintained image, the same Python minor as the CLI, multi-arch, with the digest verified to exist.

The proxy only tunnels. It never terminates TLS, so it never sees plaintext and needs no CA in the Sandbox. Every decision is one JSON line on stdout: `{"ts","kind":"connect"|"sni","host","raw_host","port","decision":"allow"|"deny"|"would_deny","reason"}`. `host` is the D5-normalised name used for matching; `raw_host` is the host exactly as the client sent it, before normalisation (review round 4 finding 9, §5.3). The first line is `{"kind":"ready"}`.

Rejected alternatives:
- `ubuntu/squid` by digest: a config language that needs a custom ACL for the resolved-private-address rule, log-format mapping, a larger image, and no host-side unit tests of the match rule.
- tinyproxy: regex filtering, no SNI path.

Missing image: `images.get(ref)`, else `images.pull("python", tag="3.11.16-slim@sha256:…")`. A pull failure raises `EgressError("egress: cannot pull the egress proxy image <ref>: <err>. Run `docker pull <ref>` while online, or use --egress=off.")`.

**D7 — Config switch, default, grammar and per-agent lists.**

```yaml
egress:
  mode: allowlist          # off | log-only | allowlist    (default: off)
  allow:                   # optional; UNIONED with the agent's built-in list
    - api.example.com
    - "*.example.org:8443"
```

`run --egress off|log-only|allowlist` overrides `mode`. Precedence: CLI flag, then config, then `off`.

**Default `off`.** Existing projects then keep today's behaviour with no proxy image pull. The mock-agent Docker tests are unaffected. The Antigravity list is not runtime-confirmed yet, and an unconfirmed list would break real Runs. Switching the default for antigravity once the log-only run has confirmed the list is a §9 sign-off item.

Validation (`ConfigError`, then `ClickException` at step 1, before anything exists on disk):
- `egress` must be a mapping with keys ⊆ {`mode`, `allow`}. Unknown keys raise. YAML `off` without quotes parses as `False` and is accepted as `"off"`. `True` and any other mode raise.
- An `allow` entry is `host[:port]`, after lowercasing and stripping one trailing dot. `host` is one of:
  - an LDH DNS name with at least 2 labels, labels of 1-63 characters, ≤ 253 characters in total;
  - `*.` followed by such a name, where the suffix has at least 2 labels (`*.com` and a bare `*` are rejected, and the wildcard may only be the leftmost label);
  - a **global** IPv4 literal, or a bracketed global IPv6 literal.
- Rejected outright: a scheme (`://`), a path, userinfo, whitespace, a port outside 1-65535, and a private/reserved IP literal (it could never pass D5 anyway).
- Matching:
  - `*.example.com` matches `a.example.com` and `a.b.example.com`, but not `example.com` and not `badexample.com`.
  - An IP target matches only an equal IP entry.
- `allowlist` or `log-only` with an empty effective list raises `ClickException`. The exception is `mock`, whose built-in policy is an explicit deny-all, because it needs no network.

Built-in lists (`egress.BUILTIN_ALLOWLISTS`):

| Agent | Entries | Status |
|---|---|---|
| antigravity | `oauth2.googleapis.com`, `cloudcode-pa.googleapis.com` | core, from binary strings [inv §6]; **confirm with the log-only probe run** |
| codex | `api.openai.com` | unverified (API-key mode) |
| kiro | `runtime.us-east-1.kiro.dev`, `management.us-east-1.kiro.dev` | unverified. Source is AWS's endpoint-change notice moving Kiro to `*.kiro.dev`, as reposted in a [kiro-gateway issue](https://github.com/jwadow/kiro-gateway/issues/146); the official [Kiro firewall page](https://kiro.dev/docs/privacy-and-security/firewalls/) did not render for fetching |
| mock | *(empty: deny-all)* | verified (mock makes no requests) |

`www.googleapis.com` is deliberately **not** built in. It serves the Cloud Storage JSON upload API, so an agent could exfiltrate to a world-writable bucket through it. It is added only if the log-only run shows `agy` cannot work without it (§9).

**D8 — Violations: denials do not fail the Run; the failure path uploads the Event_Log best-effort (finding 8: both a and b).** A denial is evidence, not a crash. Telemetry hosts may be denied on every Antigravity Run, and failing Runs on that basis would make the policy unusable.

The failed-Run gap is real and also hides `OffLimitsViolation` and token-hygiene violations. So in the outer `except BaseException`, before `_mark_run_failed_best_effort`, the CLI calls `_upload_events_best_effort`. It POSTs `events.jsonl` only, with each `SecretSet` needle replaced by `***`, and never raises.

The proxy log is read during teardown. Each `deny` line becomes `EventLogRecord("violation", line.ts, payload)`. Each `would_deny` line becomes a violation with the log-only payload (§5.3). `allow` lines are not events. Records are sorted with `monitor._timestamp_sort_key` and appended with `monitor._append_event_log`, the same writer `sandbox_monitor` uses. The timeline sorts by timestamp anyway.

**D9 — Token hygiene: harden capture, scan what leaves the machine on the host, fail closed.**
- Git capture runs with a fixed environment and prefix (§5.4).
- Credential material is snapshotted from the container with `container.get_archive`. That is a Docker API call, so no in-container binary is involved and the agent cannot fake the read. Snapshots are taken at three points, all through the Docker API (review round 3 finding 3, one rule everywhere):
  - **#1** at 5c, after sign-in (or right after 5-base when there is no sign-in), which is before the agent can alter the file;
  - **#2a** right after step 6 (agent finished), which catches a token the agent refreshed and the app might later delete;
  - **#2** at the start of step 8, right after the Sandbox is killed (§5.4.1 step 0) and before `get_archive(REPO_DIR)`. This is the authoritative post-run snapshot: step 7 runs agent-written app code as root with read access to `/root/.gemini` and proxy access to `oauth2.googleapis.com`, so the app could refresh the token and write the new access token into the credential file and the repo. With the Sandbox killed, no process can change either between snapshot #2 and the work-tree archive.
  - **Needle sourcing (review round 4 finding 1).** #1 fixes the **credential file set** (the regular files under the root at sign-in). #2a and #2 re-read only those paths, plus new files with a credential-like basename outside `tmp`/`logs`/`history`/`brain`/`conversations`/`cache` directories. Agent session data written during the Run never becomes a needle. Whole-file needles come only from files ≤ 8 KiB. Rule and limits in §5.5; sign-off in §9 item 14.
- On a hit, the Run **fails**: `violation` event, no `upload_run`, `diff.json` renamed to `diff.quarantined.json` so a later manual retry cannot upload it, events uploaded scrubbed, failed PATCH. The alternative (redact the entry, mark it `patch_truncated: true`, continue) is listed in §9.
- The guard covers the whole credential root `/root/.gemini`, not one filename. `Antigravity_Adapter.credential_paths()` returns `("/root/.gemini",)`. The base default is `()`.
- **Trusted capture (user-approved addition, closes the former residual risk "capture still runs the container's own `git`, which root could replace").** The committed run branch and `diff.json` are produced by a git the agent cannot tamper with: a fresh **capture helper container** (option b), detailed in §5.4.1. The agent's container is used only as a data source, read through the Docker API (`container.get_archive`), so no in-container binary runs during capture. The container's `.git` is untrusted: its config, hooks, refs, `info/*`, alternates and index are never used. The branch is rebuilt from the trusted pre-agent base commit id, recorded before any agent code runs.

  **Why option (b), a helper container, and not (a), host git.** Evidence from the code:
  - The host filesystem cannot represent the work tree faithfully on the platform this design was measured on. APFS refuses non-UTF-8 names with `EILSEQ`; the repo's own test helper documents and works around this (`cli/tests/test_git_capture.py` `_commit_non_utf8_file`, ~L869-895). Batch 1's non-UTF-8 → `patch_truncated: true` behaviour would break under (a). APFS is also case-insensitive by default, so `A.txt` and `a.txt` from a Linux work tree would collide on extraction and one would be silently dropped. That breaks the "never silently drop files" rule from Batch 1. A Linux container's filesystem is case-sensitive and stores names as bytes, so (b) avoids both problems.
  - Under (b), git capture code stays as it is. `commit_run_branch`, `resolve_diff_base` and `build_structured_diff` (`git_capture.py` L530-580, L582-695, L696-743) take any object with `.exec(cmd)` → `ExecResult` that runs in the repo directory. The existing `LocalGitSandbox` stand-in (`cli/tests/test_git_capture.py` L171-205) already relies on that duck typing. The helper implements the same `exec` as `Sandbox.exec` (`exec_run(cmd, workdir=REPO_DIR, demux=True)`, `sandbox.py` ~L660-666), so capture keeps the image's git version (Debian's packaged git; the exact version is **[unverified]** and is not assumed: `TrustedCapture.start()` requires ≥ 2.32, §5.4.1 step 4, and the §8.2 test 6 vectors run against whatever the image ships) instead of whichever git each developer has installed.
  - Under (a), agent-written bytes would be written out as real files on the developer's disk. Under (b) they pass through the host only as a tar stream: they are filtered and spooled to an anonymous temp file, never extracted on the host.
  - Cost of (b): one more short-lived container per Run, created from the image the Sandbox already uses. There is no image pull and no network.

  The host-side scan of `diff.json` and `events.jsonl` (§5.5) still runs on bytes the host writes. The changed-file bytes it scans are now read back from the helper's trusted copy, never from the agent's container.

**D10 — Teardown order (finding 11).** The inner `finally` runs `try: sandbox.teardown() finally: guard.teardown(run_dir)`. `guard.teardown` runs these steps in order, each one best-effort with one warning on failure and never raising:
1. read the proxy logs → `<run_dir>/egress.jsonl` and violation events
2. remove `vaultshield-egress-<id>`
3. remove `vaultshield-int-<id>`
4. remove `vaultshield-open-<id>`
5. `shutil.rmtree(stage, ignore_errors=True)` for the proxy's host staging dir (D6); skipped if `start()` never created it

The capture helper (§5.4.1) removes itself in its own `finally` as soon as step 8 has finished with it. As a backstop, the inner `finally` also calls `remove_capture_helper(run_id, client)` after `sandbox.teardown()` and before `guard.teardown`. That call is best-effort, prints one warning on failure, and does nothing if the helper is already gone.

The Sandbox goes first so no new connections arrive while the logs are read. Only then does the outer handler upload events and send the failed PATCH. Batch 2's "teardown first, then failed PATCH" holds.

**D11 — `Sandbox` gains three keyword-only knobs, all no-ops by default.**
- `create(..., network=None, cap_drop=None, sysctls=None, extra_hosts=None)`: each kwarg is passed to `containers.create` only when not `None`, so off-mode call kwargs are byte-identical.
- `exec(cmd, *, env=None)` → `exec_run(..., environment=env)`. This is needed for the git env (D9).
- `exec_env_defaults: dict[str, str]` (default `{}`) is merged under the caller's env in `exec_stream`, `exec_interactive` and `exec_detached`. The guard sets `HTTPS_PROXY`/`HTTP_PROXY` and their lowercase forms to `http://vaultshield-egress:3128`, and sets `NO_PROXY`/`no_proxy` to `localhost,127.0.0.1,::1`. An empty `NO_PROXY` would make curl, Go `net/http`, npm and Python `requests` send the agent's own `http://localhost:<port>` traffic to the proxy, which denies it as `plain_http` and floods the timeline with false violations (review 2 finding 8). Loopback never leaves the Sandbox's netns, so bypassing the proxy for it does not weaken D2. These are not secrets, and exec env does not persist (`test_an_exec_environment_does_not_persist_into_the_container`).

## 5. Design detail

### 5.1 Phases (non-off modes)

| Phase | `run` step | Sandbox networks | Sandbox can reach |
|---|---|---|---|
| guard start | 5a (new) | (no Sandbox yet) | the proxy is created on `vaultshield-open-<id>`, connected to `vaultshield-int-<id>` with alias `vaultshield-egress`, started, and waited on for `{"kind":"ready"}` (≤ 10 s) |
| create, clone, deps | 5 | `vaultshield-open-<id>` | anything, incl. `host.docker.internal` (needed by the `served_repo` tests) and the internet |
| switch + assertion | 5 (end) | `vaultshield-int-<id>` only | proxy only |
| trusted base record (all modes) | 5-base (new) | int (off mode: default bridge) | n/a. Docker API `get_archive` of `.git/HEAD`, the ref, `packed-refs`, `.git/config` and `.git/objects` (object snapshot), before any agent code (including `agy` sign-in) runs (§5.4.1) |
| sign-in (`--interactive-auth`) | 5b | int | proxy → allowlisted Google hosts |
| credential snapshot #1 | 5c (new) | int | n/a (Docker API) |
| agent | 6 | int | proxy |
| credential snapshot #2a | 6 (end) | int | n/a (Docker API) |
| app + screenshots | 7 | int | proxy; the host reaches the app via the proxy's published port (D4) |
| Sandbox killed; snapshot #2, git capture, hygiene scan | 8 | int (container stopped) | nothing: **Sandbox killed; Docker API reads only** (`kill()`, then `get_archive` for snapshot #2 and the work tree; no exec in the Sandbox). Capture runs in the capture helper (`network_mode="none"`, §5.4.1) |
| teardown | 9 | — | — |

`guard.isolate(sandbox)`:
1. `client.networks.get(int).connect(sandbox.container)`
2. `client.networks.get(open).disconnect(sandbox.container, force=False)`
3. Set `sandbox.exec_env_defaults` (D11).
4. Assert: run `sandbox.exec(["python3", "-c", _ISOLATION_CHECK, probe_name])`. The check script exits 0 only if all of these hold: TCP to `1.1.1.1:443` fails within 2 s, `getaddrinfo(sys.argv[1])` fails, and TCP to `vaultshield-egress:3128` succeeds. Python is already required in the Sandbox by the watcher.
   - `probe_name = isolation_probe_name(policy)`, a pure function in `egress.py`: the first of `("example.com", "example.net", "example.org", "vs-isolation-check.example.com", "vs-isolation-check.invalid")` that the effective allowlist does **not** match (exact or wildcard, any port). If every candidate is matched (only possible by listing them deliberately), `resolve_policy` raises `EgressConfigError` at step 1. Reason: `extra_hosts` maps every exact allowlisted name to the proxy's IP, so an allowlisted probe name would resolve from `/etc/hosts` and the check would fail on a correctly isolated Sandbox (review 2 finding 7; §8.2 tests 1-2 allowlist `example.com`).

Any Docker error, or a non-zero assertion, raises `EgressError("egress: could not isolate sandbox network: <detail>")`. That goes through the normal failed path: teardown, events upload, failed PATCH.

`extra_hosts` for the SNI path is computed before `Sandbox.create`. It maps each exact (non-wildcard) allowlist name to the proxy's int-network IP, read from `proxy.attrs["NetworkSettings"]["Networks"][int]["IPAddress"]` after `reload()`. During the open phase those names resolve to an address that cannot be reached yet. Clone and deps do not use the agent's API hosts.

### 5.2 Docker objects (all labelled `vaultshield.run_id=<id>`, `vaultshield.role=<role>`)

| Object | Spec |
|---|---|
| `vaultshield-open-<id>` | `networks.create(name, driver="bridge", internal=False, enable_ipv6=False, labels=…)` |
| `vaultshield-int-<id>` | `networks.create(name, driver="bridge", internal=True, enable_ipv6=False, labels=…)` |
| `vaultshield-egress-<id>` | `containers.create(EGRESS_PROXY_IMAGE, command=["python3", "-u", "/opt/vaultshield/egress_proxy.py", "/opt/vaultshield/egress.json"], network=open, ports={f"{port}/tcp": None} (only when the app port is published), cap_drop=["ALL"], cap_add=["NET_BIND_SERVICE"], sysctls={"net.ipv4.ip_forward":"0","net.ipv6.conf.all.forwarding":"0","net.ipv6.conf.all.disable_ipv6":"1"}, read_only=True, tmpfs={"/tmp": ""}, volumes={str(stage): {"bind": "/opt/vaultshield", "mode": "ro"}}, security_opt=["no-new-privileges"], mem_limit="128m", pids_limit=128, labels=…)`. No `put_archive` (a read-only rootfs refuses it, D6). `stage` is the host staging dir holding `egress_proxy.py` and `egress.json`. Then `networks.get(int).connect(proxy, aliases=["vaultshield-egress"])`, then `start()`. |
| Sandbox (non-off) | today's kwargs, plus `network=open`, `cap_drop=["NET_RAW"]`, `sysctls={"net.ipv6.conf.all.disable_ipv6":"1"}`, `extra_hosts={…}`, and `publish_port=None` |

`egress.json` = `{"agent", "mode", "allow": [{"host","port","wildcard"}...], "forward": {"port": P, "target": "vaultshield-run-<id>"} | null}`. It contains no secrets.

The proxy listens on 3128 (CONNECT), on 443 (SNI) and on P (forward). **P must not collide with them (review round 4 finding 4).** `resolve_policy(agent, config_egress, cli_mode, *, app_port)` runs at step 1, before anything exists: if the effective mode is not `off` and `app_port in (3128, 443)`, it raises `EgressConfigError("egress: the app port <p> collides with the egress proxy's port <p>; change `port` in .vaultshield.yml or use --egress=off")`, which `cli.py` turns into a `ClickException`. Off mode is unaffected (no proxy). It allows at most 64 concurrent tunnels, each with a 5 s handshake and a 300 s idle timeout. At startup it refuses to run, with exit 2 and a JSON `{"kind":"fatal"}` line, if `ip_forward` or `forwarding` is not `0`.

### 5.3 Proxy log → `violation` events

`guard.teardown(run_dir)` reads `proxy.logs(stdout=True, stderr=False)`, writes the raw lines to `<run_dir>/egress.jsonl` (a local artifact the uploader does not read), and maps them as follows:

| Log line | Event payload (exact) |
|---|---|
| `decision=deny`, `kind=connect` | `egress violation: CONNECT <host>:<port> denied (<reason>; agent <agent>, egress <mode>)` |
| `decision=deny`, `kind=sni` | `egress violation: TLS SNI <host>:443 denied (<reason>; agent <agent>, egress <mode>)` |
| `decision=would_deny` | `egress violation (log-only, allowed): CONNECT <host>:<port> not in allowlist for <agent>` |
| `allow`, `ready`, unparsable | no event |

- `reason` ∈ {`not_in_allowlist`, `ip_literal`, `private_address`, `port_not_allowed`, `plain_http`, `bad_request`, `no_sni`}.
- `<host>` in every payload is the line's **`raw_host`** (the pre-normalisation spelling), not the lowercased `host` (review round 4 finding 9). Reason: `SecretSet.scrub` is case-sensitive, so a token the agent placed in a hostname (`<Token>.example.com`) would survive scrubbing in lowercased form and reach the dashboard. `raw_host` comes from the agent, so it is sanitised: printable ASCII only, other characters → `?`, cut to 253 characters. A missing value becomes `-`. The events upload then scrubs the payload as usual (§5.5).
- `timestamp` = the line's `ts`, written by the proxy as `datetime.now(timezone.utc).isoformat()`, which is `monitor._now_iso8601()`'s format.
- At most 200 events per Run. If more denials happened, one final `egress violation: <n> further denials not listed (see egress.jsonl in the Run_Directory)` is added.
- Every payload is non-blank and the event type is `violation` (≤ 32 characters), so backend ingest and `TimelineTab` need no change (§1).

### 5.4 Git capture hardening (token vector 2)

`git_capture.py` gains `_git(*args) -> list[str]`, built as:

```
["git", "--git-dir=.git", "--work-tree=.",
 "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
 "-c", "core.excludesFile=/dev/null", "-c", "core.attributesFile=/dev/null",
 "-c", "protocol.allow=never", *args]
```

It also gains `_GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}`, passed as `sandbox.exec(cmd, env=_GIT_ENV)`.

The paths are relative because `Sandbox.exec` runs in `REPO_DIR`. That keeps `LocalGitSandbox` (cwd = the temp repo) working. The command-line `--work-tree` beats an agent-set `core.worktree`, and `-c` beats `.git/config` and its includes.

Every git call in `commit_run_branch`, `build_structured_diff` and `resolve_diff_base` uses `_git`. Numstat also gains `--no-ext-diff --no-textconv`.

New first step of `commit_run_branch`, `_assert_capture_repo_is_safe(sandbox)`:
1. Check `_git("rev-parse", "--absolute-git-dir")` == `_git("rev-parse", "--show-toplevel")` + `"/.git"`. This refuses a `.git` that is a gitfile or a symlink.
2. `_git("config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$|^diff\..*\.(textconv|command)$")` must print nothing. Exit 1 means no match and is OK. This runs before `git add`, so a clean filter can never execute.

Either failure raises `UnsafeGitConfig(GitCaptureError)`, which `cli.py` handles like a token-hygiene violation (D9).

With trusted capture (§5.4.1), the git that runs `_git` and `_GIT_ENV` is the helper's, inside a repository the helper created itself. `_assert_capture_repo_is_safe` **stays the first step of `commit_run_branch`** and therefore also runs, harmlessly, against the helper's own repo in `run` (review round 3 finding 9); the §8.1 helper exec-sequence test includes its two commands. In addition (not instead), the same two checks run on the untrusted data:
- Check 1 becomes "the archived `repo/.git` member is a directory". A regular file (gitfile) or a symlink raises `UnsafeGitConfig`. An archive with **no** `repo/.git` directory member raises `UnsafeGitConfig("trusted capture: the Sandbox repo has no .git directory")` (fail closed, §5.4.1 step 2 "Archive root and `.git`").
- Check 2 runs as `git config --file /tmp/untrusted-config --get-regexp <same regex>` inside the helper, on the archived `.git/config` delivered as the one-member tar described in §5.4.1 step 1. `--file` without `--includes` does not follow `include.path`. The full exit-code rule (review round 4 finding 3; the file is agent-controlled, and a malformed config such as `[filter "x"` with no closing bracket makes `git config --file` exit 3):
  - exit 0 and non-empty stdout → `UnsafeGitConfig("trusted capture: the Sandbox .git/config defines a filter or diff driver")`;
  - exit 1, or exit 0 with empty stdout → OK;
  - any other exit code → `UnsafeGitConfig("trusted capture: the Sandbox .git/config could not be parsed (git config exit <n>)")`. Fail closed: today a malformed `.git/config` also fails the Run, because every in-Sandbox git command errors. Handled by the same clause as other hygiene failures. If `repo/.git/config` is absent, check 2 is skipped (nothing to refuse, no `put_archive` of the config); if it is present but not a regular file (symlink, hardlink, directory), `split_archive` raises `UnsafeGitConfig`.

§9 item 7 is unchanged in effect: a filter/textconv driver in the repo's config still fails the Run. The prefix and env stay as defence in depth.

### 5.4.1 Trusted capture: the capture helper container (D9, option b)

New module `cli/vaultshield/trusted_capture.py`. It works on `docker` objects and `tarfile` only. Every constant below is module-level.

**Step 5-base: record the trusted base, before any agent code.** `base = record_trusted_base(sandbox, *, caps) -> TrustedBase(object_format, commit_oid | None, objects_tar)` runs right after `sandbox.create(...)` returns. In non-off modes it runs after `guard.isolate(sandbox)`. It always runs before `_authenticate_in_sandbox`, because `agy` sign-in is agent code. It uses only `sandbox.container.get_archive(...)`, a Docker API call, so no in-container binary runs:
1. Read `REPO_DIR/.git/HEAD` (≤ 4 KiB, must be a regular-file member). Accepted forms, and nothing else (review 2 finding 3; `git_capture._checked_ref` is **not** used, because it only rejects an empty string and a leading `-`):
   ```python
   _HEAD_REF = re.compile(rb"ref: (refs/heads/[A-Za-z0-9._/-]{1,200})\n?\Z")
   _BARE_OID = re.compile(rb"([0-9a-f]{40}|[0-9a-f]{64})\n?\Z")
   def _valid_branch_ref(name: str) -> bool:
       comps = name.split("/")
       return not (
           ".." in name or "//" in name or "@{" in name
           or name.endswith("/") or name.endswith(".")
           or any(c == "" or c.startswith(".") or c.endswith(".lock") for c in comps)
       )
   ```
   A symbolic HEAD is accepted only if `_HEAD_REF` matches **and** `_valid_branch_ref(name)` holds. A bare oid is accepted only if `_BARE_OID` matches and its length agrees with the object format (item 3). This keeps the interpolated `get_archive(REPO_DIR/.git/<ref>)` path inside `.git/refs/heads/`, so a hostile install script cannot write `ref: refs/heads/../../../../root/.gemini/x` and make the host read an arbitrary file as the base.
2. Read the ref: `get_archive(REPO_DIR/.git/<ref>)` (≤ 4 KiB, regular file, content must match `_BARE_OID`). On `NotFound`, read `REPO_DIR/.git/packed-refs` (≤ 1 MiB) and parse only `<oid> SP <ref>` lines whose `<ref>` passes the same `_HEAD_REF`-name charset and `_valid_branch_ref`; `#` header lines and `^<oid>` peel lines are skipped, anything else is malformed. If neither has the ref, the clone is **unborn** (empty repository): `commit_oid=None`, the root-capture case already handled by `resolve_diff_base`.
3. Read `extensions.objectformat` from the archived `.git/config` (≤ 64 KiB) as plain text with this grammar (review 2 finding 12): scan lines; a line matching `^\s*\[\s*extensions\s*\]\s*$` (case-insensitive) opens the section, any other `^\s*\[` line closes it; inside it, a line matching `^\s*objectformat\s*=\s*(\S+)\s*$` (key case-insensitive) sets the value, and the **last** occurrence wins. No section or no key → `sha1`. A value other than `sha1`/`sha256` (compared lowercased) → `GitCaptureError`. `sha1` means 40-hex oids, `sha256` 64-hex.
4. **Snapshot the object store now, not after the agent** (review 2 finding 2). `get_archive(REPO_DIR/.git/objects)` is read through `_ChunkReader(stream, caps.stream)` (step 1 below) with `tarfile.open(mode="r|")`, and only members matching `objects/[0-9a-f]{2}/[0-9a-f]{38}` (sha1) or `objects/[0-9a-f]{2}/[0-9a-f]{62}` (sha256) and `objects/pack/pack-[0-9a-f]{40,64}\.(pack|idx)` are written, through the same `_filter_member` name/type/mode rules, to `TrustedBase.objects_tar` (a `SpooledTemporaryFile(max_size=64 MiB)`). Members are renamed `objects/...` (the archive root is already `objects/`). The limits are those of step 3, each applied separately (review round 3 finding 10): the stream limit counts every tar byte read, the member limit counts every member read including dropped ones, and `TRUSTED_CAPTURE_MAX_MEMBER_BYTES` also applies to pack files. `objects/info/*` (alternates, http-alternates, commit-graph) and anything else is dropped. The clone is full (no `--depth`/`--filter`, `sandbox.py` `_clone_repo`), so the base's objects are all present. `TrustedBase.close()` closes the spool; it is called in step 8's `finally` and in the inner `finally` of `run` (idempotent).

Anything malformed, oversized or of the wrong type (a symlink or a directory where a file is expected) raises `GitCaptureError("trusted capture: cannot read the pre-agent base commit: <reason>")` and goes through the normal failed path. `TrustedBase` holds no work-tree content; its `objects_tar` holds only pre-agent git objects, which carry the same trust as the base id. Trust rests on D3: the base is what clone + dependency install left behind, and hostile install scripts are already a stated residual risk (§9 item 10). Docker-level errors here follow the "Docker exception mapping" rule below.

**Docker exception mapping (review round 3 finding 4).** Applies to every docker-py call and every chunk iteration in `trusted_capture.py` (`record_trusted_base`, step 0 `kill()`, the step-8 `get_archive`, helper `containers.create`/`start`/`put_archive`/`exec_run`/`remove`) and in `token_hygiene.snapshot`:
- `docker.errors.NotFound` from a `get_archive` keeps its specific handling (a missing ref → `packed-refs` fallback; a missing credential root → empty `SecretSet`; the Sandbox gone at step 8 → `GitCaptureError("trusted capture: the Sandbox container is gone")`).
- Any other `docker.errors.DockerException` or `requests.RequestException` (including one raised mid-stream while iterating `get_archive` chunks inside `_ChunkReader.readinto`) → `raise GitCaptureError(f"trusted capture: {step} failed: {type(exc).__name__}: {exc}") from exc`, where `step` is a fixed label such as `"read .git/HEAD"`, `"kill the Sandbox"`, `"archive the Sandbox repo"`, `"create the capture helper"`, `"put_archive work tree"`. In `token_hygiene.snapshot` the prefix is `"token hygiene: credential snapshot failed: "`. Docker error text never contains needle bytes (the needles are only ever in host memory).
- `TrustedCapture.exec` mirrors `Sandbox.exec`: a docker-py error is wrapped as `SandboxError(f"trusted capture exec failed for run {run_id}: {exc}")`, which reaches the existing `except SandboxError` clause (`cli.py` ~L828).
- `TrustedCapture.remove()` keeps its rule: never raises, one warning.
- The image id is read as `sandbox.container.attrs["Image"]` (no extra API call), not `sandbox.container.image.id` (which makes an `images.get` call).
- Every mapped error is a `GitCaptureError` or `SandboxError`, so it reaches an existing `cli.py` clause and becomes the one-line `ClickException`, never a raw docker-py traceback.

**Step 8, step 0: stop the Sandbox (review round 3 finding 2).** Step 8 starts with `sandbox.container.kill()` through the Docker API (mapped per the rule above). This stops the step-7 app (which `browser_capture` never stops) and any background process the agent left, so:
- no file can grow mid-archive (Docker sizes tar headers from `Lstat`; a growing `.next/` or `node_modules/.cache` file would otherwise truncate the stream intermittently);
- no process can change the credential root or the work tree between snapshot #2 and the work-tree archive (closes the TOCTOU window).

Then, in this order: snapshot #2 (`token_hygiene.snapshot`, `get_archive`), then `get_archive(REPO_DIR)` (step 1). `get_archive` works on an exited container (asserted by a Docker-gated test, §8.2 test 7). From here on, step 8 makes **no exec in the Sandbox**; it only reads through `get_archive`. `sandbox.teardown()` stays `remove(v=True, force=True)`, which handles a stopped container. A `kill()` that raises `APIError` because the container already exited (409 "is not running") is treated as success; any other error is mapped per the rule above.

**Step 8: build the branch in the helper.** `capture = TrustedCapture(run_id, client, image_id=sandbox.container.attrs["Image"])`. That is the immutable image ID the Sandbox was created from, not the `vaultshield-sandbox:local` tag, so a rebuild during the Run cannot swap the image. The agent cannot modify image layers: its writes go to the container's own copy-on-write layer, and there is no docker.sock. The helper itself is:

```
containers.create(image_id, command=KEEP_ALIVE_CMD, name=f"vaultshield-capture-{run_id}",
                  working_dir=WORKDIR, network_mode="none", cap_drop=["ALL"],
                  security_opt=["no-new-privileges"], mem_limit="1g", pids_limit=256,
                  labels={"vaultshield.run_id": run_id, "vaultshield.role": "capture"})
```

There are no volumes, no env file, no published ports and no network. Then `start()`. The helper has no agent processes and no agent token, and its only inputs are the filtered archives below. Helper creation, use and removal run inside `try/finally` in `cli.py` step 8.

1. **Stream the work tree out.** `get_archive` returns `(chunk_iterator, stat)`, not a file object, so the host wraps it (review 2 finding 10):
   ```python
   class _ChunkReader(io.RawIOBase):
       """File adapter over docker-py's chunk iterator that enforces the stream cap."""
       def __init__(self, chunks: Iterator[bytes], cap: int): ...
       def readable(self) -> bool: return True
       def readinto(self, b) -> int:
           # copy from the current chunk (pulling the next one when empty) into b;
           # add the copied count to self.total; if self.total > cap raise
           # GitCaptureError("trusted capture: Sandbox archive exceeds <cap> (<total>+ bytes)");
           # return 0 at end of iterator
   stream, _stat = sandbox.container.get_archive(REPO_DIR)
   split = split_archive(io.BufferedReader(_ChunkReader(stream, caps.stream)), caps=caps)
   ```
   The stream's members are named `repo/...`. The host reads it once with `tarfile.open(fileobj=<reader>, mode="r|")`, which is streaming and never seeks. It writes one filtered **work-tree tar** to a `tempfile.SpooledTemporaryFile(max_size=64 MiB)`: memory first, then an anonymous file in the system temp directory, never inside any repo, deleted on close. **Nothing is extracted on the host.** Every spool (this one, `TrustedBase.objects_tar`, and the untrusted-config bytes) is `seek(0)`'d immediately before it is passed to `put_archive`.
   - **Work-tree tar:** every accepted member outside `repo/.git/` and outside any nested `.git` (step 2).
   - **No objects are taken from the post-agent archive.** The objects come from `TrustedBase.objects_tar`, snapshotted at 5-base before any agent code (review 2 finding 2). An agent that deletes, forges or alternates objects after 5-base changes nothing the helper uses.
   - **Everything under `repo/.git/` is dropped,** except `config` (separate copy below): `hooks/`, `refs/`, `packed-refs`, `HEAD`, `index`, `info/` (incl. `info/exclude`, `info/grafts`), `objects/` (all of it, incl. `objects/info/alternates`, `http-alternates`, `commit-graph`), `multi-pack-index`, `shallow`, `modules/`, `worktrees/`.
   - **Untrusted config:** `repo/.git/config` (≤ 64 KiB) is copied into the helper at `/tmp/untrusted-config`, only for the §5.4 check 2. Because `put_archive` takes a tar stream, `SplitArchive.untrusted_config` is a `SpooledTemporaryFile` holding a **PAX tar with exactly one member** named `untrusted-config` (regular file, mode `0o644`, `uid=gid=0`, `uname=gname=""`, content = the config bytes), written with the same `tarfile.open(fileobj=spool, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape")` call as the other output tars (review round 4 finding 8).
2. **Safe tar handling** (`_filter_member`). It applies to every member, and a rejection is fail-closed (`GitCaptureError("trusted capture: unsafe archive member <sanitised name>: <reason>")`, printable-ASCII name, ≤ 200 characters):
   - **Names.** Reject absolute names, any `..` component, an empty component, a NUL, and duplicate names. The member named exactly `repo` (the archive root) is allowed; every other name must start with `repo/` (review round 3 finding 1). Names are kept as raw bytes (`encoding="utf-8", errors="surrogateescape"`), so non-UTF-8 paths round-trip unchanged into the helper's Linux filesystem (Batch 1 behaviour preserved).
   - **Archive root and `.git`** (review round 3 finding 1; one unit case each, §8.1):
     - The `repo` member must be a directory. A symlink, regular file or any other type (the agent is root and can replace `/workspace/repo`) raises `GitCaptureError("trusted capture: the Sandbox repo directory is not a directory")`.
     - When the stream ends without a `repo/.git` directory member, `split_archive` raises `UnsafeGitConfig("trusted capture: the Sandbox repo has no .git directory")`. Fail closed: a deleted `.git` is a tamper signal, not a reason to skip check 1.
     - `repo/.git/config` absent → check 2 is skipped (`SplitArchive.untrusted_config is None`, nothing is put into the helper). Present but not a regular file (symlink, hardlink, directory) → `UnsafeGitConfig("trusted capture: the Sandbox .git/config is not a regular file")`.
   - **Malformed stream** (review round 3 finding 2). A `tarfile.TarError` (incl. `ReadError`, `HeaderError`) or `EOFError` raised while `split_archive` reads the stream → `GitCaptureError("trusted capture: the Sandbox archive stream was truncated or malformed")`. Docker/`requests` errors raised from inside `_ChunkReader` follow the Docker exception mapping above and are not re-wrapped.
   - **Output tar format** (review round 3 finding 11). Both output tars (the work-tree tar and `TrustedBase.objects_tar`) are written with `tarfile.open(fileobj=spool, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape")`, so a non-UTF-8 name is written as its raw bytes in a PAX header; §8.2 test 8 Case A checks the bytes round-trip through Docker.
   - **Types.** Allowed: regular file, directory, symlink, and hardlink. Rejected: character device, block device, FIFO, contiguous file, GNU sparse, and any unknown type.
   - **Hardlinks** (review 2 finding 4). A `LNKTYPE` member is accepted only if all of these hold:
     - (i) its `linkname` passes the same name rules as a member name (not absolute, no `..` or empty component, no NUL, under `repo/` for the work-tree stream or under `objects/` for the 5-base objects stream);
     - (ii) `linkname` names an **earlier** member that was emitted as a regular file into **the same output tar** (the work-tree tar for the post-agent stream; `objects_tar` for the 5-base stream), tracked in a per-output-tar `set` of emitted regular names;
     - (iii) in `objects_tar`, `linkname` is rewritten with the same prefix rule as the member name (the 5-base archive root is already `objects/`, so the rewrite is the identity, stated so the rule is total).
     
     Anything else, including a work-tree link whose target is under `repo/.git/` (dropped, so not in the work-tree tar), raises `GitCaptureError("trusted capture: unsafe archive member <name>: hardlink to <sanitised linkname> outside its archive")`.
   - **Symlinks** (review round 3 finding 6, the review's preferred fix). Every symlink member, in-repo or escaping (absolute target, or `posixpath.normpath(join(dirname(link), target))` outside `repo/`), is **copied into the helper as a symlink**. It is never followed by the host, `put_archive` creates the link without following it, and the helper holds no secrets, so an escaping link there points at nothing of value. The filter itself raises no symlink finding. `symlink_outside_repo` is derived only from links the Run **added or retargeted**, from §5.5 (c)'s `diff --raw` records whose new mode is `120000` (target read by `cat-file blob <new oid>`). Reason: a base repo that already tracks an escaping link (e.g. `docs/latest -> /usr/share/doc/x`) would otherwise fail every Run, and dropping the link would make the helper's `add --all` stage a deletion the agent never made. An agent-created link to `/root/.gemini/...` still fails the Run (acceptance 6 unchanged).
   - **Symlinked parents.** Reject any member whose parent path passes through an accepted symlink (in-repo or escaping). Writing through a link is the classic extraction escape.
   - **Metadata.** Modes are **normalised**, not masked (review 2 finding 9): directories → `0o755`; regular files → `0o755` if any of `0o111` is set, else `0o644`; symlinks → `0o777`. This strips setuid/setgid/sticky, keeps the one bit git records, and lets the `cap_drop=["ALL"]` helper (root without `CAP_DAC_OVERRIDE`) read a file or traverse a directory the agent `chmod 000`'d. `uid=gid=0`, `uname=gname=""`. PAX and GNU extended headers are rebuilt by `tarfile` from the name and linkname only, so no other header passes through.
   - **`repo/.git` itself must be a directory member.** A file (gitfile) or a symlink raises `UnsafeGitConfig` (§5.4 check 1); its absence raises too ("Archive root and `.git`" above).
   - **Nested `.git` entries are dropped, and the files under them are captured as plain content** (review 2 finding 5, the review's recommended option). A nested `.git` (directory or file, e.g. from `git init sub`) would make the helper's `add --all` record `sub/` as a gitlink, so `diff.json` would show only a "Subproject commit" line and scan (c) would have no blob: a hiding trick. The filter therefore drops every member whose path has a component exactly `.git` **below** `repo/` (i.e. `repo/<p>/.git` and everything under it, for any non-empty `<p>`), and records one entry per distinct such directory in `SplitArchive.notices` (non-failing, kept separate from the failing `findings`). Right after `split_archive` returns, `cli.py` step 8 appends each notice with `monitor._append_event_log` as one `violation` event: `trusted capture: nested git repository at <sanitised path> was captured as plain files`. The helper's `add --all` then commits `sub/...` as ordinary files, which are diffed and scanned. Paths that are gitlinks in the base tree (unpopulated submodules) are unaffected, because a fresh clone has no `.git` there; a populated one after the agent is captured as plain files the same way, which shows in the diff as the gitlink being replaced by files. Chosen over failing the Run because an agent legitimately scaffolding a project can run `git init`, and the content is now fully visible.
   - **Defence in depth.** Docker's `put_archive` extraction also resolves paths within the container root (docker `archive` "FollowSymlinkInScope"). The host filter does not rely on it.
3. **Size limits.** All of these fail loudly with `GitCaptureError("trusted capture: <what> exceeds <cap> (<measured>)")`. Nothing is skipped or dropped silently.
   - Each limit applies separately, to each stream (the 5-base objects stream and the step-8 work-tree stream) on its own (review round 3 finding 10).
   - `TRUSTED_CAPTURE_MAX_STREAM_BYTES = 4 GiB`: every tar byte read from the Sandbox, counted by `_ChunkReader`. This is the dominant cost, because ignored `node_modules` are streamed too (§9 item 12).
   - `TRUSTED_CAPTURE_MAX_MEMBERS = 1_000_000`: every member read, **including dropped ones** (e.g. `repo/.git/*`, nested `.git`).
   - `TRUSTED_CAPTURE_MAX_MEMBER_BYTES = 1 GiB` for a single regular member, **including git pack files** in the 5-base objects stream.
   - `.git/config` ≤ 64 KiB; the base-record files as in 5-base.
   - Helper limits: `mem_limit="1g"`, `pids_limit=256`.
4. **Load the helper.** `TrustedCapture.start()` first runs `git version` in the helper and parses `git version (\d+)\.(\d+)`; below 2.32 or unparsable it raises `GitCaptureError("trusted capture: the Sandbox image's git <v> is older than 2.32")`. 2.32 is the floor because it is the first release that honours `GIT_CONFIG_GLOBAL` (used by `_GIT_ENV`, §5.4); on 2.29-2.31 that variable is ignored and the "cannot read global config" claim would be false. It also covers `init --object-format` (2.29) (review round 4 finding 6; review 2 finding 12; `base_image` is configurable, so the version is checked, not assumed). Then `helper.put_archive(WORKDIR, <work-tree tar>)` lays down `/workspace/repo/` without any `.git`. Then the helper runs, all through `_git`/`_GIT_ENV` plus `GIT_NO_REPLACE_OBJECTS=1`:
   - `git init -q --template= --object-format=<fmt> /workspace/repo`. `--template=` means no sample hooks.
   - `put_archive(/workspace/repo/.git, base.objects_tar)` (the 5-base snapshot, never post-agent objects).
   - `put_archive("/tmp", split.untrusted_config)` (the one-member tar, step 1; it lays down `/tmp/untrusted-config`), then the §5.4 check 2 with its exit-code rule. Both are skipped when `split.untrusted_config is None` (no `repo/.git/config` in the archive).
5. **Rebuild from the trusted base.** When `commit_oid` is set:
   - `_git("cat-file", "-e", f"{oid}^{{tree}}")` must exit 0, then `_git("rev-list", "--objects", oid)` (stdout discarded) must exit 0. Together they check presence and connectivity of the base and everything reachable from it. Either failure → `GitCaptureError("trusted capture: the pre-agent object snapshot is incomplete for base <oid>")`. This can only happen if clone + install left a broken store (D3 residual), because the objects were snapshotted at 5-base, before any agent code.
   - **No `git fsck --full`** (review 2 finding 2). It would fail legitimate repos on non-integrity fsck messages (`badTimezone`, `missingSpaceBeforeDate`, `badFilemode`, which `git clone` accepts by default with `transfer.fsckObjects=false`) and re-hash the whole store on every Run. Hash re-verification is unnecessary: the objects carry the same trust as the base id, since both were read before any agent code ran.
   - `_git("update-ref", "--no-deref", "HEAD", oid)` then `_git("read-tree", oid)`. The index is the base tree, and HEAD is detached at the base.

   When `commit_oid` is `None` (unborn), HEAD stays unborn, exactly like an empty clone today.
6. **Unchanged capture code, new exec target.** `TrustedCapture` exposes `exec(cmd, *, env=None) -> ExecResult` with `Sandbox.exec`'s exact contract (`exec_run(cmd, workdir=REPO_DIR, demux=True, environment=env)`, the same decoding and `stdout_bytes`). It also exposes `.container`. `cli.py` step 8 then calls today's `commit_run_branch(capture, run_id)`, `resolve_diff_base(capture, branch)` and `build_structured_diff(capture, base_ref, branch, run_dir)` with **no change to their bodies** beyond §5.4's `_git` prefix:
   - `checkout -b` from the detached base works and keeps the work-tree changes.
   - `add --all` respects the work tree's `.gitignore` files. These are agent-editable, but that is today's semantics, and a `.gitignore` edit is itself in the diff. The container's `.git/info/exclude` and the index, including `assume-unchanged`/`skip-worktree` bits, are gone, which closes those hiding tricks.
   - `diff.json` is written by the host from the helper's exec output.
7. **How the branch reaches wherever it goes today: nowhere, unchanged.**
   - Today `commit_run_branch` commits in the Sandbox (`git_capture.py` L530-580, called at `cli.py` L815-817). The branch dies with the container at `sandbox.teardown()` (`cli.py` ~L890), and nothing fetches or bundles it (§1).
   - `submit` pushes `vaultshield/run-<run_id>` from the host `repo_root` (`submit.py` L1153-1155, `[*config_args, "push", remote, branch]`). Its docstring names the gap: the host checkout has no such branch (`submit.py` ~L207-224).
   - Trusted capture keeps exactly that. The branch lives in the helper and dies with it. What leaves the machine is still `diff.json`, `events.jsonl` and screenshots, now built from trusted git.
   - Closing the `submit` gap (e.g. a `git bundle` from the helper into the Run_Directory) is a separate change and is not in this batch. The helper makes it straightforward, because the bundle would come from trusted git.
8. **Credential scan on host-held bytes (§5.5).**
   - `check_run_outputs` receives `capture`, not `sandbox`.
   - (c) reads each changed file's committed blob by **oid** through `capture.exec` (`diff --raw` + `cat-file blob`, §5.5), never by path. These are the bytes the helper's git committed, never the agent's container.
   - (d) uses the same `diff --raw` records as (c): only added or retargeted symlinks (new mode `120000`) are checked, by `cat-file blob <new oid>` through `capture`. There is no whole-tree `ls-tree` scan and no symlink finding from step 2 (review round 3 finding 6).
   - (a) and (b) are unchanged: `diff.json` and `events.jsonl` are host files.
9. **Removal.** `capture.remove()` = `container.remove(v=True, force=True)`. It runs in step 8's `finally` after the scan has read what it needs, followed by `split.close()` and `base.close()` (spools; idempotent, never raise). `NotFound` is ignored; any other error gives one warning. `remove_capture_helper(run_id, client)` (by name, idempotent) is the inner-`finally` backstop (D10) and is also called by `teardown.py`.

### 5.5 Credential scan (token vectors 1, 3, and agent output)

`token_hygiene.py`:
- `snapshot(sandbox, roots, *, credential_files=None) -> Snapshot(secrets: SecretSet, credential_files: frozenset[str], skipped: int)`. For each root, `container.get_archive(root)`; `NotFound` gives an empty result. The tar stream is read through `_ChunkReader(stream, CREDENTIAL_SNAPSHOT_MAX_STREAM_BYTES = 64 MiB, error_prefix="token hygiene: credential snapshot failed: ")` (imported from `trusted_capture.py`; the new keyword-only `error_prefix` defaults to today's `"trusted capture: "` wording), so a larger stream raises `GitCaptureError("token hygiene: credential snapshot failed: archive of <root> exceeds 64 MiB (<n>+ bytes)")` (review round 4 finding 2).
  - **Credential file set (review round 4 finding 1).** Snapshot #1 (5c, post-sign-in, pre-agent) is called with `credential_files=None` and **defines** the set: the root-relative paths (as raw tar names) of every regular member under each root. It is returned in `Snapshot.credential_files` and kept by `cli.py` for the Run. Snapshots #2a and #2 pass that set back and select only:
    - members whose path is in the set (re-read, so a refreshed token in the same file is caught), plus
    - **new** members whose basename matches `(?i)(token|oauth|credential|cred|secret|auth|session|\.pbtxt$|\.json$)` **and** none of whose directory components equals (case-insensitively) `tmp`, `logs`, `history`, `brain`, `conversations` or `cache`.
    
    Every other member (agent session state, logs, transcripts, checkpoints written during the Run) contributes **no needles**. Reason: what `agy` writes under `/root/.gemini` during a Run is [unknown] (inv §3), and agent CLIs of this family keep transcripts there; a transcript would turn the prompt, the agent's narration and code it wrote into needles and fail legitimate Runs.
  - **Needles from each selected regular member**, in archive order:
    - the whole content as a needle, if ≥ 32 bytes and **≤ 8 KiB** (a credential, not a state dump);
    - every run matching `[A-Za-z0-9._~+/=-]{32,}` that contains at least one digit, one uppercase and one lowercase character (access/refresh-token shapes; long plain URLs and words are excluded), from every selected member.
  - **Limits, never silent (review round 4 finding 2).** A selected member over 1 MiB, or any selected member reached after 8 MiB of selected content has been read, contributes no needles and is counted in `Snapshot.skipped` (its bytes are still read through the stream cap). Right after each snapshot call, `cli.py` appends, if `skipped > 0`, one **non-failing** `violation` event with `monitor._append_event_log`: `token hygiene: <n> credential-root files not scanned for needles (over 1 MiB or past the 8 MiB cap)` (built by `token_hygiene.skipped_snapshot_event(n)`).

  Plus the path literals: the absolute root (`/root/.gemini`) and each first-level child directory of the root seen in the snapshot, in `~/`-form (e.g. `~/.gemini/antigravity-cli`). A bare `.gemini` is deliberately not a needle, because repos legitimately carry a project-level `.gemini/` directory. (Path literals are unchanged by the file-set rule, so §9 item 5 is unaffected.) Only the needles stay in memory, and `SecretSet.__repr__` prints counts only. `cli.py` merges each `Snapshot.secrets` into the Run's `SecretSet` with `secrets.add(...)`.
  - When the developer probe (§3.2) has confirmed the token file, `Antigravity_Adapter.credential_paths()` narrows from `("/root/.gemini",)` to that file (§9 item 14).
- `check_run_outputs(capture, secrets, run_dir, branch, base_ref)` scans the items below. `capture` is the §5.4.1 helper. (The former `extra_findings` parameter is removed: since review round 3 finding 6 the tar filter produces no symlink findings; all `symlink_outside_repo` findings come from (d).)
  - (a) `diff.json`, parsed: each entry's `path` and `patch`;
  - (b) each `events.jsonl` payload;
  - (c) the capture branch, path-independently (review 2 finding 6: `diff.json` stores the U+FFFD spelling of a non-UTF-8 name, `lossy_path=True`, so reading by that path raises `NotFound` or reads a different file). It runs `capture.exec(_git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base_ref, branch), env=_GIT_ENV)` and parses the raw bytes: each record is `:<old mode> <new mode> <old oid> <new oid> <status>\0<path bytes>\0`. For each record whose new mode is `100644` or `100755` and whose new oid is not all zeros, it runs `capture.exec(_git("cat-file", "blob", <new oid>), env=_GIT_ENV)` and scans `stdout_bytes`. Findings name the file by the lossy display path (`path_bytes.decode("utf-8", "replace")`). No `get_archive` is used. This reads the **capture helper's** committed blobs (§5.4.1 step 8), never the agent's container. A malformed raw record or a failing `cat-file` → `GitCaptureError`, never a silent skip. The cap is 32 MiB in total, counted in blob order. Anything past the cap is recorded as one non-failing `violation` event: `token hygiene: <n> changed files not scanned (over 32 MiB)`. It leaves the machine only through `diff.json`, which is scanned in full;
  - (d) symlinks the Run **added or retargeted**, from the same (c) `diff --raw` records: each record whose new mode is `120000` and whose new oid is not all zeros → `capture.exec(_git("cat-file", "blob", <new oid>), env=_GIT_ENV)`. A target that is absolute, or whose `normpath` from the link's directory escapes the repo, is a finding `symlink_outside_repo`. Unchanged symlinks tracked by the base are not inspected, so a repo that already tracks an escaping link runs normally (review round 3 finding 6). The target bytes are also scanned for needles like any blob.
- A finding is `(kind, artifact, repo_path)` and never contains the matched bytes. `kind` ∈ {`credential_bytes`, `credential_path`, `symlink_outside_repo`, `unsafe_git_config`}.
- On any finding:
  1. Append one `violation` event per finding: `token hygiene violation: <kind> in <artifact> (<repo_path or '-'>); the Run's diff and screenshots were not uploaded`.
  2. Rename `diff.json` → `diff.quarantined.json`.
  3. Raise `TokenHygieneViolation(findings)`, with the message: `credential material from <root> found in <artifacts>; nothing but the scrubbed timeline was uploaded`.
- `SecretSet.scrub(text)` replaces every needle with `***`, the same marker as `sandbox._REDACTED`. It is applied to every payload in the events upload, on the success path too, as defence in depth.

A `.gitignore`d copy of the token is not committed (git respects `.gitignore` for `add --all`), so it does not reach `diff.json` or vector (c), and it is not a finding. The Docker-gated test pins this.

### 5.6 macOS (Docker Desktop) vs Linux

The mechanism is the same on both, because only the daemon API is used.
- On Docker Desktop, everything was measured on 29.2.1 (§1).
- On Linux, `host.docker.internal` does not exist unless `host-gateway` is mapped. VaultShield does not map it.
- Linux residual risk: Docker may not filter traffic from an internal bridge to the host's own IP on that bridge (the INPUT path). The int gateway checks in the Docker-gated test cover the machine they run on. This design does not claim it for an unmeasured Linux host. The CI/Linux result is reported separately. ~~`com.docker.network.bridge.inhibit_ipv4` is accepted by 29.2.1, but its effect was ambiguous (measured "refused" instead of "timeout"), so it is not relied on.~~ **Superseded (iteration 1, after the real-agy probe):** a full 1-65535 TCP scan from a container on a plain `internal: true` bridge found the Docker Desktop VM's own listeners open on the gateway (`.1`): rpcbind **:111** and a Docker Desktop daemon on **:63289** (published compose ports 5173/8000 time out there; on the default bridge all four are open). So the int network is now created with `options={"com.docker.network.bridge.inhibit_ipv4": "true"}`: the bridge gets **no address**, so the VM has no presence on the int network at all. The earlier "ambiguous refused" is explained: with no bridge address, `.1` is assigned to the first container on the network — in that measurement the probing container itself, hence "refused" on every port. In a Run the proxy is connected first and holds `.1`; the Docker-gated test scans every port at `.1` and asserts the only open ones are the proxy's 443 and 3128, that `.1` is the proxy, and that the int network's IPAM has no `Gateway`. Sandbox → proxy (container-to-container) is unaffected.
- IPv6: both networks set `enable_ipv6=False` and both containers set `disable_ipv6=1`. This holds even on a daemon with IPv6 enabled by default.

## 6. Design by file

| File | Change |
|---|---|
| `cli/vaultshield/egress_proxy.py` (new) | Self-contained stdlib proxy (D5, D6). Pure, importable functions: `parse_allow_entry`, `normalise_host`, `matches(entries, host, port)`, `is_public_address`, `decide(entries, mode, host, port, resolver) -> Decision`, `parse_connect_request(bytes)`, `parse_client_hello_sni(bytes)`. A `serve(config_path, *, resolver=socket.getaddrinfo, address_policy=is_public_address)` entry point that also runs the forwarder. `__main__` does the startup sysctl check and prints the `ready` line. No VaultShield imports, because it is copied alone into the container. |
| `cli/vaultshield/egress.py` (new) | Imports `EgressConfig` from `config.py`, never the other way round, so `config.py` stays free of docker imports. `BUILTIN_ALLOWLISTS` (D7). `EgressError`, `EgressConfigError`. `resolve_policy(agent, config_egress, cli_mode, *, app_port) -> EgressPolicy` (validation + union; in non-off modes an `app_port` of 443 or 3128 raises `EgressConfigError`, §5.2, review round 4 finding 4). Names: `egress_container_name`, `open_network_name`, `int_network_name`. `EGRESS_PROXY_IMAGE`, `PROXY_ALIAS`, `PROXY_PORT = 3128`. `class EgressGuard(run_id, client, policy, *, app_port)` with `.active`, `.start()`, `.sandbox_create_kwargs()`, `.isolate(sandbox)`, `.port_publisher`, `.teardown(run_dir) -> None` (never raises), and `.proxy_log_lines()`. `proxy_lines_to_events(lines, agent, mode) -> list[EventLogRecord]` (§5.3). `remove_egress_resources(run_id, client) -> bool` for `teardown.py`; after the proxy and networks it also removes every `glob(f"{tempfile.gettempdir()}/vaultshield-egress-{run_id}-*")` directory with `shutil.rmtree(..., ignore_errors=True)`, because `mkdtemp`'s random suffix is known only to the `guard` of the killed process (review round 4 finding 10). In off mode every method is a no-op and makes no Docker call. |
| `cli/vaultshield/token_hygiene.py` (new) | `SecretSet` (`add`, `scrub`, `__bool__`, count-only `__repr__`). `snapshot(sandbox, roots, *, credential_files=None) -> Snapshot(secrets, credential_files, skipped)` (credential file set fixed at #1, selection regex and excluded directories, 8 KiB whole-file rule, 1 MiB member / 8 MiB total limits counted in `skipped`, stream bounded by `_ChunkReader(…, 64 MiB, error_prefix=…)`; review round 4 findings 1-2). `skipped_snapshot_event(n) -> EventLogRecord`. `Finding`. `TokenHygieneViolation(Exception)`. `check_run_outputs(capture, secrets, run_dir, branch, base_ref)` (§5.5; `capture` is the trusted capture helper, (c) and (d) read through it by oid from one `diff --raw`). `snapshot` maps Docker/`requests` errors per §5.4.1 "Docker exception mapping". |
| `cli/vaultshield/trusted_capture.py` (new) | §5.4.1 (Batch 3 FR5, trusted capture). `TrustedBase(object_format, commit_oid, objects_tar)` with `.close()`; `record_trusted_base(sandbox, *, caps) -> TrustedBase` (Docker API only; HEAD/ref rules `_HEAD_REF`, `_BARE_OID`, `_valid_branch_ref`; objectformat grammar; 5-base object snapshot); `_ChunkReader(chunks, cap, *, error_prefix="trusted capture: ")` (`io.RawIOBase` with `readinto`, enforces the stream cap; `token_hygiene.snapshot` passes its own prefix, review round 4 finding 2); `TRUSTED_CAPTURE_MAX_STREAM_BYTES`, `TRUSTED_CAPTURE_MAX_MEMBERS`, `TRUSTED_CAPTURE_MAX_MEMBER_BYTES`, and frozen `TrustedCaptureCaps(stream, members, member, config)` defaulting to them (tests pass small caps); `notices_to_events(notices) -> list[EventLogRecord]` (the nested-`.git` payload, §5.4.1 step 2); `capture_container_name(run_id)` → `vaultshield-capture-<id>`; pure `_filter_member(member, state) -> Accept \| Drop \| Notice` (rejections raise; no symlink findings since round 3 finding 6) and `split_archive(reader, *, caps) -> SplitArchive(worktree_tar, untrusted_config: SpooledTemporaryFile \| None, notices)` with `.close()` (unit-testable without Docker; root/`.git`/config rules, tar-error mapping and PAX output format per §5.4.1 step 2); `kill_sandbox(sandbox)` (step 0; "not running" 409 is success); the Docker exception mapping of §5.4.1 wraps every docker-py call and chunk iteration; `class TrustedCapture(run_id, client, *, image_id)` with `.start()` (create, start, git ≥ 2.32 check), `.load(split, base)` (init, 5-base objects, config check, `cat-file -e`/`rev-list --objects`, detached base), `.exec(cmd, *, env=None)` (same contract as `Sandbox.exec`), `.container`, `.remove()` (never raises; one warning); `remove_capture_helper(run_id, client) -> bool` for `teardown.py`. Imports `git_capture._git`, `_GIT_ENV`, `GitCaptureError`, `UnsafeGitConfig` and `sandbox.REPO_DIR`, `WORKDIR`, `KEEP_ALIVE_CMD` (the public alias added in `sandbox.py`), `SandboxError`. |
| `cli/vaultshield/config.py` | New frozen `EgressConfig(mode: Literal["off","log-only","allowlist"] = "off", allow: tuple[AllowEntry, ...] = ())`. `ProjectConfig.egress: EgressConfig = field(default_factory=EgressConfig)` (last field with a default, like `project`). `_optional_egress(raw, path)` does the shape and grammar validation by calling `egress_proxy.parse_allow_entry`, so a bad entry is a `ConfigError` at step 1. |
| `cli/vaultshield/sandbox.py` | D11. `create(..., network=None, cap_drop=None, sysctls=None, extra_hosts=None)`. `exec(cmd, *, env=None)`. `exec_env_defaults` merged in `exec_stream`/`exec_interactive`/`exec_detached`. Public alias `KEEP_ALIVE_CMD = _KEEP_ALIVE_CMD` next to `_KEEP_ALIVE_CMD` (~L141), used by `trusted_capture.py` (review round 3 finding 8). Docstrings tagged (Batch 3 FR2). `teardown`/`_cleanup_after_failure` unchanged: the guard owns its own objects, and a `create()` failure propagates to `run`'s inner `finally`, which calls `guard.teardown`. |
| `cli/vaultshield/git_capture.py` | §5.4: `_git`, `_GIT_ENV`, `UnsafeGitConfig(GitCaptureError)`, `_assert_capture_repo_is_safe`, and all git calls routed through `_git(..)` with `env=_GIT_ENV`. The module docstring gains an "Agent-controlled git configuration" section, which also says that `run` calls these functions on the trusted capture helper (§5.4.1), not on the Sandbox. The function signatures stay duck-typed on `.exec`, so the bodies do not change for trusted capture. `_assert_capture_repo_is_safe` stays the first step of `commit_run_branch`, so in `run` it also runs, harmlessly, against the helper's own repo; the untrusted-archive versions of its two checks (§5.4) are additional, not replacements (review round 3 finding 9). |
| `cli/vaultshield/browser_capture.py` | `resolve_base_url(sandbox, port, *, published_by=None)` and `start_and_capture(..., published_by=None)` (D4). When `published_by` is given, the container-IP fallback is skipped: an internal-only Sandbox IP is never host-reachable. |
| `cli/vaultshield/adapters/base.py` | `Agent_Adapter.credential_paths(self) -> tuple[str, ...]: return ()`. |
| `cli/vaultshield/adapters/antigravity.py` | `ANTIGRAVITY_CREDENTIAL_ROOT = "/root/.gemini"` and `credential_paths()` returning it. `ANTIGRAVITY_TOKEN_PATH` is kept, but its comment says the filename is unconfirmed and that the guard covers the whole root. The docstring's `jetski_state.pbtxt` paragraph is aligned with this. Final filename update after the probe run. |
| `cli/vaultshield/monitor.py` | No change. `egress.py` and `token_hygiene.py` use `EventLogRecord`, `event_log_path`, `_append_event_log` and `_timestamp_sort_key`, the same way `sandbox_monitor` does. |
| `cli/vaultshield/uploader.py` | `upload_events_best_effort(backend_url, token, run_id, run_dir, *, scrub) -> str \| None`. It reuses `_read_event_log`, `_batched` and `EVENTS_PATH_TEMPLATE`, uses `httpx.Timeout(5.0, connect=2.0)`, returns a one-line warning or `None`, and never raises. `upload_run` gains `scrub: Callable[[str], str] = lambda s: s`, applied to event payloads. |
| `cli/vaultshield/cli.py` | `run --egress` option (`click.Choice(["off","log-only","allowlist"])`, default `None`). Step 1: `policy = egress.resolve_policy(agent, config.egress, egress_mode, app_port=config.port)` → `ClickException`. `secrets = SecretSet()` before the outer `try`. Step 5: `guard = EgressGuard(...)`, `guard.start()`, `sandbox.create(..., publish_port=None if guard.active else config.port, **guard.sandbox_create_kwargs())`, `guard.isolate(sandbox)`. 5c: `snap1 = snapshot(sandbox, adapter.credential_paths())` after 5b (#1, defines `snap1.credential_files`), again after step 6 (#2a) and at the start of step 8 right after `kill_sandbox(sandbox)` (#2, D9), both with `credential_files=snap1.credential_files`. After each call: `secrets.add(snap.secrets)` and, if `snap.skipped`, append `skipped_snapshot_event(snap.skipped)` (§5.5). Step 7 passes `published_by=guard.port_publisher`. Step 8, after `build_structured_diff`: `check_run_outputs(...)`. New `except EgressError` / `except (TokenHygieneViolation, UnsafeGitConfig)` clauses → `ClickException(f"Run {run_id} failed: {exc} (Event_Log preserved at …)")`. Inner `finally` per D10. Outer `except BaseException`: `_upload_events_best_effort(...)`, then `_mark_run_failed_best_effort(...)`, then `raise`. `upload_run` receives `scrub=secrets.scrub`. The `run` docstring pipeline list gains 5a/5-base/5c. Trusted capture, in every egress mode:
- 5-base: `base = record_trusted_base(sandbox, caps=caps)`, after `create` and `isolate`, before 5b.
- Step 8: `kill_sandbox(sandbox)`, then snapshot #2, then `capture = TrustedCapture(run_id, client, image_id=sandbox.container.attrs["Image"])`, `capture.start()`, `stream, _ = sandbox.container.get_archive(REPO_DIR); split = split_archive(io.BufferedReader(_ChunkReader(stream, caps.stream)), caps=caps)`, append `split.notices` as `violation` events, `capture.load(split, base)`. Then today's three `git_capture` calls with `capture` instead of `sandbox`, then `check_run_outputs(capture, secrets, run_dir, branch, base_ref)`, all inside `try/finally: capture.remove(); split.close(); base.close()`.
- New helper `_remove_capture_helper_best_effort(run_id, client)` in `cli.py`: `try: trusted_capture.remove_capture_helper(run_id, client) except BaseException as exc: click.echo(f"Warning: could not remove the capture helper for run {run_id}: {type(exc).__name__}", err=True)`. Never raises; used by the inner `finally` backstop (D10).
- New helper `_take_snapshot(sandbox, adapter, secrets, run_dir, credential_files) -> Snapshot`: calls `token_hygiene.snapshot(sandbox, adapter.credential_paths(), credential_files=credential_files)`, `secrets.add(snap.secrets)`, and appends `skipped_snapshot_event(snap.skipped)` when `snap.skipped > 0`. Errors propagate (they are `GitCaptureError`s, §5.4.1 mapping).
- `except GitCaptureError` clause (today `cli.py` ~L863), via new helper `_event_log_suffix(run_dir) -> str`: the suffix `(Event_Log preserved at <path>)` is appended only when `event_log_path(run_dir).exists()`; otherwise the message ends `(the agent never started)`. Pre-agent failures at 5-base (`record_trusted_base`) or 5c (snapshot #1) therefore no longer claim an Event_Log that does not exist; the `AuthError` clause already avoids that claim (review round 4 finding 7). The same rule applies to the new `(TokenHygieneViolation, UnsafeGitConfig)` clause.
- The step-8 echo says "in the capture helper" instead of "in the Sandbox".
- The inner `finally` gains the `remove_capture_helper` backstop (D10). |
| `cli/vaultshield/teardown.py` | After `teardown_sandbox`, call `trusted_capture.remove_capture_helper(run_id, client)`, then `egress.remove_egress_resources(run_id, client)`: helper, proxy, int, open, then the `vaultshield-egress-<id>-*` staging dirs in the system temp dir (glob + `rmtree(ignore_errors=True)`), each idempotent (`NotFound` → skip). Docker refusal → `TeardownError` naming the object. |
| `cli/isolation_probe.py` | New flags (§3.2), JSON output, teardown in `finally`, cleanup line printed up front. |
| `cli/isolation_probe_net.py` (new) | The in-container probe (§3.2). |
| `cli/README.md` | Document `egress` / `--egress`, the phase model, the built-in lists and their verification status, the proxy image pull, and the token-hygiene failure. Fix L72's filename statement to "under `~/.gemini/`, filename confirmed by the probe run". |
| backend, frontend | **No change** (§1). Payloads fit the existing schema and the generic timeline. |

## 7. Error handling and teardown

Shape of `run` after the change. The parts not shown are unchanged from Batch 2.

```python
policy = _resolve_egress(agent, config, egress_mode)    # step 1: ClickException, nothing on disk;
                                                        # calls resolve_policy(..., app_port=config.port) (port-collision check)
...                                                       # 2, 2b unchanged
secrets = SecretSet()
try:
    sandbox_env = ...; adapter = ...; client = docker.from_env(); sandbox = Sandbox(run_id, client)
    guard = EgressGuard(run_id, client, policy, app_port=config.port)
    base = split = None; caps = TrustedCaptureCaps()      # module-level cap constants
    try:
        guard.start()                                     # 5a  (EgressError)
        sandbox.create(..., publish_port=None if guard.active else config.port,
                       **guard.sandbox_create_kwargs())   # 5
        guard.isolate(sandbox)                            # 5   (EgressError)
        base = record_trusted_base(sandbox, caps=caps)    # 5-base (GitCaptureError); Docker API only, before any agent code;
                                                          # HEAD/ref validated, objects snapshotted here (§5.4.1)
        if interactive_auth: _authenticate_in_sandbox(...)        # 5b
        snap1 = _take_snapshot(sandbox, adapter, secrets, run_dir, None)  # 5c: defines the credential file set (§5.5)
        log_path = supervise_in_sandbox(...)              # 6
        _take_snapshot(sandbox, adapter, secrets, run_dir, snap1.credential_files)  # #2a
        screenshots = start_and_capture(..., published_by=guard.port_publisher)  # 7
        kill_sandbox(sandbox)                             # 8 step 0: stop app + agent leftovers; Docker API only from here
        _take_snapshot(sandbox, adapter, secrets, run_dir, snap1.credential_files)  # #2 (authoritative; after step 7, before the archive)
        # _take_snapshot = snapshot(sandbox, adapter.credential_paths(), credential_files=...), then
        # secrets.add(snap.secrets), then the skipped-files event if snap.skipped > 0 (review round 4 findings 1-2)
        capture = TrustedCapture(run_id, client, image_id=sandbox.container.attrs["Image"])  # 8: trusted git (§5.4.1)
        try:
            capture.start()                               # create, start, git >= 2.32 check
            stream, _stat = sandbox.container.get_archive(REPO_DIR)  # works on the exited container
            split = split_archive(io.BufferedReader(_ChunkReader(stream, caps.stream)), caps=caps)  # safe filter, caps
            _append_event_log(event_log_path(run_dir), notices_to_events(split.notices))  # nested .git, non-failing
            capture.load(split, base)                     # init, 5-base objects, config check, cat-file -e/rev-list, detached base
            branch = commit_run_branch(capture, run_id)   # unchanged bodies, helper as exec target
            base_ref = resolve_diff_base(capture, branch)
            diff_file = build_structured_diff(capture, base_ref, branch, run_dir)
            check_run_outputs(capture, secrets, run_dir, branch, base_ref)  # (TokenHygieneViolation)
        finally:
            capture.remove()                              # never raises; one warning
            if split is not None: split.close()           # spools seek(0)'d before each put_archive, closed here
    except EgressError as exc: raise click.ClickException(f"Run {run_id} failed: {exc}") from exc
    except (TokenHygieneViolation, UnsafeGitConfig) as exc:
        raise click.ClickException(f"Run {run_id} failed: {exc} {_event_log_suffix(run_dir)}") from exc
    # _event_log_suffix(run_dir): "(Event_Log preserved at <path>)" if event_log_path(run_dir).exists(),
    # else "(the agent never started)"; also used by the existing `except GitCaptureError` clause (round 4 finding 7)
    ...                                                   # existing clauses unchanged
    finally:
        try:
            sandbox.teardown(); click.echo(...)          # 9a  unchanged
        finally:
            _remove_capture_helper_best_effort(run_id, client)  # 9a' backstop (§6 cli.py); no-op if step 8 removed it
            if base is not None: base.close()            # 5-base object spool; idempotent
            guard.teardown(run_dir)                      # 9b  logs→events, proxy, int, open; never raises
    ...                                                  # 10 upload_run(..., scrub=secrets.scrub), 11 complete
except BaseException:
    _upload_events_best_effort(run_id, backend_url, token, run_dir, secrets)  # never raises
    _mark_run_failed_best_effort(run_id, backend_url, token)
    raise
```

Notes:
- `guard` is created before the inner `try`, and `EgressGuard.__init__` makes no Docker call, so `guard.teardown` is always bound. It is safe after a partial `start()`: each object is removed only if it was created or can be found by name.
- `UnsafeGitConfig` is a `GitCaptureError` subclass, so its clause comes **before** the existing `except GitCaptureError`.
- A `Sandbox.create` failure: `_cleanup_after_failure` removes the container, a `SandboxError` propagates, the `finally` runs (`sandbox.teardown()` is a no-op, then `guard.teardown` removes the proxy and networks), and the outer handler runs. A test covers this.
- `guard.teardown` failures print `Warning: could not remove <object> for run <id>: <ExcType>` on stderr, at most once per object. The exception text is omitted, so the line stays single and token-free. `vaultshield teardown <run_id>` removes leftovers.
- Network removal waits for detachment. After `sandbox.teardown()` and the proxy removal no endpoints remain, so `network.remove()` does not hit "active endpoints". If it does, the warning names the network, and `teardown <id>` retries.
- `_upload_events_best_effort` is skipped when `events.jsonl` does not exist. It wraps `uploader.upload_events_best_effort` in `try/except BaseException`, exactly like `_mark_run_failed_best_effort`, and prints at most one `Warning: could not upload the timeline of run <id>: <warning>` line.
- Ctrl-C during the proxy wait or the switch is handled by the same path. A second Ctrl-C during best-effort steps is swallowed, as in Batch 2.
- New exception messages never include proxy log lines, needles, or env values.
- Trusted-capture errors (`GitCaptureError` from `record_trusted_base`, `split_archive`, `capture.load` or a size cap) reach the existing `except GitCaptureError` clause. That clause (and the token-hygiene clause) append `(Event_Log preserved at …)` only if `event_log_path(run_dir).exists()`, else `(the agent never started)`, so a 5-base or 5c failure does not point at a missing file (review round 4 finding 7). `UnsafeGitConfig` reaches the earlier token-hygiene clause. Both go through the normal failed path. A failing `capture.remove()` never masks them; it adds at most one `Warning: could not remove the capture helper for run <id>: <ExcType>` line. If the Sandbox itself is gone when step 8 starts, `kill()` or `get_archive` raises `NotFound`, which becomes `GitCaptureError("trusted capture: the Sandbox container is gone")`.
- Step 8 starts with `kill_sandbox(sandbox)` (review round 3 finding 2): the step-7 app and any agent background process stop, so the work-tree stream cannot be truncated by a growing file and nothing can change between snapshot #2 and the archive. No exec runs in the Sandbox after this point. `sandbox.teardown()` (`remove(v=True, force=True)`) handles the stopped container unchanged. A truncated or malformed tar → `GitCaptureError("trusted capture: the Sandbox archive stream was truncated or malformed")`.
- Docker-level errors anywhere in trusted capture or `token_hygiene.snapshot` (image lookup, `kill`, helper `create`/`start`/`put_archive`/`exec_run`, non-`NotFound` `get_archive` errors, `requests` errors mid-stream) are mapped per §5.4.1 "Docker exception mapping" to `GitCaptureError` (or `SandboxError` for `TrustedCapture.exec`), so they reach the existing `except GitCaptureError` / `except SandboxError` clauses and the user sees one `ClickException` line, never a docker-py traceback (review round 3 finding 4).
- `record_trusted_base` runs in off mode too. A `NotFound` there means the clone has no `.git`, which `create` already rules out. It is still reported as `GitCaptureError`, never swallowed.

## 8. Verification plan

### 8.1 Unit tests (no Docker)

- **`cli/tests/test_egress.py` (new)**
  - **Parsing.**
    - Accepted: exact name, uppercase → lowercase, trailing dot stripped, `:8443`, default port 443, `*.example.com`, a global IPv4 literal, `[2001:4860:4860::8888]:443`.
    - Rejected: `https://x.com`, `x.com/path`, `u@x.com`, `*`, `*.com`, `a.*.b.com`, `10.0.0.1`, `192.168.65.254`, `169.254.169.254`, `[::1]`, `[fc00::1]`, ports `0`/`65536`/`abc`, an empty entry, a 254-character name, a 64-character label, an entry with whitespace.
  - **Matching.** Exact match. Wildcard at depth 1 and 2. A wildcard does not match its apex. `badexample.com` does not match `*.example.com`. Port mismatch. Case and trailing dot on the target. An IP target matches only an equal IP entry, and a name entry never matches an IP target. IPv4-mapped IPv6.
  - **Address policy.** Every range in D5 is denied, including `100.64.0.1` and `::ffff:10.0.0.1`. `8.8.8.8` and `2001:4860::8888` are allowed.
  - **`decide` with a recording fake resolver.**
    - A non-allowlisted name is denied with `not_in_allowlist`, **and the resolver is never called** (proxy-side DNS exfil closed).
    - An allowlisted name resolving to `192.168.65.254` → `private_address`.
    - An allowlisted name with mixed public and private records → deny.
    - An IP literal → `ip_literal`.
    - `log-only` → `would_deny`, while the private and IP-literal rules still deny.
  - **Request parsing.** CONNECT ok. `GET http://…` → `plain_http`. A missing port. Headers over 8 KiB → `bad_request`.
  - **SNI parser.** A real ClientHello generated on the host with `ssl.MemoryBIO` and `server_hostname="oauth2.googleapis.com"` → that name. No SNI → `None`. Truncated or garbage bytes → `None`.
  - **Proxy server in a thread on 127.0.0.1**, with a fake resolver and `address_policy=lambda a: True` to allow the local upstream echo server:
    - an allowed CONNECT relays bytes both ways;
    - a denied CONNECT gets 403 and produces exactly one JSON `deny` line;
    - the forwarder relays to a local target.
  - **`proxy_lines_to_events`.** The exact payload strings from §5.3. The `ts` is preserved and parseable by `_timestamp_sort_key`. Non-printable host characters are sanitised. The payload uses `raw_host`, not `host` (a line with `host="abc.example.com"`, `raw_host="AbC.example.com"` gives `AbC.example.com`), and `SecretSet.scrub` applied to the payload replaces a mixed-case needle embedded in `raw_host` (e.g. `<Needle>.example.com` → `***.example.com`) (review round 4 finding 9). `allow`/`ready`/garbage lines are ignored. 250 denials → 200 events plus one summary.
  - **`resolve_policy`.**
    - Default `off`; config mode; CLI override wins.
    - YAML `False` → `off`; unknown mode or key → error.
    - Union with built-ins.
    - Empty effective list for antigravity → error; mock → deny-all accepted.
    - App port (review round 4 finding 4): `allowlist` with `app_port=443` and with `app_port=3128` → `EgressConfigError` naming the port; `allowlist` with `app_port=3000` accepted; `off` with `app_port=443` accepted.
  - **`EgressGuard` with a fake Docker client that records calls.**
    - Off mode makes zero calls.
    - `start` creates open, int, proxy (with the §5.2 kwargs, including `read_only=True` and the `/opt/vaultshield` read-only bind mount of a staging dir that holds exactly `egress_proxy.py` and `egress.json`), connects int with the alias, starts, then waits for `ready`. **No `put_archive` call is recorded.** A missing `ready` → `EgressError`.
    - `isolate` calls `connect(int)` before `disconnect(open)`. A failing assertion exec → `EgressError`. The assertion exec's argv ends with the probe name.
    - `exec_env_defaults` sets `NO_PROXY`/`no_proxy` to exactly `localhost,127.0.0.1,::1`.
  - **`isolation_probe_name`.** Allowlist without any `example.*` → `example.com`. `example.com` allowlisted → `example.net`. `*.example.com`-only does not match `example.com` → `example.com`. All three allowlisted → `vs-isolation-check.example.com`. `*.example.com` plus the three apexes → `vs-isolation-check.invalid`. All five candidates listed → `EgressConfigError`.
    - `teardown` order is logs → proxy → int → open → staging dir removed. Each removal raising `APIError` gives one warning, and `teardown` still returns normally.
- **`cli/tests/test_token_hygiene.py` (new)**
  - Needle extraction: whole file (only ≤ 8 KiB; a 9 KiB selected file gives token-shaped runs but no whole-file needle), token-shaped runs, URLs and short strings excluded.
  - **Credential file set (review round 4 finding 1).** Snapshot #1 from a fake archive holds `antigravity-cli/token` (a sentinel). Snapshot #2, called with #1's `credential_files`, additionally holds `antigravity-cli/brain/conv.json` containing a 40-character mixed-case-and-digit run, `tmp/abc/logs.json` with another such run, and a new `antigravity-cli/oauth_creds.json` with a third. The conv.json run and the tmp run, both copied into a `diff.json` patch → **no** finding; the sentinel copied into the patch → `credential_bytes`; the `oauth_creds.json` run copied into the patch → `credential_bytes` (new credential-named file outside excluded dirs is selected). `Snapshot.credential_files` from #1 is exactly `{"antigravity-cli/token"}`-equivalent raw names.
  - **Snapshot limits (review round 4 finding 2).** A selected 2 MiB member → `skipped == 1`, no exception, and `skipped_snapshot_event(1)` gives the exact payload `token hygiene: 1 credential-root files not scanned for needles (over 1 MiB or past the 8 MiB cap)`. Nine 1 MiB (exactly `1 << 20` bytes) selected members → the members past 8 MiB are skipped and `skipped` equals that count, named in the event. A stream over the (test-shrunk) 64 MiB stream cap → `GitCaptureError` starting `token hygiene: credential snapshot failed: `. A `test_cli.py` case asserts the event is appended once when `skipped > 0` and not at all when it is 0.
  - `snapshot` from a fake `get_archive` tar stream, plus `NotFound` → empty.
  - `check_run_outputs` finds a needle in a `diff.json` patch, in a `path`, in an event payload, and in a changed file's blob read via a fake `capture.exec` answering `diff --raw -z` and `cat-file blob <oid>` (no `get_archive` is called). A raw record for a non-UTF-8 path `b"\xff.txt"` is read by oid and its finding is named `\ufffd.txt`. A deleted file (all-zero new oid) and a `120000`/`160000` new mode are not read as blobs in (c). A malformed raw record → `GitCaptureError`. It finds an absolute or escaping target in an added or retargeted `120000` raw record (read by `cat-file blob <new oid>`), and makes **no** `ls-tree` call, so an unchanged escaping symlink tracked by the base yields no finding.
  - `snapshot`: a fake `get_archive` raising `APIError` (500), and a chunk iterator raising `requests.ConnectionError` mid-stream → `GitCaptureError("token hygiene: credential snapshot failed: …")`.
  - On a finding: events are written, `diff.json` is renamed, and the needle is in neither `str(exc)` nor the events file.
  - `scrub` replaces needles with `***`. `repr(SecretSet)` shows counts only.
- **`cli/tests/test_git_capture.py`** (real local git through `LocalGitSandbox`; the stand-in gains `env=None`, merged into its subprocess env, and `CannedSandbox` gains `env=None`; argv expectations are updated to the `_git` prefix, which is an exact update, not a weakened assertion):
  - (a) `core.worktree=<parent>` with a secret file outside → the branch's `ls-tree -r` lists only in-repo paths.
  - (b) A `filter.x.clean` defined in `.git/config` and used in `.gitattributes` → `UnsafeGitConfig` before staging; the filter's marker file never appears.
  - (c) A `.git/hooks/post-checkout` that writes a marker → no marker.
  - (d) A `$HOME/.gitconfig` with `core.excludesFile` hiding a file → the file is still committed.
  - (e) `.git` replaced by a gitfile → `UnsafeGitConfig`.
- **`cli/tests/test_trusted_capture.py` (new, no Docker).** Tars are built in memory with `tarfile`. The fake `get_archive` returns `(iter(chunks), stat)` like docker-py.
  - **`split_archive` rejections.** Each of these raises `GitCaptureError` naming the member:
    - an absolute name, a `repo/../x` name, a NUL, a name outside `repo/`, a duplicate;
    - a CHR, BLK or FIFO member;
    - a hardlink to an unknown or later member;
    - a work-tree hardlink `repo/x` whose `linkname` is `repo/.git/objects/ab/<38hex>` (target not in the work-tree tar);
    - a hardlink whose `linkname` is absolute (`/root/.gemini/x`) or contains `..`;
    - a member under an accepted symlink (`repo/link` → `sub`, then `repo/link/x`).
  - **`split_archive` acceptance.**
    - A hardlink `repo/b` → earlier regular `repo/a` is emitted with `linkname == "repo/a"`.
    - A setuid `0o4755` file is emitted with mode exactly `0o755`; a `0o4644` file with `0o644`; a `0o000` file with `0o644`; a `0o000` directory with `0o755`; a symlink with `0o777`; all with `uid == gid == 0`.
    - Nested `.git`: from `repo/sub/.git/HEAD`, `repo/sub/.git/config` and `repo/sub/secret.txt`, only `repo/sub/secret.txt` is emitted, and `split.notices` has exactly one entry for `sub`; `notices_to_events` gives the exact §5.4.1 payload. A top-level `repo/.git` is not a notice.
    - `_ChunkReader`: chunks of uneven sizes reassemble byte for byte; exceeding the cap raises `GitCaptureError` with the measured count from inside `readinto`.
    - A non-UTF-8 name (`b"repo/\xff.txt"`) round-trips byte for byte.
    - An in-repo relative symlink is kept.
    - An absolute symlink and an escaping `../../root/.gemini` symlink are **emitted as symlinks** with their targets unchanged, and `split_archive` raises nothing for them (the finding comes from §5.5 (d)). A member under either link is still rejected.
    - The output tars are `PAX_FORMAT`: reading the emitted work-tree tar back with `encoding="utf-8", errors="surrogateescape"` yields the name bytes `b"repo/\xff.txt"`.
  - **Archive root and `.git` rules** (review round 3 finding 1):
    - The root member `repo` (a directory) is accepted; `repo` as a symlink or a regular file → `GitCaptureError("… repo directory is not a directory")`.
    - A stream with no `repo/.git` member → `UnsafeGitConfig("… has no .git directory")`.
    - No `repo/.git/config` → `split.untrusted_config is None` and, in the `TrustedCapture` sequence test, no config `put_archive` and no `git config --file` exec. `repo/.git/config` as a symlink, a hardlink or a directory → `UnsafeGitConfig`.
    - **Config spool format (review round 4 finding 8).** `split.untrusted_config`, read back with `tarfile.open(fileobj=…, mode="r")`, is PAX format with exactly one member `untrusted-config`: regular, mode `0o644`, `uid == gid == 0`, content equal to the archived config bytes.
    - **Check 2 exit codes (review round 4 finding 3).** With a fake helper exec answering `git config --file /tmp/untrusted-config --get-regexp …`: exit 3 → `UnsafeGitConfig("… could not be parsed (git config exit 3)")`; exit 0 with `filter.x.clean cat` → `UnsafeGitConfig("… defines a filter or diff driver")`; exit 1 → OK; exit 0 with empty stdout → OK.
  - **Malformed stream** (finding 2): a tar cut mid-member and a garbage header → `GitCaptureError("… truncated or malformed")`.
  - **`.git` handling.**
    - `repo/.git` as a regular file or a symlink → `UnsafeGitConfig`.
    - From a post-agent `.git` holding `config`, `hooks/post-checkout`, `index`, `info/exclude`, `refs/replace/<oid>`, `objects/info/alternates`, `objects/info/http-alternates`, `objects/ab/<38hex>` and `objects/pack/pack-<hex>.pack/.idx`, **nothing** reaches the work-tree tar and only `config` reaches `untrusted_config` (objects come only from 5-base).
  - **Caps.** Each one raises with the measured value in the message: stream bytes, member count, a single member, a 65 KiB `.git/config`. Tests use small caps passed via `caps=`.
  - **`record_trusted_base`** from fake archives:
    - symbolic HEAD + loose ref; symbolic HEAD + `packed-refs` only (with `#` header and `^` peel lines); detached oid; unborn (no ref anywhere) → `None`.
    - objectformat grammar: `[extensions]\n\tobjectFormat = sha256` → 64-hex; `[Extensions]` header case; two `objectformat` lines, last wins; the key in another section is ignored; no key → `sha1`; `objectformat = md5` → `GitCaptureError`.
    - Garbage HEAD, an over-4 KiB HEAD, a ref that is a symlink → `GitCaptureError`. Each of these HEAD refs → `GitCaptureError` **and no `get_archive` call for the ref path is made**: `refs/heads/../../../../root/.gemini/x`, `refs/heads/a//b`, `refs/heads/.hidden`, `refs/heads/a.lock`, `refs/heads/a@{1}`, `refs/heads/a/`, `refs/heads/a.`, `refs/tags/v1`.
    - Object snapshot: from a fake `.git/objects` archive holding loose objects, a pack pair, `info/alternates` and `info/commit-graph`, `objects_tar` holds exactly the loose objects and the pack pair; the stream cap applies.
  - **`TrustedCapture.start` git version.** `git version 2.28.0` and `git version 2.31.8` → the "older than 2.32" error; `git version 2.32.0`, `git version 2.39.5` and `git version 2.45.2.windows.1`-style suffixes pass; unparsable output → error.
  - **`TrustedCapture` with a recording fake client.**
    - `containers.create` kwargs are exactly the §5.4.1 spec (`network_mode="none"`, `cap_drop=["ALL"]`, `security_opt`, limits, labels, and the image **ID**, not the tag).
    - The `exec`/`put_archive` sequence is `git version` → work-tree `put_archive` → init → `put_archive(base.objects_tar)` → config `put_archive` + check → `cat-file -e <oid>^{tree}` → `rev-list --objects <oid>` → detached HEAD `update-ref` → `read-tree`. No `fsck` call is made. Each spool's position is 0 when `put_archive` receives it. `cat-file -e` or `rev-list` exiting non-zero → the "pre-agent object snapshot is incomplete" error.
    - `remove()` raising `APIError` → one warning, no raise.
    - The image id is taken from `sandbox.container.attrs["Image"]`; the fake container's `.image` property raises if touched.
    - The sequence includes the two `_assert_capture_repo_is_safe` commands (`rev-parse --absolute-git-dir`/`--show-toplevel` and the `config --get-regexp`) run by `commit_run_branch` against the helper (review round 3 finding 9).
  - **Docker exception mapping** (review round 3 finding 4), one case per call site with a fake client raising `docker.errors.APIError`: `record_trusted_base`'s `get_archive` (non-`NotFound`), `kill_sandbox` (non-409), step-8 `get_archive`, a `requests.ConnectionError` from the chunk iterator, helper `containers.create`, `start`, `put_archive` → each a `GitCaptureError` starting `trusted capture: <step> failed: APIError`; helper `exec_run` → `SandboxError("trusted capture exec failed for run …")`. `kill_sandbox` with a 409 "is not running" → no error.
- **`cli/tests/test_cli.py`**
  - Trusted capture order: `record_trusted_base` runs after `create` (and after `isolate` in allowlist mode) and **before** `_authenticate_in_sandbox`. Snapshot order: #1 after auth, #2a after `supervise_in_sandbox`, then `start_and_capture`, then `kill_sandbox`, then #2, then the step-8 `get_archive(REPO_DIR)`; no `sandbox.exec` call is recorded after `kill_sandbox`. A `GitCaptureError` from the Docker exception mapping surfaces as one `ClickException` line with exactly one `mark_run_failed`. In step 8, the three `git_capture` calls receive the `TrustedCapture`, never the `Sandbox`. `capture.remove()` runs before `sandbox.teardown()`, and `_remove_capture_helper_best_effort` runs again (no-op) before `guard.teardown`; if `remove_capture_helper` raises there, exactly one warning line is printed and the original outcome is unchanged. A `GitCaptureError` from `split_archive` → exactly one `mark_run_failed` and the original message.
  - `--egress` choices.
  - Off mode: `Sandbox.create` kwargs and `docker.from_env` usage are identical to today.
  - Allowlist: call order `guard.start` → `create` → `isolate` → auth → supervise.
  - A failure injected at `guard.start`, `isolate`, `check_run_outputs` or `guard.teardown` → exactly one `mark_run_failed`, after `sandbox.teardown` and `guard.teardown`, and the original message surfaces.
  - The events upload runs before the failed PATCH. A raising events upload does not change the error or exit code.
  - A token violation → `upload_run` is not called.
  - **Event_Log suffix (review round 4 finding 7).** A `GitCaptureError` from `record_trusted_base` (5-base, no `events.jsonl` yet) → the `ClickException` ends with `(the agent never started)` and does not contain `Event_Log preserved`; a `GitCaptureError` at step 8 (after `supervise_in_sandbox` wrote the Event_Log) → ends with `(Event_Log preserved at <path>)`.
  - **Port collision (review round 4 finding 4).** `run --egress=allowlist` with `port: 443` in `.vaultshield.yml` → `ClickException` with the collision message, no Run directory content beyond step 1, and no Docker client call.
- **`cli/tests/test_config.py`**: the `egress` key cases.
- **`cli/tests/test_browser_capture.py`**: `resolve_base_url` with `published_by`, and no IP fallback in that case.
- **`cli/tests/test_teardown.py`**: the capture helper, proxy and networks are removed by their derived names, in that order; absent objects are a no-op; a Docker refusal → `TeardownError`. A `vaultshield-egress-<id>-abc123` dir created under a monkeypatched `tempfile.gettempdir()` is removed, and a `vaultshield-egress-<other-id>-x` dir is left alone (review round 4 finding 10).
- **`cli/tests/test_interactive_auth.py`**: `credential_paths() == ("/root/.gemini",)`, and the root is outside `REPO_DIR`.

### 8.2 Docker-gated integration (`cli/tests/test_egress_docker.py`, new)

These tests reuse `requires_docker`, `requires_sandbox_image`, `served_repo` and `_offline_backend` from `test_sandbox_execution.py`. Two new markers are added: `requires_proxy_image` (the image is present or can be pulled, otherwise skip with the reason) and `requires_internet` (a 2 s host TCP connect to `1.1.1.1:443`).

1. **`test_a_blocked_connection_is_blocked_and_recorded`.** Allow list `["example.com"]`.
   - Clone `served_repo` on the open network. This proves phase separation works with the host git daemon.
   - Isolate, then run in the Sandbox: CONNECT `example.org:443` → 403; CONNECT `1.1.1.1:443` → 403; CONNECT `host.docker.internal:8000` → 403.
   - Direct `1.1.1.1:443`, UDP `8.8.8.8:53`, IPv6 → unreachable.
   - A random-label `getaddrinfo` → `gaierror`.
   - The int gateway on 8000 and 27018 → not open.
   - Raw ICMP socket → `PermissionError`.
   - Loopback bypass: `exec_detached(["python3", "-m", "http.server", "8765", "--bind", "127.0.0.1"])`, then `exec_stream` with the guard's `exec_env_defaults` runs `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/` → `200`, and the proxy log has no line for `127.0.0.1` (D11 `NO_PROXY`).
   - The post-switch check passed even though `example.com` is allowlisted (it probed `example.net`, §5.1 step 4).
   - Then `guard.teardown(run_dir)`: `events.jsonl` has the exact `example.org` violation line, and no object labelled with the run id remains.
2. **`test_an_allowlisted_host_is_reachable`** (`requires_internet`). CONNECT `example.com:443` → 200, then a TLS handshake with SNI `example.com` succeeds.
3. **`test_dns_exfil_is_blocked`.** The Sandbox's resolver fails for a random label. A CONNECT to that label is denied with `not_in_allowlist`, and the proxy log shows no `allow` line for it. The unit test proves no resolver call.
4. **`test_the_app_port_is_reachable_through_the_proxy`.** `exec_detached(["python3", "-m", "http.server", "8000"])` in the isolated Sandbox, then `resolve_base_url(..., published_by=guard.port_publisher)` and host `httpx.get` → 200.
5. **`test_run_with_egress_allowlist_end_to_end`.** `vaultshield run --agent=mock --egress=allowlist` through `CliRunner`, mirroring `test_agent_executes_inside_the_sandbox_and_cannot_reach_the_host`. Expect exit 0, the Event_Log written, and no leftovers.
6. **`test_token_hygiene_vectors_in_a_real_container`.** Write a sentinel token to `/root/.gemini/antigravity-cli/antigravity-oauth-token`, then take snapshot #1 (it defines the credential file set, §5.5).
   - **Capture path (review round 4 finding 5).** Every vector runs through a shared module-level helper `_run_trusted_step8(sandbox, client, run_dir, secrets, base) -> (branch, base_ref)` in `test_egress_docker.py`, which performs exactly the §7 step-8 sequence: `kill_sandbox`, snapshot #2 (with snapshot #1's credential file set), `TrustedCapture(...).start()`, `get_archive(REPO_DIR)`, `split_archive`, notices → events, `capture.load(split, base)`, `commit_run_branch(capture, …)`, `resolve_diff_base(capture, …)`, `build_structured_diff(capture, …)`, `check_run_outputs(capture, …)`, with `capture.remove()`/`split.close()` in `finally`. Test 7 uses the same helper. Nothing calls `commit_run_branch(sandbox, …)`. `base` comes from `record_trusted_base` right after the clone. Each vector uses its own Sandbox (the helper kills it).
   - (1) `cp` it into `repo/leak.txt` → `TokenHygieneViolation`.
   - (2) **The Sandbox's `core.worktree` has no effect on trusted capture.** `git config core.worktree /root` in the Sandbox repo, then the helper. Assert: the branch's `ls-tree -r --name-only` in the helper lists no path outside the repo; there is no finding; and `git config --get core.worktree` in the helper repo exits 1. The test prints the image's git version (`git version` in the helper) into the `-rs`/log output; ≥ 2.32 is enforced by `TrustedCapture.start`.
   - (3) `ln -s /root/.gemini/... repo/link` → `symlink_outside_repo` (from the added `120000` raw-diff record). Counter-case: a base repo served by the new fixture **`served_repo_with_tracked_symlink`** that already tracks `docs/latest -> /usr/share/doc/x`, unchanged by the "agent" → no finding, and the branch has no deletion of `docs/latest` (review round 3 finding 6). The fixture follows the `served_repo` pattern (`test_sandbox_execution.py` L181-230: temp source repo, `git daemon` on a free port, `host.docker.internal` URL), as a `served_repo` factory taking an `extra_setup(source)` callback; its callback runs `os.symlink("/usr/share/doc/x", source / "docs" / "latest")` (after `mkdir docs`; works on APFS) before the initial commit. `served_repo` itself keeps its current content and behaviour.
   - (4) `leak.txt` listed in `.gitignore` → not committed, not in `diff.json`, no finding.
7. **`test_a_tampered_in_container_git_cannot_hide_a_file_from_the_branch_or_diff`** (`requires_docker`, `requires_sandbox_image`; offline, egress `off`, so it also proves trusted capture is always on).
   - **Set up a real Sandbox.** Clone `served_repo`, then call `record_trusted_base`.
   - **Tamper as root, playing the agent** (steps that need git, such as `update-index`, `git init sub` and writing the replace ref, run with the real git before the wrapper is installed last).
     - Replace `/usr/bin/git` with a shell wrapper. It writes `/tmp/fake-git-ran`. For `add` it stages nothing (`exit 0`); for `diff` and `ls-tree` it pipes the real git's output through `grep -v hidden`.
     - Write `repo/hidden.txt` containing a sentinel token that is also written to `/root/.gemini/antigravity-cli/antigravity-oauth-token` and snapshotted.
     - Modify a tracked file `README` and mark it `git update-index --assume-unchanged`.
     - Add `hidden.txt` to `.git/info/exclude`.
     - Write `.git/objects/info/alternates` pointing at `/tmp/evil-objects`.
     - Add a `refs/replace/<base-oid>` pointing at a forged commit, whose object is written into the post-agent `.git/objects`.
     - Delete one loose object or pack of the base from the post-agent `.git/objects` (it is still in the 5-base snapshot).
     - Set `.git/hooks/post-checkout` to write a marker.
     - `git init sub` and write `sub/secret.txt` (nested repository).
   - Start a background writer in the Sandbox (`exec_detached`) that keeps appending to `repo/.cache/grow.log`, standing in for the step-7 app.
   - **Run step 8's sequence** exactly as `cli.py` does, through the shared `_run_trusted_step8` helper (test 6): `kill_sandbox`, snapshot #2, `TrustedCapture`, `get_archive(REPO_DIR)`, `split_archive`, `load`, the three `git_capture` calls, `check_run_outputs`. Asserts that the Sandbox's `State.Status` is `exited` after `kill_sandbox` and that **`get_archive` on the exited container succeeds on Docker 29.2.1** (review round 3 finding 2; the version is printed into the log).
   - **Assert:**
     - `/tmp/fake-git-ran` does **not** exist in the Sandbox (no in-container binary ran during capture); checked with `get_archive("/tmp/fake-git-ran")` → `NotFound`, since the Sandbox is stopped.
     - Capture succeeded although the background writer was growing `repo/.cache/grow.log` until `kill_sandbox` (no truncated stream).
     - The branch's `ls-tree -r` in the helper lists `hidden.txt`, the modified `README` and `sub/secret.txt` as a regular blob (no `160000` gitlink), and `diff.json` has an entry for `sub/secret.txt`; `events.jsonl` has the `nested git repository at sub` violation.
     - The branch's parent is the recorded base oid, not the replace target; the forged object is absent from the helper's store (`cat-file -e` fails), and capture succeeded despite the deleted base object (objects came from 5-base).
     - `diff.json` has entries for both files.
     - `check_run_outputs` raises `TokenHygieneViolation` with a `credential_bytes` finding for `hidden.txt`, and the sentinel is in no message or event.
     - No hook marker exists in either container.
     - After `capture.remove()`, no `vaultshield-capture-<id>` container remains.
   - **Second case, same file:** no sentinel in `hidden.txt` → no finding. The file is still in the branch and in `diff.json`, which proves the tamper cannot *hide* a non-secret change either.
8. **`test_trusted_capture_preserves_non_utf8_and_case_variant_names`** (`requires_docker`, `requires_sandbox_image`). **Drives the real `vaultshield run --agent=mock` through `CliRunner`** (review 2 finding 13), mirroring `test_agent_executes_inside_the_sandbox_and_cannot_reach_the_host` and `_offline_backend`, so the real step 8 and `check_run_outputs` run. Test seam 1 (both cases): `monkeypatch` wraps `cli.supervise_in_sandbox` so that, before delegating to the real function, it does one in-container Python exec (`sandbox.exec(["python3", "-c", …])`) that writes `os.fsencode`-built `b"\xff.txt"` plus `A.txt` and `a.txt` into `/workspace/repo`.
   - **Case A (no secret):** exit 0. `diff.json` has all three entries. The `\xff` entry carries `patch_truncated: true` and no `patch` (Batch 1 behaviour), even though the host is APFS.
   - **Case B (secret in the non-UTF-8 file):** the same exec also writes a sentinel token to `/root/.gemini/antigravity-cli/antigravity-oauth-token` and into `\xff.txt`. **Second test seam** (review round 3 finding 5): `monkeypatch.setattr(Mock_Adapter, "credential_paths", lambda self: ("/root/.gemini",))` (`cli/vaultshield/adapters/mock.py`), because the base default is `()` and the mock adapter would otherwise yield an empty `SecretSet`. The sentinel is written during step 6 (inside the `supervise_in_sandbox` wrapper), so it is present for snapshot #2a and for the authoritative snapshot #2 taken at the start of step 8 after `kill_sandbox`. It is a new file at #2a/#2 (snapshot #1 saw none), and it is selected by the §5.5 basename rule (`antigravity-oauth-token` matches `token`/`oauth`; `antigravity-cli` is not an excluded directory), so it yields needles (review round 4 finding 1). Exit non-zero with the token-hygiene `ClickException`; `events.jsonl` has a `token hygiene violation: credential_bytes in … (\ufffd.txt) …` event (scan (c) read the blob by oid, §5.5); `diff.quarantined.json` exists and `diff.json` does not; the sentinel is in no output, message or event.
   - Both cases leave no `vaultshield-capture-<id>` container.

### 8.3 Commands

- CLI, full suite: `cd cli && .venv/bin/python -m pytest -q -rs --deselect tests/test_sandbox_execution.py::test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever`. Expect the 728 baseline passes plus all new tests passing, and exactly 1 failure, the pre-existing `test_exec_interactive_really_delivers_stdin_to_a_container_process`. Report the skips (`-rs`) and their reasons.
- New tests alone: `cd cli && .venv/bin/python -m pytest -q -rs tests/test_egress.py tests/test_token_hygiene.py tests/test_trusted_capture.py tests/test_egress_docker.py`.
- The existing Docker-gated end-to-end tests in `test_sandbox_execution.py` now exercise trusted capture on every `run`. Their `diff.json` assertions must pass unchanged, and they must leave no `vaultshield-capture-*` container behind. Check with `docker ps -a --filter label=vaultshield.role=capture` → empty.
- Frontend and backend are not touched, so they are not required. Optional regression runs: `cd frontend && CI=true npm test -- --ci` (60/60), and the backend with the replica set from [inv §8] (326).
- `git diff --quiet backend/security_scan/rules.py`, and `git status` showing nothing staged.
- **Developer-run, not runnable by the agent:** the two (or three) `isolation_probe.py` commands in §3.2. The final report must list them as not run until the developer pastes the JSON output, and must then fill the "After" column, the token filename and the observed agy hosts from it.

## 9. Points that change user-visible behaviour (sign-off)

1. **Default `egress: off`.** Today's behaviour stays unless the developer opts in. Alternative: default `allowlist` for `--agent=antigravity` once the log-only probe confirms the list.
2. **Egress denials never fail a Run.** They are only recorded as `violation` events. Alternative: fail on the first denial, which would make telemetry denials fatal.
3. **Failed Runs now upload their Event_Log** (best-effort, scrubbed) before the failed PATCH, so off-limits, egress and token-hygiene violations show in the dashboard timeline. Today a failed Run has an empty timeline.
4. **Token hygiene is on in every egress mode.** On a credential hit the Run fails: a `violation` event is recorded, no diff or screenshots are uploaded, and `diff.json` is renamed `diff.quarantined.json`. Alternative: redact the entry, mark it `patch_truncated: true`, and continue.
5. **A literal `/root/.gemini` or `~/.gemini/<child>` in a changed file fails the Run.** False positives are possible in a repo that documents those paths. Alternative: record it as a non-failing violation.
6. **Event payloads are scrubbed of credential needles (`***`) on upload, on every path.**
7. **Git capture ignores agent-set config.** System and global gitconfig, `core.excludesFile`, `core.attributesFile`, hooks and fsmonitor are ignored, and `core.worktree` is overridden. A filter/textconv driver in repo config fails the Run. In practice only a repo using git-lfs filters in `.git/config` would notice, and the image has no git-lfs.
8. **In `allowlist`/`log-only`:**
   - the agent cannot install new dependencies (no registry access);
   - the app at step 7 has no internet;
   - NET_RAW is dropped and IPv6 is disabled in the Sandbox;
   - screenshots go through the proxy's published port;
   - the first run pulls the pinned `python:3.11.16-slim` image (a slim Debian image; download size not measured).
9. **The built-in Antigravity list is `oauth2.googleapis.com` + `cloudcode-pa.googleapis.com` only.** `www.googleapis.com` is excluded because of the Cloud Storage upload exfil path. If the probe shows `agy` needs it, choose between adding it, with the risk stated, and leaving Antigravity unusable in allowlist mode.
10. **Residual risks accepted for this batch:**
    - hostile dependency install scripts run with open network (pre-token);
    - the container's `git` can be replaced by the agent; *(closed by the user-approved addition in item 12: the Sandbox's git no longer produces the branch or `diff.json`)*
    - encoded exfil through an allowlisted host (including Google APIs under the developer's own account) is not detectable;
    - the int-gateway → host path on Linux is not measured here.
11. **Docs:** `cli/README.md` and the Antigravity docstring stop asserting a single token filename until the probe run confirms it.
12. **Trusted capture (user-approved addition; consistent with item 10, does not change items 1-11).** In every egress mode, the run branch and `diff.json` are built by git in a fresh `vaultshield-capture-<id>` helper container (§5.4.1), not by the agent's container. User-visible effects:
    - One extra short-lived container per Run, created from the Sandbox's own image ID: no pull, no network, no capabilities. It is removed at the end of step 8, by the teardown backstop, and by `vaultshield teardown <id>`.
    - Step 8 now streams the whole Sandbox repo directory, including ignored `node_modules`, through the host once. Capture time grows with that size: seconds for typical repos (not measured).
    - A repo over 4 GiB or 1,000,000 entries (counting ignored and dropped ones), with any single file over 1 GiB, or with a git pack over 1 GiB, fails the Run with a clear message. Nothing is silently dropped.
    - Step 8 first stops (kills) the Sandbox container, so the step-7 app and any process the agent left are ended before capture rather than at teardown. Artifacts on disk are unaffected; the container is still removed at teardown as today.
    - A device file, a FIFO, a member under a symlinked directory, a hardlink outside its archive, or a pre-agent object snapshot that is incomplete for the base fails the Run. Objects the agent deletes, forges or alternates after 5-base are ignored, because the objects come from the 5-base snapshot (no `git fsck --full`, so legitimate repos with non-integrity fsck warnings are not failed).
    - Hiding tricks that worked against the in-container git no longer work: `.git/info/exclude`, `assume-unchanged`/`skip-worktree`, `refs/replace`, grafts, alternates, hooks, a replaced `git` binary, **and nested `.git` directories** (an agent-created `sub/.git` is dropped and `sub/`'s files are captured as plain files, with one non-failing `nested git repository at <path> was captured as plain files` violation event). Files hidden that way now show up in the branch and in `diff.json`. The `.gitignore` files remain honoured (today's semantics; an edit to them is itself in the diff).
    - The capture helper normalises file modes (dirs 755, files 644/755, symlinks 777), so an agent `chmod 000` cannot fail capture.
    - The Sandbox image's git must be ≥ 2.32 (checked at step 8; a configured `base_image` with older git fails the Run with a clear message).
    - An escaping symlink the Run **added or retargeted** fails the Run as `symlink_outside_repo`, as acceptance 6 specifies. An escaping symlink the base repo already tracks, left unchanged, is copied into the helper as a symlink and does not fail the Run (today's behaviour for such repos; no fake deletion in the diff).
    - A Sandbox whose `/workspace/repo` is not a directory, or has no `.git` directory, or has a non-regular `.git/config`, or a `.git/config` that git cannot parse (any `git config --file` exit other than 0/1), fails the Run (fail closed; a malformed config also fails the Run today, review round 4 finding 3).
    - The branch still does not reach the host checkout (unchanged; `submit.py` L207-224 gap).
    - Alternative: host git (option a). Rejected because APFS cannot store non-UTF-8 names and folds case, which would break Batch 1's `patch_truncated` behaviour and silently collide `A.txt`/`a.txt`.
13. **Disclosed, not closed: a token rendered into a screenshot is not detected** (review round 3 finding 7; a disclosure that adds to, and does not change, items 1-11). Step 7 runs agent-written app code as root with read access to `/root/.gemini`; on a clean scan the PNG screenshots are uploaded, and images are not scanned. In allowlist mode the proxy blocks direct exfil, but not this upload. Mitigations (OCR, or running step 7 with the credential root removed from the Sandbox) are out of scope for this batch. The final report must repeat this item.
14. **Needles come from the credential file set fixed at sign-in** (review round 4 finding 1; a refinement of how item 4 finds credential bytes, which does not change items 1-11). Snapshot #1 (post-sign-in, pre-agent) fixes the set of credential files under `/root/.gemini`. Later snapshots re-read only those files plus new files with a credential-like name (`token`, `oauth`, `credential`, `cred`, `secret`, `auth`, `session`, `*.pbtxt`, `*.json`) outside `tmp`/`logs`/`history`/`brain`/`conversations`/`cache` directories. Whole-file needles come only from files ≤ 8 KiB. Effect: agent transcripts and session state written during a Run cannot fail a legitimate Run; a token written into a new file with an unrelated name in an excluded directory is not a needle (encoded/renamed exfil is already out of scope). Credential-root files over 1 MiB, or past the 8 MiB total, are listed in one non-failing `token hygiene: <n> credential-root files not scanned …` violation event. **Before the first real Run**, the developer probe's `/root/.gemini` listing (§3.2) must be checked against this rule, and `Antigravity_Adapter.credential_paths()` then narrows to the confirmed file. Alternative: needles from every file under the root (today's design text), which risks false `credential_bytes` failures from transcripts.
15. **In `allowlist`/`log-only`, an app `port` of 443 or 3128 is refused at step 1** (review round 4 finding 4; adds a validation to item 8's mode, does not change items 1-11). These are the proxy's own SNI and CONNECT listeners. The message says to change `port` in `.vaultshield.yml` or use `--egress=off`. Off mode accepts any port, as today.

### 9.1 Real-`agy` log-only probe applied (build iteration 1)

Source: `~/.vaultshield/probes/20261009T083128Z-egress-log-only{.json,-proxy.jsonl}` (hostnames, decisions and an `lstat` listing only; no file contents were read from them).

**Item 9 — Antigravity allowlist, host by host.** Built-in list is now `oauth2.googleapis.com`, `daily-cloudcode-pa.googleapis.com`, `cloudcode-pa.googleapis.com`.

| Host seen | Decision | Reason |
|---|---|---|
| `oauth2.googleapis.com` | **allow** (kept) | OAuth sign-in / token refresh (3 allowed CONNECTs) |
| `daily-cloudcode-pa.googleapis.com` | **allow** (added) | the model API this `agy` build actually uses (11 CONNECTs) |
| `cloudcode-pa.googleapis.com` | **allow** (kept; not seen) | not contacted by this build; kept only on binary-strings evidence [inv §6] as the same API's production endpoint for other release channels. Weak evidence: flagged to the developer at the allowlist-probe pause; drop it if they prefer least privilege |
| `www.googleapis.com` | deny | userinfo, but also Cloud Storage JSON upload API (exfil path) |
| `storage.googleapis.com` | deny | Cloud Storage upload (exfil path) |
| `play.googleapis.com` | deny | Play logging/telemetry |
| `antigravity-unleash.goog` | deny | feature-flag service |
| `antigravity-cli-auto-updater-974169037036.us-central1.run.app` | deny | auto-updater (would replace the binary mid-Run) |
| `lh3.googleusercontent.com` | deny | avatar images |
| `playwright.azureedge.net`, `playwright-akamai.azureedge.net`, `playwright-verizon.azureedge.net` | deny | browser binary downloads (arbitrary code into the Sandbox) |
| `1.1.1.1`, `example.com`, `host.docker.internal` | deny | the probe's own test targets |

**Items 11 & 14 — credential files.** Confirmed token: `/root/.gemini/antigravity-cli/antigravity-oauth-token` (497 B, 0600). `credential_paths()` = that file + `antigravity-cli/jetski_state.pbtxt`. Structure of `jetski_state.pbtxt` and `config/config.json` inspected in a throwaway `--network none` container on the developer's host copies (same 518-byte `jetski_state.pbtxt`), printing field names and value shapes only: `jetski_state.pbtxt` = `post_onboarding.completed_steps`, `installation_uuid`, `migrations{key,value}` — no OAuth/JWT-shaped value; `config/config.json` = `plugins`, `userSettings` (permission grants, `remoteControlHostname`, booleans) — 0 OAuth/JWT-shaped strings. Decision: `config/config.json` excluded; `jetski_state.pbtxt` holds no credential material but stays a needle source (it was already in the set, mode 0600 and written by the sign-in; cost is one whole-file needle that agent output does not reproduce). `cache/last_conversations.json`, `cache/onboarding.json` and the `*.lock` files are excluded. `antigravity-cli/cli.log` is a symlink: never followed (regular members only; real-container test added). `brain/*/…/transcript*.jsonl`, `conversations/*.db` are not needle sources (excluded dirs; unit test on the real listing).

**§5.6 / item 10 — int-network gateway.** Closed topologically with `inhibit_ipv4` (see §5.6). Item 10's "int-gateway → host path on Linux is not measured" remains.

**Violation events.** The probe's proxy log has `would_deny` for `CONNECT example.com:443`; `proxy_lines_to_events` maps it to `egress violation (log-only, allowed): CONNECT example.com:443 not in allowlist for antigravity` (unit test over the real file).

## 10. Responses to review findings

| # | Finding | Decision | Where |
|---|---|---|---|
| 1 | HIGH: design missing | This file exists at `/Users/aayush/Codes/VaultShield/.agents/tasks/batch3-design.md`, with one section per user item: item 1 → §3, item 2 → §4 D1-D6 and §5.1-5.2/5.6, item 3 → D7/D8 and §5.3, item 4 → D9 and §5.4/5.5, item 5 → §8. | whole file |
| 2 | HIGH: internal network breaks browser capture | **Option (a).** The Sandbox stays internal through step 7. The dual-homed proxy publishes `<port>/tcp` and forwards it to `vaultshield-run-<id>:<port>`. `resolve_base_url(..., published_by=)`. Docker test 4. | D4, §5.2, §6, §8.2 |
| 3 | HIGH: phase separation not concrete | Per-phase table. `connect(int)` before `disconnect(open)`, right after `create` and before `_authenticate_in_sandbox`. Post-switch assertion (1.1.1.1 fails, `example.com` unresolvable, proxy reachable). `EgressError("egress: could not isolate sandbox network: …")` through the failed path. `postinstall` justification and residual risk. | D3, §5.1, §7, §9.10 |
| 4 | HIGH: why root cannot undo | Topological argument: no default route, no NET_ADMIN, network/cap changes need the Docker API, there is no docker.sock, `cap_drop=["NET_RAW"]` (measured `PermissionError`), proxy `ip_forward=0` (measured default `1`), and proxy env is convenience only. Raw-socket test in Docker test 1. | D2, §5.2, §8.2 |
| 5 | HIGH: DNS, IPv6, host ports, private ranges | (i) The Sandbox has no external resolver after the switch, and the proxy matches the allowlist **before** any DNS lookup. (ii) `enable_ipv6=False` on both networks plus `disable_ipv6=1`. (iii) IP-literal deny, plus deny if any resolved address is not `is_global`, covering every listed range. (iv) `host.docker.internal:8000` CONNECT → 403, asserted. | D5, §5.2, §5.6, §8.1, §8.2 test 1/3 |
| 6 | HIGH: proxy implementation, pin, match, reporting, names | In-repo stdlib proxy in `python:3.11.16-slim@sha256:e41613d4…b41e` (digest verified). Exact or leftmost-wildcard host match, port default 443, plain HTTP refused. One JSON line per decision on stdout, read with `container.logs()` before removal. Names `vaultshield-egress-<id>`, `vaultshield-int-<id>`, `vaultshield-open-<id>`. A missing image is pulled; a failed pull gives an `EgressError` with the `docker pull` hint. | D5, D6, §5.2, §5.3 |
| 7 | HIGH: token hygiene vectors, decision, filename | Vector 2: `_git` prefix, `_GIT_ENV`, `.git` gitfile/symlink check, and filter/textconv refusal before `add` (measured: `core.worktree` leak reproduced and closed). Vectors 1/3 and agent output: host-side scan of `diff.json`, `events.jsonl`, changed files and symlinks against needles from `get_archive` snapshots. Decision: fail the Run, record a violation, upload neither diff nor screenshots (alternative in §9.4). The whole `/root/.gemini` tree is guarded. Docstring and README are fixed after the probe confirms the filename. | D9, §5.4, §5.5, §8.1, §8.2 test 6 |
| 8 | MEDIUM: violations never reach the dashboard for failed Runs | **Both (a) and (b).** Denials do not fail the Run, and the failure path best-effort uploads `events.jsonl` before the failed PATCH and never masks the error. Exact payloads, `_now_iso8601`-format timestamps, and `_timestamp_sort_key` + `_append_event_log` are specified. | D8, §5.3, §7 |
| 9 | MEDIUM: config key, default, validation, merge, built-ins | `egress: {mode: off\|log-only\|allowlist, allow: [...]}` plus `--egress`. Default `off`, with justification. Full grammar (wildcards, ports and global IP literals are allowed, because the user's test list names them). An empty effective list is an error except for mock (deny-all). Merge semantics: **union**. Antigravity has 2 core hosts, to be confirmed by log-only; codex and kiro lists are defined and marked unverified, not placeholders, as the step brief asks. | D7, §6 config.py |
| 10 | MEDIUM: threat check probes, handoff, safety, recording | Probe list. One copy-pasteable command per mode, which signs in and then runs the probe through Docker exec inside the signed-in container; there is no `run` flag, because the probe script owns the pause. Safety rules. Outputs under `~/.vaultshield/probes/`, including the proxy log (hosts agy contacted) and the `/root/.gemini` listing (filename). These feed D7. | §3.2, §8.3 |
| 11 | MEDIUM: teardown ordering and cleanup | Inner `finally`: Sandbox → proxy logs → proxy → int → open, all before the events upload and the failed PATCH. Each step is best-effort with one warning. `teardown.py` removes the same names derived from `run_id`. `_cleanup_after_failure` needs no change, because a `create()` failure reaches the same `finally` (explained, and tested). Mocked-client order test plus a failing-removal test. | D10, §6, §7, §8.1 |
| — | Review's "Unverified" list | Real token filename and agy hosts: produced by the §3.2 probe run (developer-run). `docker network connect` restoring published ports: moot, because the Sandbox never publishes in non-off modes (D4). Switch during `exec_stream`: moot, because the switch happens before any agent exec (measured on a running container). git honouring `core.worktree` for `add --all`: measured yes on host git and closed; the image git is checked in Docker test 6. agy honouring `HTTPS_PROXY`: no longer load-bearing, because the SNI path through `extra_hosts` covers clients that ignore it. The Dockerfile curl comment is reported, not changed. | §1, D4, §5.1, §8.2 |

### 10.2 Responses to the trusted-capture review (`batch3-design-review.md`, findings 1-13)

None of these changes alters a signed-off §9 decision 1-11. Finding 5 corrects the §9 item 12 claim (item 12 is not in the protected set).

| # | Finding | Decision | Where |
|---|---|---|---|
| 1 | HIGH: proxy `read_only=True` but loaded via `put_archive` | **Accepted, review's fix.** `read_only=True` kept. `egress_proxy.py` and `egress.json` are delivered by a read-only bind mount of a per-Run host staging dir (`tempfile.mkdtemp(prefix="vaultshield-egress-<id>-")`) at `/opt/vaultshield`. No `put_archive` into the proxy. `guard.teardown` gains step 5, `shutil.rmtree(stage, ignore_errors=True)`. The `EgressGuard` call-order unit test asserts the bind mount in the create kwargs and no `put_archive`. | D6, §5.2 proxy row, D10, §8.1 |
| 2 | MEDIUM: `fsck --full` on the post-agent store | **Accepted, review's fix.** `record_trusted_base` snapshots `.git/objects` (loose objects and pack pairs only, same filter rules) into `TrustedBase.objects_tar` at 5-base, before any agent code. Step 8 takes no objects from the post-agent archive and drops all of `repo/.git/` except `config`. `fsck --full` is replaced by `cat-file -e <oid>^{tree}` plus `rev-list --objects <oid>` (presence and connectivity). The "missing from the Sandbox's object store" error becomes "pre-agent object snapshot is incomplete". The §8.1 sequence test and §8.2 test 7 are updated: test 7 also forges an object, deletes a base object post-agent, and shows both are ignored. | §5.4.1 5-base item 4, steps 1, 4, 5, 9; §6; §7; §8.1; §8.2 test 7; §9 item 12 |
| 3 | MEDIUM: HEAD ref-name validation undefined | **Accepted.** `_checked_ref` is no longer cited. Defined `_HEAD_REF`, `_BARE_OID` and `_valid_branch_ref` (rejects `..`, `//`, `@{`, empty or `.`-leading components, `.lock` components, trailing `/` or `.`). The same rule applies to `packed-refs` lines. Unit cases include the `refs/heads/../../../../root/.gemini/x` traversal and assert no `get_archive` for the ref path. | §5.4.1 5-base items 1-2, §8.1 |
| 4 | MEDIUM: hardlink rule ambiguous | **Accepted, review's rule (i)-(iii).** `linkname` goes through the name rules and must name an earlier regular member emitted into the same **output** tar, and it is rewritten with the same prefix rule. Anything else fails with "hardlink … outside its archive". Unit cases cover a work-tree → `.git/objects` link and an absolute `linkname`. | §5.4.1 step 2 Hardlinks, §8.1 |
| 5 | MEDIUM: nested `.git` still hides content | **Accepted, the review's recommended option (capture as plain files).** Every member under `repo/<p>/.git` is dropped. One non-failing `violation` event per directory (`trusted capture: nested git repository at <path> was captured as plain files`) comes from `SplitArchive.notices`, kept separate from failing findings. The §9 item 12 claim is corrected to list nested `.git` among the closed tricks. Failing the Run was rejected because a legitimate `git init` scaffold would fail every Run. Unit case and test 7 case added. | §5.4.1 step 2, §6, §7, §8.1, §8.2 test 7, §9 item 12 |
| 6 | MEDIUM: scan (c) reads by lossy path | **Accepted, review's fix.** (c) runs `diff --raw -z --no-renames --no-abbrev base_ref branch` in the helper, then `cat-file blob <new oid>` for each `100644`/`100755` record with a non-zero oid. Findings are named by the lossy display path. No `get_archive`. A malformed record or a failing `cat-file` is a `GitCaptureError`, never a skip. The 32 MiB cap and its event are kept. Test 8 case B pins a token in `\xff.txt` → `credential_bytes`. | §5.4.1 step 8, §5.5 (c), §8.1, §8.2 test 8 |
| 7 | MEDIUM: post-switch `getaddrinfo("example.com")` contradicts tests | **Accepted.** `isolation_probe_name(policy)` picks the first unmatched of `example.com`, `example.net`, `example.org`, `vs-isolation-check.example.com`, `vs-isolation-check.invalid`, passed as `sys.argv[1]`. If all are allowlisted, `EgressConfigError` is raised at step 1. Unit cases and a test 1 sub-assertion added. | §5.1 isolate step 4, §8.1, §8.2 test 1 |
| 8 | MEDIUM: `NO_PROXY=""` | **Accepted.** `NO_PROXY`/`no_proxy` = `localhost,127.0.0.1,::1`. D2 is unaffected because loopback stays in the netns. Unit assertion added, plus a test 1 loopback `curl` sub-assertion against an in-Sandbox `http.server`. | D11, §8.1, §8.2 test 1 |
| 9 | NIT: `cap_drop=ALL` helper cannot read `0o000` files | **Accepted.** Modes are normalised: dirs `0o755`, files `0o755` if any x bit is set, else `0o644`, symlinks `0o777`. The setuid unit case asserts `0o755`, with extra cases for `0o000`. Noted in §9 item 12. | §5.4.1 step 2 Metadata, §8.1, §9 item 12 |
| 10 | NIT: stream adapter and rewinds | **Accepted.** `_ChunkReader(chunks, cap)` (`io.RawIOBase.readinto`, enforces the stream cap itself). The §6/§7 call is written in full. Every spool is `seek(0)`'d before `put_archive`, and `split.close()`/`base.close()` run in `finally`. Unit cases cover reassembly and the cap. | §5.4.1 step 1, §6, §7, §8.1 |
| 11 | NIT: stale/undefined names | **Accepted.** The §6 `token_hygiene.py` row now has the current `check_run_outputs(capture, secrets, run_dir, branch, *, extra_findings=())` signature (superseded by §10.3 #6: now `check_run_outputs(capture, secrets, run_dir, branch, base_ref)`), and the separate delta row is removed. `_remove_capture_helper_best_effort` is defined in the §6 `cli.py` row (try/except `BaseException`, one warning) and used in §7. NFR1 now excepts the always-on capture helper, matching acceptance 1. | §6, §7, NFR1 |
| 12 | NIT: objectformat grammar, git version | **Accepted.** Grammar: case-insensitive `[extensions]` header, `objectformat = <v>` with whitespace stripped and a case-insensitive key, last occurrence wins. Missing → `sha1`. Anything else → `GitCaptureError`. `TrustedCapture.start()` checks `git version` ≥ 2.29. "Debian 2.39" is replaced by **[unverified]**, and test 6 prints the image's git version. | §5.4.1 5-base item 3 and step 4, D9, §8.1, §8.2 test 6, §9 item 12 |
| 13 | NIT: test 8 path | **Accepted, the review's preferred option.** Test 8 drives `vaultshield run --agent=mock` through `CliRunner`. A monkeypatched wrapper around `supervise_in_sandbox` does one in-container exec that writes the files, so the real step 8 and `check_run_outputs` run. | §8.2 test 8 |

### 10.3 Responses to the round 3 whole-design review (`batch3-design-review.md`, findings 1-11)

None of these changes alters a signed-off §9 decision 1-11. Item 10's text is unchanged; the screenshot disclosure (finding 7) is a new item 13, and the other user-visible effects are added to item 12 (not in the protected set).

| # | Finding | Decision | Where |
|---|---|---|---|
| 1 | MEDIUM: archive root `repo`, missing `.git` / `.git/config` | **Accepted, review's rules.** The member named exactly `repo` is allowed and must be a directory, else `GitCaptureError("… repo directory is not a directory")`; every other name must start with `repo/`. No `repo/.git` directory member by end of stream → `UnsafeGitConfig("… has no .git directory")` (fail closed). `.git/config` absent → check 2 skipped (`untrusted_config is None`, no put/exec); present but not a regular file → `UnsafeGitConfig`. One unit case each. | §5.4 checks 1-2, §5.4.1 step 2 "Names" and "Archive root and `.git`", step 4, §6 trusted_capture row, §8.1, §9 item 12 |
| 2 | MEDIUM: Sandbox processes still run during the step-8 archive | **Accepted, review's fix.** New step 8 step 0 `kill_sandbox(sandbox)` (`container.kill()`; a 409 "not running" is success). Then snapshot #2, then `get_archive(REPO_DIR)`; no Sandbox exec after the kill. `teardown()` unchanged (`remove(force=True)`). `tarfile.TarError`/`EOFError` in `split_archive` → `GitCaptureError("… truncated or malformed")`. Docker-gated test 7 asserts `exited` state, a successful `get_archive` after `kill()` on 29.2.1, and capture succeeding while a background writer grew a file until the kill. | §5.1 table, §5.4.1 step 0 and step 2 "Malformed stream", §6, §7 code and notes, §8.1, §8.2 test 7, §9 item 12 |
| 3 | MEDIUM: snapshot #2 timing inconsistent, misses step 7 | **Accepted, one rule everywhere.** #1 at 5c; #2a after step 6 (kept, optional per review); **#2 at the start of step 8, right after `kill_sandbox`, before `get_archive(REPO_DIR)`**, so a token refreshed by the step-7 app is a needle. | D9, §5.1 table, §6 cli row, §7 code, §8.1 test_cli order |
| 4 | MEDIUM: Docker exceptions in trusted capture unmapped | **Accepted, review's fix.** Image id from `sandbox.container.attrs["Image"]`. New "Docker exception mapping" rule: every docker-py call and chunk iteration in `trusted_capture.py` and `token_hygiene.snapshot` wraps `DockerException`/`requests.RequestException` as `GitCaptureError("trusted capture: <step> failed: <Type>: <exc>")` (snapshot: `token hygiene: credential snapshot failed: …`); `NotFound` keeps its specific handling; `TrustedCapture.exec` wraps as `SandboxError`, like `Sandbox.exec`. All reach existing `cli.py` clauses. Unit case per call site with a fake client raising `APIError`. | §5.4.1 "Docker exception mapping", step 8 constructor, §6, §7 notes, §8.1 |
| 5 | MEDIUM: test 8 Case B has no needles with `--agent=mock` | **Accepted.** Second test seam: `monkeypatch.setattr(Mock_Adapter, "credential_paths", lambda self: ("/root/.gemini",))`. The sentinel is written inside the `supervise_in_sandbox` wrapper (step 6), so it is present for #2a and for #2 at step 8. | §8.2 test 8 |
| 6 | MEDIUM: tracked escaping symlink fails every Run and fakes a deletion | **Accepted, the review's preferred fix.** All symlinks, escaping ones included, are copied into the helper as symlinks (never followed; the helper holds no secrets; the symlinked-parents rule still applies). `symlink_outside_repo` comes only from §5.5 (d) on `diff --raw` records with new mode `120000` (added or retargeted), target read by `cat-file blob <new oid>`. The whole-tree `ls-tree` scan is dropped, and so are `SplitArchive.findings` and `check_run_outputs`' `extra_findings` (it now takes `base_ref`, which (c) already used). Unit cases updated; Docker test 6 gains a tracked-escaping-link counter-case. Acceptance 6 (an agent-created link to `/root/.gemini/...` fails the Run) is unchanged. Recorded under §9 item 12, not item 4/5. | §5.4.1 step 2 "Symlinks", step 8, §5.5 (d), §6, §7, §8.1, §8.2 test 6, §9 item 12 |
| 7 | MEDIUM: screenshots are an unlisted token leak path | **Accepted as a disclosure.** New §1 row (5th leak vector, not closed), an out-of-scope line, and **§9 item 13** ("a token rendered into a screenshot is not detected"), which the final report must repeat. Placed as item 13 rather than a bullet in item 10, so the signed-off text of item 10 is unchanged; no behaviour changes. OCR or running step 7 without the credential root is out of scope. | §1, §2 Out of scope, §9 item 13 |
| 8 | NIT: stale `record_trusted_base` signature; `KEEP_ALIVE_CMD` alias | **Accepted.** §6 cli row uses `record_trusted_base(sandbox, caps=caps)`. The `sandbox.py` row adds `KEEP_ALIVE_CMD = _KEEP_ALIVE_CMD`; the helper spec and the trusted_capture row use the public name. | §5.4.1 helper spec, §6 |
| 9 | NIT: is `_assert_capture_repo_is_safe` called in `run`? | **Accepted.** It stays the first step of `commit_run_branch`, so it also runs harmlessly against the helper's repo; the untrusted-archive checks are additional. The §8.1 helper exec-sequence test includes its two commands. | §5.4, §6 git_capture row, §8.1 |
| 10 | NIT: cap semantics | **Accepted, review's wording.** Each limit applies separately per stream; the stream limit counts every tar byte read; the member limit counts every member read, including dropped ones; `MAX_MEMBER_BYTES` applies to pack files. §9 item 12 adds "or a git pack over 1 GiB". | §5.4.1 5-base item 4, step 3, §9 item 12 |
| 11 | NIT: output tar format not pinned | **Accepted.** Both output tars use `tarfile.open(fileobj=spool, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape")`. Unit case reads the name bytes back; Docker test 8 Case A checks the round trip. | §5.4.1 step 2 "Output tar format", §8.1 |


### 10.4 Responses to the round 4 whole-design review (`batch3-design-review.md`, findings 1-12)

None of these changes alters a signed-off §9 decision 1-11. Item 4 (token hygiene on in every mode, fail on a hit) and item 5 (path literals fail the Run) keep their text and effect; finding 1 refines which files supply credential bytes and is recorded as new item 14, finding 4's port check as new item 15, and the other user-visible effects go into item 12 (not in the protected set).

| # | Finding | Decision | Where |
|---|---|---|---|
| 1 | HIGH: post-agent snapshots harvest needles from agent session/transcript files | **Accepted, review's rule.** `snapshot(..., credential_files=None) -> Snapshot(secrets, credential_files, skipped)`. #1 fixes the credential file set; #2a/#2 re-read only those paths plus new files whose basename matches `(?i)(token\|oauth\|credential\|cred\|secret\|auth\|session\|\.pbtxt$\|\.json$)` and that are under no `tmp`/`logs`/`history`/`brain`/`conversations`/`cache` directory. Whole-file needles only from files ≤ 8 KiB (was 64 KiB); token-shaped runs from all selected files. Path literals unchanged. New §9 item 14 (probe listing checked before the first real Run; `credential_paths()` narrows once confirmed). Unit test with `antigravity-cli/brain/conv.json` (no finding) vs the sentinel (finding). Test 8 Case B's sentinel file is selected by the basename rule (noted). | D9, §5.5, §6 token_hygiene/cli rows, §7 code, §8.1, §8.2 test 8, §9 item 14 |
| 2 | MEDIUM: credential snapshot limits drop data silently | **Accepted, review's fix.** Selected members over 1 MiB or past the 8 MiB total are counted in `Snapshot.skipped`; if non-zero, `cli.py` appends one non-failing `violation` event `token hygiene: <n> credential-root files not scanned for needles (over 1 MiB or past the 8 MiB cap)`. The stream is bounded by `_ChunkReader(stream, 64 MiB, error_prefix="token hygiene: credential snapshot failed: ")` (new keyword on `_ChunkReader`). Unit cases: 2 MiB member, nine 1 MiB members, stream cap. | §5.5, §6 trusted_capture/token_hygiene/cli rows, §7 code, §8.1 |
| 3 | MEDIUM: check 2 has no rule for other git config exit codes | **Accepted, review's rule.** exit 0 + output → "defines a filter or diff driver"; exit 1 or exit 0 empty → OK; any other exit → `UnsafeGitConfig("… could not be parsed (git config exit <n>)")`. Fail closed, consistent with today (a malformed config fails every in-Sandbox git command). Unit cases for exit 3/0/1/0-empty. Listed in §9 item 12. | §5.4 check 2, §5.4.1 step 4, §8.1, §9 item 12 |
| 4 | MEDIUM: app port 443/3128 collides with the proxy | **Accepted.** `resolve_policy(agent, config_egress, cli_mode, *, app_port)` raises `EgressConfigError` at step 1 for 443/3128 in non-off modes, before any Docker object exists. Unit cases (443, 3128 rejected; 3000 accepted; off + 443 accepted) and a `test_cli.py` case. New §9 item 15. | §5.2, §6 egress/cli rows, §7 code, §8.1, §9 item 15 |
| 5 | MEDIUM: test 6 stale vs D9, capture path unspecified, fixture missing | **Accepted.** Shared `_run_trusted_step8(sandbox, client, run_dir, secrets, base) -> (branch, base_ref)` for tests 6 and 7, performing exactly the §7 step-8 sequence. Vector (2) reworded ("the Sandbox's `core.worktree` has no effect on trusted capture") with the review's three assertions; the "image's own git" claim is removed from test 6 and the §1 row. New fixture `served_repo_with_tracked_symlink` (a `served_repo` factory with an `extra_setup(source)` callback creating `docs/latest -> /usr/share/doc/x`). | §1 core.worktree row, §8.2 tests 6-7 |
| 6 | NIT: `GIT_CONFIG_GLOBAL` needs git ≥ 2.32 | **Accepted, preferred option.** Floor raised to 2.32 ("older than 2.32"); unit case `git version 2.31.8` → error, `2.32.0` passes. | D9, §5.4.1 step 4, §6, §7 code, §8.1, §8.2 test 6, §9 item 12 |
| 7 | NIT: pre-agent failures claim a missing Event_Log | **Accepted.** New `_event_log_suffix(run_dir)` used by the `except GitCaptureError` and token-hygiene clauses: `(Event_Log preserved at …)` only if the file exists, else `(the agent never started)`. One `test_cli.py` case each way. | §6 cli row, §7 code and notes, §8.1 |
| 8 | NIT: untrusted-config spool must be a tar | **Accepted.** `SplitArchive.untrusted_config` is a PAX tar spool with exactly one member `untrusted-config` (regular, `0o644`, uid/gid 0), written with the shared `tarfile.open(..., format=PAX_FORMAT)` call; `put_archive("/tmp", …)`. Unit case reads it back. | §5.4 check 2, §5.4.1 steps 1 and 4, §8.1 |
| 9 | NIT: violation payload host spelling | **Accepted.** The proxy logs `raw_host` (pre-normalisation) next to `host`; §5.3 payloads use the sanitised `raw_host`; the upload scrub then applies. `test_egress.py` case: a mixed-case needle in `raw_host` is scrubbed to `***`. | D6 log format, §5.3, §8.1 |
| 10 | NIT: `vaultshield teardown` cannot find the staging dir | **Accepted.** `remove_egress_resources` also globs `vaultshield-egress-<id>-*` in `tempfile.gettempdir()` and `rmtree(ignore_errors=True)`s each. `test_teardown.py` case (own id removed, other id kept). | §6 egress/teardown rows, §8.1 |
| 11 | NIT: FR5 and NFR1 wording | **Accepted.** FR5: "Symlinks the Run adds or retargets to a path outside the repo fail the Run." NFR1 lists the helper's calls, the Sandbox `get_archive` calls (5-base, snapshots #1/#2a/#2, step-8 work tree) and `container.kill()`. | §2 FR5, NFR1 |
| 12 | NIT: stale signature in §10.2 row 11 | **Accepted.** Row 11 now says "(superseded by §10.3 #6: …, branch, base_ref)". | §10.2 row 11 |
