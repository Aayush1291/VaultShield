# Batch 3 investigation: sandbox isolation facts and egress-control groundwork

Status: investigation only. No repository file was changed. Two throwaway probe
containers and one throwaway `internal: true` network were created from
`vaultshield-sandbox:local` and removed afterwards (verified: no `vs-*`
containers or networks left, `git status` unchanged at 26 entries). The probe
scripts lived in `/tmp` and were deleted.

Host: macOS, Docker Desktop, server `linux 29.2.1 arm64`, kernel
`6.12.72-linuxkit`, security options `seccomp(builtin), cgroupns`. Containers run
in the Docker Desktop Linux VM, so host (macOS) packet filters do not apply to
them, and the CLI has no direct access to the VM's iptables.

Legend: **[measured]** = observed in this investigation; **[code]** = read
from source; **[unknown]** = cannot be settled without a real signed-in `agy`.

---

## 1. Container config today (`cli/vaultshield/sandbox.py`, `Sandbox.create`)

[code] The only `containers.create` call (sandbox.py ~L606-624) passes exactly:

```python
self._client.containers.create(
    base_image,
    command=["tail", "-f", "/dev/null"],
    name=f"vaultshield-run-{run_id}",
    working_dir="/workspace",
    volumes={str(env_file.resolve()): {"bind": "/workspace/.env.sandbox", "mode": "ro"}},
    ports={f"{publish_port}/tcp": None} if publish_port is not None else None,
)
```

No `network`, `cap_add/cap_drop`, `user`, `security_opt`, `read_only`,
`dns`, `extra_hosts`, `pids_limit`, `mem_limit` or `environment`. So all of
these are Docker defaults. Measured on a replica container with the same kwargs
[measured]:

| Property | Value |
|---|---|
| NetworkMode | `bridge` (the default `docker0` bridge, 172.17.0.0/16, ICC enabled, masquerade on) |
| Container IP / gateway | 172.17.0.2 / 172.17.0.1 |
| User | `""` in config, i.e. **root** (`uid=0(root) gid=0(root)`) |
| Privileged | false |
| CapEff/CapBnd | `00000000a80425fb` = Docker default set: CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, SETGID, SETUID, SETPCAP, NET_BIND_SERVICE, **NET_RAW**, SYS_CHROOT, MKNOD, AUDIT_WRITE, SETFCAP. **No NET_ADMIN**, no SYS_ADMIN |
| Seccomp | mode 2 (Docker default profile), NoNewPrivs 0 |
| Mounts | only the `.env.sandbox` bind (ro) in a real run; no other bind or volume |
| `/sys` | `ro` |
| `/proc/kcore` | masked (char device 1,3 = `/dev/null`); `/proc/sysrq-trigger` read gives EIO |
| docker.sock | absent at `/var/run/docker.sock` and `/run/docker.sock` |
| Env | only image env: `PATH`, `NODE_VERSION`, `YARN_VERSION` |
| Port publish | `config.port/tcp -> ephemeral host port` on 0.0.0.0 (needed by browser capture) |
| DNS | `/etc/resolv.conf` -> `nameserver 192.168.65.7` (Docker Desktop's host DNS forwarder). The default bridge does NOT use Docker's embedded resolver 127.0.0.11 |
| IPv6 | container has only `::1` on `lo`; no global v6 address |
| Tools in image | `curl` present (`/usr/bin/curl`; the Dockerfile comment says it is "kept out of the final layer", but it is installed via apt and never removed), `python3`, `git`, `node/npm`. No `ip`, `iptables`, `nft` |

`exec`, `exec_stream`, `exec_detached`, `exec_interactive` all run as the
container default user (root); none pass `user=` or `privileged=` [code].
`teardown()` is `container.remove(v=True, force=True)`; it removes nothing
else (no networks today) [code]. `teardown.py` finds a container by name
(`containers.get(container_name(run_id))`) and reuses `Sandbox.teardown` [code].

## 2. Threat check: what the sandbox container can reach (measured)

All from the replica container above (same config as a real Run, default
bridge, root, default caps), using Python sockets.

### Host files
- No host path is mounted besides the ro `.env.sandbox` (dummy values only).
  `/root` in the container is the image's (`.bashrc`, `.profile`). No `/host`,
  no host HOME [measured]. The existing Docker-gated test
  `test_agent_executes_inside_the_sandbox_and_cannot_reach_the_host` already
  proves host `.env`/host HOME are unreadable and docker.sock absent [code].
- `/proc` is the container's own pid namespace; `/proc/kcore` masked; `/sys` ro.

### docker.sock
- Absent [measured]. No way to drive the daemon from inside.

### Host / private network ranges (IPv4)
| Target | Result |
|---|---|
| Docker gateway 172.17.0.1 :27017 (host-published mongo) | **OPEN** |
| Docker gateway 172.17.0.1 :5050 (host-published API) | **OPEN** |
| `host.docker.internal` -> 192.168.65.254 (also AAAA fdc4:f303:9324::254) :27017 / :5050 / :8000 | **OPEN / OPEN / OPEN** |
| `gateway.docker.internal` -> 192.168.65.1 | resolves |
| **192.168.1.1:80 — the developer's real LAN router** (host default gateway is 192.168.1.1 on en0, host IP 192.168.1.18) | **OPEN** |
| 10.0.0.1:80 | timeout (nothing there; not blocked by any rule — routing allows it) |
| 172.16.0.1:80 | timeout (same) |
| 169.254.169.254:80 cloud metadata | timeout — no metadata service on a laptop. Nothing in the config blocks it; on a cloud VM host it would be routed the same way |

So every service the developer publishes on the host (here: an unrelated
project's mongo:27017, API:5050, ML:8000; in VaultShield's own setup the
backend on localhost:8000 and the test replica set on 27018) is reachable from
inside the Sandbox, as is the developer's LAN. The default backend URL
`http://localhost:8000` is reachable as `host.docker.internal:8000`, but the
dashboard token is never placed in the container [code: uploader runs on host].

Note: the existing Docker-gated tests clone from
`git://host.docker.internal:9418/target.git` (`served_repo` fixture in
`cli/tests/test_sandbox_execution.py`). The clone phase therefore **depends on
host reachability** in tests; any policy applied before/at clone time must
keep that working or those tests break.

### Internet
- TCP `example.com:443`, `1.1.1.1:443`, `oauth2.googleapis.com:443`: **OPEN**.
- Direct UDP/53 to 8.8.8.8: got a 61-byte DNS answer, i.e. **arbitrary UDP to
  the internet works**, bypassing the configured resolver.
- IPv6: `2001:4860:4860::8888:443` -> `ENETUNREACH`. No v6 egress on the
  default bridge today (Docker Desktop default). A user-defined network created
  with `enable_ipv6=True` would change that, so a design must not enable it.

### DNS / DNS-based exfil
- The resolver (192.168.65.7) resolves arbitrary names: Google hosts resolve,
  and a random never-registered `vs-exfil-probe-<hex>.example.com` returns
  NXDOMAIN. NXDOMAIN for a random label means the query was forwarded upstream,
  so **arbitrary-label DNS exfil via the configured resolver is possible**
  (inferred from the response; the upstream query was not observed on the wire).
- Plus direct UDP/53 to any public resolver (above), so blocking only the
  configured resolver would not be enough.

### Capabilities / ability to alter networking
- No NET_ADMIN: `ioctl(SIOCSIFMTU)` on eth0 -> `EPERM` [measured]. Without
  NET_ADMIN a root process cannot change routes, interfaces, or iptables in its
  netns, even if it installs `iptables`/`iproute2` (it can: apt + internet).
- **NET_RAW is present**: `socket(AF_INET, SOCK_RAW, IPPROTO_ICMP)` succeeds.
  Raw sockets allow crafting arbitrary IP/ICMP packets (ICMP tunnelling,
  spoofed packets on the bridge, ARP games against other default-bridge
  containers). Any filter must therefore act on packets, not only on
  TCP connects or DNS, or NET_RAW must be dropped.
- Default bridge has ICC enabled: other containers on `docker0` are reachable
  (none running now; the unrelated project uses its own network).

### Behaviour of an `internal: true` user-defined network (measured)
Created `vs-batch3-internal-probe` (bridge, `internal=True`, 172.30.99.0/24) with
a peer container (`python3 -m http.server 8080`) and a probe container:
- No default route (only 172.30.99.0/24 on eth0).
- resolv.conf is Docker's embedded resolver with `ExtServers: [host(192.168.65.7)]`,
  but **external names do not resolve** (`oauth2.googleapis.com`,
  `host.docker.internal` -> EAI_AGAIN). Docker 29 does not forward external
  queries for containers attached only to internal networks.
- 1.1.1.1:443, example IP:443, 192.168.65.254:27017 -> `ENETUNREACH`.
  Network gateway 172.30.99.1:27017 -> timeout (host-published ports are not
  reachable through the internal network's gateway).
- **Peer container by name (`vs-b3-peer:8080`) -> OPEN.** A sidecar (e.g. a
  filtering proxy) on the same internal network is reachable by name.
- **Port publishing is silently dropped** on an internal-only container:
  `NetworkSettings.Ports == {'3000/tcp': []}`. `browser_capture.resolve_base_url`
  would then fall back to the container IP, which is not reachable from macOS
  (Docker Desktop), so **step 7 (browser capture) breaks if the Sandbox is
  internal-only at that point**. Whether `docker network connect` of a second,
  non-internal network later makes the original `ports=` binding live was NOT
  tested [unknown].

### Isolation probe (`cli/isolation_probe.py`) [code]
Standalone script, uses real `Sandbox.create` (defaults `python:3.11`,
octocat/Hello-World, needs `.env.sandbox`), runs 10 read-only probes: `pwd`,
`ls -la`, `cat /host/.env`, `cat /workspace/.env.sandbox`, `ls /root`,
`ls /home`, `ls -la /var/run/docker.sock`, curl to docker.sock, `hostname`,
write a marker into the repo. **It has no network probes at all** — none for
host ranges, metadata, internet, DNS, IPv6, caps. Batch 3's threat check must
add them. It also calls `Sandbox.create` without `publish_port`, and does
nothing with `--interactive-auth`/real `agy`.

The real-`agy` part of item (1) (running the probe with a signed-in agent) was
not done here: it needs the developer's interactive Google sign-in. The
network facts above do not depend on which process runs in the container — the
agent runs as root with the same netns via `exec_stream` — so they apply to
`agy` as measured.

## 3. Antigravity token location vs REPO_DIR

- `REPO_DIR = "/workspace/repo"`, `WORKDIR = "/workspace"` [code, sandbox.py].
- `ANTIGRAVITY_TOKEN_PATH = "/root/.gemini/antigravity-cli/antigravity-oauth-token"`
  [code, adapters/antigravity.py L308]. HOME is `/root`. So the token is
  **outside `/workspace` entirely**, not under REPO_DIR; the clone's work tree
  is `/workspace/repo`, and `git add --all` (git_capture.commit_run_branch,
  run with `workdir=/workspace/repo`) only walks that work tree.
- **Inconsistency to resolve in design:** the same module's docstring (judgment
  call 2) and `cli/README.md` L72 say the session file is
  `~/.gemini/antigravity-cli/jetski_state.pbtxt`. `strings` on the image's
  `agy` 1.1.26 binary contains `jetski_state.pbtxt` and `.gemini/antigravity-cli/`
  but NOT the literal `antigravity-oauth-token` (only `jetski-standalone-oauth-token`
  appears, as part of a log string). So the exact filename is [unknown] until a
  real sign-in; the directory `/root/.gemini/` is certain. A token-hygiene
  guarantee should cover the whole `/root/.gemini` tree (and HOME generally),
  not one filename. Other credential-shaped strings in the binary:
  `.gemini/config/mcp_config.json`, `mcp_oauth_tokens.json`,
  `application_default_credentials.json`.
- Existing token tests are string-prefix checks only:
  `tests/test_interactive_auth.py` L520-532 (`not startswith(REPO_DIR)`,
  equals the constant) and Docker-gated
  `test_an_oauth_token_does_not_survive_into_a_fresh_container`
  (test_sandbox_execution.py ~L1295, writes the path in one container, checks a
  fresh one).

### How the token COULD reach the branch / diff.json (facts for item 4)
The file itself is outside the work tree, so `git add --all` cannot pick it up
by path. But the agent runs as root with `--dangerously-skip-permissions` in
`/workspace/repo`, so:
1. **Copy/inline**: it can `cat` the token into any file under the repo. git
   capture commits it and `build_structured_diff` puts it in `diff.json`'s
   `patch` (up to 200 KiB per file). `.gitignore` only helps if the target
   name is ignored; the agent controls `.gitignore` too.
2. **`.git/config` is agent-writable**: e.g. `core.worktree=/` or `/root`
   would make the later `git add --all` (run by VaultShield in the same
   container, no `-c` overrides, no `GIT_DIR`/`GIT_WORK_TREE` env) stage files
   outside `/workspace/repo`, including `/root/.gemini/...`. Also
   `core.excludesFile`, `core.fsmonitor`, `core.hooksPath`, filter drivers via
   `.gitattributes` run commands inside the container during capture. Not
   tested here [unknown whether git honours core.worktree for `add --all`
   from a cwd outside it — must be checked by the Docker-gated test].
3. **Symlink**: a symlink `repo/x -> /root/.gemini/...` is committed as a
   link (the target path string), not the content — leaks the path, not the
   token. `git diff` of a symlink shows the target string.
4. Out-of-band (not via diff): network exfil (Section 2), which is what the
   egress policy addresses.
git_capture runs: `git checkout -b`, `git add --all`, `git -c user.name -c
user.email commit --allow-empty --no-verify -m`, `git diff --numstat -z
--no-renames`, per-file `git diff -U0`, `git rev-parse`, `git hash-object`
[code]. None sets `core.worktree`/`GIT_DIR` explicitly.

## 4. Pipeline steps in `cli.py run` (where an egress phase would hook in) [code]

1 Config (`load_config`, `_resolve_project`) -> 2 Run_Directory -> 2b backend
`uploader.create_run` (abort, no container, on failure) -> outer
`try/except BaseException: _mark_run_failed_best_effort; raise` begins ->
3 `_build_run_credential_file` -> 4 adapter instantiated -> 5
`docker.from_env()`, `Sandbox(run_id, client)`, `sandbox.create(repo_url,
base_image, sandbox_env, publish_port=config.port, project_dir=...)` which does
**image -> container create+start -> `git clone` (needs network) -> `npm install`
/ `pip install` (needs network)** all inside `create()` -> 5b
`_authenticate_in_sandbox` if `--interactive-auth` (agy needs Google OAuth
hosts; developer pastes code) -> 6 `supervise_in_sandbox` (agent via
`exec_stream`, in-container watcher) -> 7 `start_and_capture` (app via
`exec_detached`, host Playwright via **published port**) -> 8
`commit_run_branch`, `resolve_diff_base`, `build_structured_diff` -> inner
`finally: sandbox.teardown()` -> 10 `_upload_run_artifacts` -> 11
`_complete_run`.

Consequences:
- Clone + dependency install happen inside `Sandbox.create` before the agent;
  they need open network (github/gitlab, npm/PyPI, and `host.docker.internal`
  in tests). The auth phase (5b) needs Google OAuth. The agent phase (6) is the
  one to restrict. Browser capture (7) needs the published port to the host and
  runs the app, which may itself try network (`npm run dev`).
- `--interactive-auth` is a flag on `run`; `_authenticate_in_sandbox` uses
  `sandbox.exec_interactive(..., workdir=Sandbox.WORKDIR,
  timeout_s=ANTIGRAVITY_AUTH_TIMEOUT_S=75)`. Monitored argv:
  `agy -p <prompt> --dangerously-skip-permissions --output-format text`,
  `env={}`. Auth argv: `agy -p "Reply with exactly the word: ready"
  --output-format text`.
- Codex: `CODEX_API_KEY` in exec env; Kiro: `KIRO_API_KEY` in exec env
  (`sandbox_invocation().env`). Neither adapter names an API host in code.

## 5. `violation` events: schema and round trip

- On-disk schema [code, monitor.py `_append_event_log`]: one JSON object per
  line, exactly `{"event_type", "timestamp", "payload"}` from the frozen
  dataclass `EventLogRecord(event_type: Literal["output","file_change","violation"],
  timestamp: str ISO-8601, payload: str)`. Written via
  `_append_event_log(event_log_path(run_dir), _merge_events(queue))`;
  `_merge_events` sorts by parsed timestamp (`_timestamp_sort_key`), so a new
  producer must use ISO-8601 UTC (`monitor._now_iso8601()`).
- Existing producer: `sandbox_monitor._collect_file_changes` emits
  `EventLogRecord(event_type="violation", payload="off_limits_paths violation: <path> (matched off_limits_paths entry '<p>')")`
  and raises `OffLimitsViolation` after the Event_Log is written.
- Uploader [code]: `_read_event_log` -> POST `/api/runs/<id>/events/` in
  batches of 100, verbatim.
- Backend [code]: `RunEvent.event_type = CharField(max_length=32)` with **no
  choices**; `RunEventIngestSerializer` requires `event_type` non-blank <=32,
  `payload` non-blank, `timestamp` parseable. Test
  `test_event_type_is_not_restricted_to_a_choice_set` (backend/runs/tests.py
  ~L617) already ingests `event_type: "violation"`. The events action has no
  status gate (accepts events for any Run state).
- Frontend [code]: `RunDetailView.jsx` `TimelineTab` renders every event
  generically (`event.event_type` badge + `payload` with `whitespace-pre-wrap`);
  no special-casing of `violation`. `RunDetailView.test.jsx` fixture already
  contains a `violation` event.
- **Conclusion: a `violation` event with the existing 3-field shape and a
  non-blank payload round-trips as-is; no backend or frontend change is needed
  for ingest or display.**
- **Caveat (load-bearing):** uploads only happen on the success path. Any
  exception after 2b goes to `_mark_run_failed_best_effort` and re-raises;
  `_upload_run_artifacts` is never called for a failed Run [code, cli.py
  ~L895-905]. So today an `OffLimitsViolation` (or any egress violation that
  fails the Run) leaves its `violation` events **on disk only**, and they do
  NOT appear in the dashboard timeline. If egress violations must show in the
  dashboard, either they must not fail the Run, or the failure path must upload
  the Event_Log (best-effort) — a design decision.

## 6. Google hosts Antigravity needs

From the binary (`strings` on `/usr/local/bin/agy` in the image, agy 1.1.26,
Go binary, ~200 MB) [measured, static]. Candidates for the minimal allowlist:
- OAuth: `accounts.google.com` (consent URL the developer opens in the HOST
  browser — the container itself may not need it), `oauth2.googleapis.com`
  (token exchange/refresh), `www.googleapis.com` (userinfo), possibly
  `oauth2.mtls.googleapis.com`, `sts.googleapis.com`, `iamcredentials.googleapis.com`.
- Model/agent API: `cloudcode-pa.googleapis.com` (the Gemini Code Assist
  backend, scopes `aicode`/`cclog` point here), `daily-cloudcode-pa`
  variants not seen; also present: `generativelanguage.googleapis.com`,
  `aiplatform.googleapis.com`, `agentaicode.googleapis.com`,
  `businessaicode.googleapis.com`.
- Feature flags/telemetry: `antigravity-unleash.goog`, `play.googleapis.com`
  (clearcut-style logging, scope `cclog`), `www.gstatic.com`.
- Updater: `antigravity-cli-auto-updater-974169037036.us-central1.run.app`
  (from the Dockerfile; image pins 1.1.26).
- OAuth client id seen: `884354919052-36trc1jjb3tguiac32ov6cod268c5blh.apps.googleusercontent.com`.
- The binary honours `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` (and lowercase) and
  `SSL_CERT_FILE` (strings present; Go's `net/http` ProxyFromEnvironment) —
  relevant if a CONNECT proxy is chosen. Not confirmed at runtime [unknown].
- **Which of these a real `-p` run actually contacts is [unknown] until a
  real signed-in `agy` run is observed** (e.g. via a logging proxy). Treat
  `oauth2.googleapis.com` + `cloudcode-pa.googleapis.com` as the expected core,
  everything else as to-be-confirmed.
- DNS for these returns many A/AAAA records (cloudcode-pa: 8 v4 + 8 v6) that
  rotate, so an IP-based allowlist needs refresh; a hostname-based (SNI/CONNECT)
  filter does not.

Codex/Kiro hosts are not named anywhere in adapter code [code]; to be taken
from vendor docs in design.

## 7. Enforcement constraints from this environment

- macOS + Docker Desktop: containers live in the linuxkit VM. Host `pf` does
  not see container traffic; the CLI cannot run `iptables` in the VM without
  a privileged helper container (`--privileged --net=host` / nsenter), which is
  itself a new trust surface.
- The agent is root but lacks NET_ADMIN, so anything configured from outside
  its netns (Docker network topology, a separate proxy container, DNS config)
  cannot be changed by it. It does have NET_RAW, so "proxy env vars only"
  is not enforcement; topology (no route out) is.
- `internal: true` networks give: no route out, no external DNS, no host
  reachability, peers reachable by name — but no port publishing.
- `docker network connect/disconnect` are host-side API calls the agent cannot
  make (no socket), so phase switching from the CLI is possible in principle.
  Not tested whether connect/disconnect on a running container behaves well
  with the in-flight `exec_stream` [unknown].
- Teardown removes only the container today; a per-run network/proxy would
  need its own cleanup in `Sandbox.teardown`, `_cleanup_after_failure`, and
  `teardown.py` (which only knows `run_id`, so names must be derivable from it).

## 8. Test infrastructure and pre-existing issues

- CLI: `cd cli && .venv/bin/pytest -q`. Docker-gated pattern in
  `tests/test_sandbox_execution.py`: `requires_docker` (`docker.from_env().ping()`)
  and `requires_sandbox_image` (image `vaultshield-sandbox:local` present);
  `served_repo` fixture runs `git daemon` on 9418, cloned from the container as
  `git://host.docker.internal:9418/target.git`; `_offline_backend(monkeypatch,
  cli_module)` stubs `load_token/create_run/complete_run/mark_run_failed`;
  `_PROJECT_ARG = "--project=0123456789abcdef01234567"`.
- Batch 2 baseline (batch2-verification.md): `.venv/bin/pytest -q -rfEs
  --deselect tests/test_sandbox_execution.py::test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever`
  -> 728 passed, 1 failed, 1 deselected.
  1. **Must be `--deselect`ed:** `test_a_hanging_signin_hits_the_read_timeout_instead_of_blocking_forever`
     hangs: docker-py polls (`docker/utils/socket.py:read` -> `poll.poll()` with
     no timeout) before `recv`, so `settimeout` in `exec_interactive` never
     fires. Real product bug; when killed it leaves container
     `vaultshield-run-20250614-091530-authhang000001` (remove with `docker rm -f`).
  2. **Pre-existing failure:** `test_exec_interactive_really_delivers_stdin_to_a_container_process`
     — `send_line` arms redaction, so `RECEIVED:[<sentinel>]` comes back as
     `RECEIVED:[***]`. Test-only fix possible (write to the raw socket). Out of
     Batch 3 scope unless the user says otherwise.
- Backend: `cd backend && .venv/bin/pytest -q` needs a Mongo replica set:
  `docker run -d --rm --name vs-test-mongo -p 27018:27017 mongo:7 --replSet rs0 --bind_ip_all`;
  `docker exec vs-test-mongo mongosh --quiet --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]})'`;
  wait for PRIMARY; `export VAULTSHIELD_MONGODB_URL='mongodb://localhost:27018/vaultshield?directConnection=true'`
  and the same for `VAULTSHIELD_TEST_MONGODB_URL`; stop with `docker stop vs-test-mongo`.
  Port 27017 is taken by an unrelated `ipsakti-mongo` container. Baseline 326 passed.
- Frontend: `cd frontend && CI=true npm test -- --ci` (baseline 7 suites / 60 tests).
- `backend/security_scan/rules.py` must stay untouched
  (`git diff --quiet backend/security_scan/rules.py`).
- Current worktree has Batch 1-2 changes uncommitted (25 modified, `.agents/`
  and `backend/runs/migrations/0002_alter_run_status.py` untracked). Everything
  stays uncommitted.
