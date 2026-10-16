# VaultShield

**Sandboxed, monitored execution and human review for AI coding agents.**

VaultShield runs an AI coding agent inside a disposable Docker container, records
what it does, captures the code changes it produces as a reviewable Git diff, and
holds those changes behind an explicit human approval step before they can become
a pull request. It is a pipeline for giving an autonomous agent a repository and a
prompt *without* giving it your working tree, your real credentials, or
unsupervised write access to your source.

> VaultShield reduces the blast radius of AI-assisted development and makes agent
> activity observable and reviewable. It does **not** eliminate the risk of a
> container escape, a malicious dependency, or a sophisticated exfiltration
> attempt, and it is not a formally verified security boundary. The security model
> below is explicit about which controls are implemented and which are residual
> risks or recommended hardening.

---

## Table of contents

- [1. Why VaultShield exists](#1-why-vaultshield-exists)
- [2. Key features](#2-key-features)
- [3. Technical stack](#3-technical-stack)
- [4. System architecture](#4-system-architecture)
- [5. End-to-end run lifecycle](#5-end-to-end-run-lifecycle)
- [6. Security architecture](#6-security-architecture)
- [7. Run artifacts and observability](#7-run-artifacts-and-observability)
- [8. Repository structure](#8-repository-structure)
- [9. Installation and local execution](#9-installation-and-local-execution)
- [10. Backend and dashboard](#10-backend-and-dashboard)
- [11. Engineering decisions and trade-offs](#11-engineering-decisions-and-trade-offs)
- [12. Impact, limitations, and future work](#12-impact-limitations-and-future-work)
- [13. Verification status](#13-verification-status)

---

## 1. Why VaultShield exists

### The problem

An AI coding agent is, operationally, a process that reads a repository, decides
what to change, and executes shell commands to do it. Handed a prompt and run
directly on a developer's machine, that same process can:

- read every secret in the shell environment and in nearby `.env` files;
- write anywhere the invoking user can write, not only inside the target repo;
- run arbitrary commands, including ones the prompt never asked for;
- make outbound network calls to anywhere;
- and leave changes that land in version control before anyone has looked at them.

Most agent tooling conflates *running the agent* with *trusting its output*.
VaultShield separates those two concerns. The agent runs in a controlled,
disposable environment; its activity is recorded as evidence; its changes are
captured as a structured diff on a dedicated branch; and a human decides whether
any of it is accepted.

### Who it is for

- **Developers** who want to let an agent attempt a change but review it like a
  pull request instead of trusting it blind.
- **Security-conscious engineering teams** that need agent activity to be
  isolated, logged, and auditable before it touches shared source.
- **Organizations adopting AI coding agents** that need a repeatable,
  evidence-producing execution path and a review gate rather than ad-hoc local runs.

### The core workflow

```
develop a prompt  ──▶  vaultshield run  ──▶  sandboxed execution + evidence
                                               │
                                               ▼
                       dashboard review  ◀──  artifacts uploaded to backend
                                               │
                           approve ──▶ vaultshield submit  (opens PR/MR)
                           reject  ──▶ vaultshield teardown (deletes branch)
```

### The engineering challenges

1. **Isolation without breaking the agent.** The agent needs a real repository, a
   working toolchain, and (sometimes) its own API credential, but must not see the
   developer's real secrets or filesystem.
2. **Credential hygiene.** A repository-host token is needed to clone a private
   repo and open a PR, but it must never reach the agent, the container
   environment, process argv, or any persisted artifact.
3. **Trustworthy evidence.** The diff and event log must describe what the agent
   actually did, captured in a way the agent could not have tampered with.
4. **Observability.** Terminal output, file changes, screenshots, and egress
   attempts all have to be recorded in a form a reviewer can reconstruct.
5. **Reliable cleanup.** Containers, networks, and branches must be torn down on
   every path — success, failure, timeout, and crash.

---

## 2. Key features

Each feature below is backed by a concrete module. "Implemented" means the code
path exists and is exercised by the test suite; integration points that require a
live Docker daemon are marked.

| Feature | What it does | Where |
|---|---|---|
| **CLI-driven agent execution** | `vaultshield run` runs the full pipeline: config → credentials → sandbox → agent → monitor → browser capture → git capture → teardown → upload. Five commands total: `init`, `login`, `run`, `submit`, `teardown`. | `cli/vaultshield/cli.py` |
| **Docker sandbox lifecycle** | Pulls/reuses a base image, creates a named container, clones the repo into it, installs dependencies, and guarantees teardown via `remove(v=True, force=True)`. | `cli/vaultshield/sandbox.py` |
| **Isolated working directory** | The agent runs against the container's clone at `/workspace/repo`, never the host checkout. A read-only `.env.sandbox` is mounted at `/workspace/.env.sandbox`. | `sandbox.py` |
| **Multiple agent adapters** | `mock`, `codex`, `kiro`, `antigravity` via a dispatch table. `mock` is a real in-container program (no API, deterministic shape) used as an end-to-end smoke test. | `cli/vaultshield/adapters/` |
| **Controlled agent invocation** | Adapters describe an invocation (`argv` + a minimal `env` whitelist) rather than spawning on the host; the container executes it. | `adapters/base.py` |
| **Monitoring** | PTY output capture plus an in-container file-change watcher, merged and timestamped into an Event_Log. | `monitor.py`, `sandbox_monitor.py` |
| **Off-limits enforcement** | A write to any `off_limits_paths` entry fails the run (`OffLimitsViolation`). | `monitor.py` |
| **Screenshot / UI evidence** | After the agent finishes, the target app is started in the container, `ready_path` is polled, and each configured route is screenshotted with Playwright. | `browser_capture.py` |
| **Git branch + structured diff** | Commits the agent's work to `vaultshield/run-<run-id>` and writes a per-file `diff.json` (path, additions, deletions, patch). | `git_capture.py` |
| **Event logging + run metadata** | `events.jsonl` (JSON Lines) and a per-run directory keyed by a sortable, collision-resistant `run_id`. | `run_directory.py`, `monitor.py` |
| **Credential redaction** | `.env.sandbox` mirrors the *shape* of real env values with dummy values; connection-string userinfo and non-local hosts are replaced. | `credentials.py` |
| **Network egress control** | Optional per-run egress policy (`off` / `log-only` / `allowlist`) enforced by a hardened sidecar proxy on an internal Docker network. | `egress.py`, `egress_proxy.py` |
| **Trusted capture** | The run branch and diff are built in a *separate* hardened helper container from objects read via the Docker API, not inside the agent's sandbox. | `trusted_capture.py` |
| **Token-hygiene scanning** | The agent's own credential files are snapshotted and run outputs are scanned for those bytes before upload (`TokenHygieneViolation`). | `token_hygiene.py` |
| **Backend registration + upload** | Runs are registered, artifacts uploaded, and status moved to `awaiting_review` via a Django REST API. | `uploader.py`, `backend/` |
| **Dashboard review** | A React dashboard lists projects and runs and renders diffs, events, screenshots, and findings for approve/reject. | `frontend/` |
| **PR/MR submission** | `vaultshield submit <run-id>` opens a pull/merge request for an approved run (GitHub/GitLab). | `submit.py`, `providers/` |
| **Cleanup + failure handling** | `teardown` removes stranded sandboxes and deletes a rejected run's remote branch; a Celery beat reaper garbage-collects orphaned sandboxes. | `teardown.py`, `backend/tasks/`, `sandbox_tracking/` |

**Partial / future-facing, labelled in-tree:**

- A Django **Channels** ASGI worker ships in the compose stack but is reserved for
  future live updates; the MVP does not stream.
- The `codex` and `kiro` built-in egress allowlists are marked **unverified** in
  `egress.py` (only `antigravity` and `mock` are verified against real traffic).

---

## 3. Technical stack

| Technology | Responsibility | Engineering significance |
|---|---|---|
| **Python 3.11+ / Click** | The `vaultshield` CLI and the whole host-side orchestration pipeline (`cli.py`). | A CLI makes agent execution scriptable, repeatable, and CI-friendly; Click gives typed options and generated help that double as the pipeline's contract. |
| **Docker SDK for Python (`docker>=7,<8`)** | Container lifecycle, exec streams, networks, bind mounts (`sandbox.py`, `egress.py`, `trusted_capture.py`). | Driving Docker through the API (not the `docker` CLI) lets the orchestrator set precise container settings, stream exec output for the Event_Log, and wrap daemon errors into one exception type. |
| **`watchdog`** | In-container file-change watching behind the Monitor. | File changes are first-class evidence; the watcher runs *inside* the sandbox so it observes the agent's real writes. |
| **Playwright (`playwright>=1.44`)** | Host-side browser that screenshots the running app per route. | Produces visual evidence of the app state after the run. The browser binary is a separate `playwright install chromium` step, reported clearly if missing. |
| **`httpx`** | CLI → backend HTTP (register, upload, status, login, submit). | A single modern HTTP client for all backend-facing commands. |
| **Git** | Clone, branch, diff, commit, and push for change capture and submission. | Git branches and structured diffs make agent output reviewable with the same mental model as a PR. |
| **Django 5.2 + Django REST Framework** | Backend API: projects, runs, artifact ingest, approve/reject state machine (`backend/`). | DRF's token auth + global permission default closes the entire `/api/` surface by default; the viewset router mirrors the REST surface in one place. |
| **MongoDB (`django-mongodb-backend`)** | System of record for runs, events, diffs, screenshots, approvals, findings, sandbox records. | The backend uses MongoDB-native transactions (Django's own transaction API is a silent no-op on this backend) and an atomic `$inc` for event sequence allocation. Requires a replica set / Atlas. |
| **Celery + Redis** | Async security scan on diff ingest; scheduled sandbox reaper and stuck-approval flagging (beat). | Separates slow/scheduled work from the request path; Redis is broker + result backend only (not storage). |
| **Pillow** | Validates uploaded screenshots as decodable images at ingest. | A corrupt upload is rejected at the boundary rather than failing when the dashboard renders it. |
| **gunicorn / daphne** | WSGI (`web`) and ASGI (`channels`, reserved) servers in compose. | Production-shaped process model for the stack. |
| **React 19 + Tailwind CSS 4 + Vite 7** | The reviewer-facing dashboard (`frontend/`). | SPA that reads fully-uploaded runs and renders the four evidence tabs; Vite proxies `/api` and `/media` to the backend so the browser stays same-origin (no CORS). |
| **Jest + Testing Library + fast-check** | Frontend unit and property tests. | Property-based tests over the API client and views. |
| **pytest + Hypothesis** | CLI and backend tests (property-based for credential shaping, run-id safety, approval atomicity). | The security-sensitive invariants are asserted as properties, not just examples. |

> Note on TanStack: the task brief mentioned TanStack libraries. The actual
> `frontend/package.json` does **not** depend on any TanStack package — the
> dashboard is plain React 19 + a hand-written `fetch`-based API client. This is
> documented as it exists, not as previously assumed. (An unrelated project under
> `APSIT-INTERNAL/` in this workspace uses TanStack; VaultShield's dashboard does not.)

---

## 4. System architecture

VaultShield is three cooperating subsystems: a **host-side CLI pipeline**, a
**Docker execution plane**, and a **backend + dashboard** review plane.

```mermaid
flowchart TB
    subgraph HOST["Developer host (vaultshield CLI)"]
        CLI["cli.py — run pipeline"]
        CFG["config.py<br/>.vaultshield.yml"]
        CRED["credentials.py<br/>.env.sandbox (dummy)"]
        RD["run_directory.py<br/>.vaultshield/runs/&lt;id&gt;"]
        MON["monitor.py /<br/>sandbox_monitor.py"]
        BC["browser_capture.py<br/>(Playwright)"]
        GC["git_capture.py"]
        TC["trusted_capture.py"]
        TH["token_hygiene.py"]
        UP["uploader.py / submit.py / teardown.py"]
    end

    subgraph DOCKER["Docker execution plane (per run)"]
        SBX["Sandbox container<br/>/workspace/repo"]
        PROXY["Egress proxy sidecar<br/>(hardened, internal net)"]
        HELPER["Capture helper<br/>(hardened, builds branch/diff)"]
        NETI["internal network<br/>(no default route)"]
    end

    subgraph BACKEND["Backend + review plane"]
        API["Django + DRF API<br/>/api/..."]
        DB[("MongoDB")]
        CEL["Celery worker/beat<br/>security scan + reaper"]
        DASH["React dashboard"]
    end

    CLI --> CFG --> CRED --> RD
    CLI --> SBX
    CRED -. read-only mount .-> SBX
    MON <--> SBX
    SBX -. egress on .-> NETI --> PROXY
    BC --> SBX
    GC --> TC --> HELPER
    TH --> SBX
    UP --> API
    API --> DB
    API --> CEL
    DASH <--> API
    UP -. submit opens PR/MR .-> API
```

### Layer responsibilities

1. **CLI layer (`cli.py`).** Parses options, resolves the project, orders the
   pipeline stages, and maps every failure to a one-line error. The `run`
   command's docstring is the authoritative stage list (verified via
   `vaultshield run --help`).
2. **Orchestration layer (`cli.py` body + helpers).** Creates the run directory,
   registers the run with the backend *before any container exists*, generates the
   credential file, instantiates the adapter, and drives sandbox/egress/capture
   lifecycles inside nested `try/finally` blocks so teardown always runs.
3. **Sandbox layer (`sandbox.py`).** Owns the Docker container for one run:
   image reuse/pull, clone, dependency install, exec primitives (`exec`,
   `exec_stream`, `exec_detached`, `exec_interactive`), and teardown.
4. **Monitoring + evidence layer (`monitor.py`, `sandbox_monitor.py`,
   `browser_capture.py`).** Streams PTY output, watches file changes inside the
   container, enforces off-limits paths, and screenshots the running app.
5. **Git integration layer (`git_capture.py`, `trusted_capture.py`).** Builds the
   `vaultshield/run-<id>` branch and the structured diff — in a separate helper
   container, not the agent's sandbox.
6. **Backend layer (`backend/`).** Registers runs, ingests artifacts, runs the
   approve/reject state machine, serves run detail, and schedules cleanup.
7. **Dashboard layer (`frontend/`).** Lets a reviewer browse projects and runs and
   inspect evidence before deciding.

### Where each operation runs

| Operation | Host | Sandbox container | Separate container | Backend / external |
|---|---|---|---|---|
| Config load, run-id, credential-file generation | ✅ | | | |
| Repo clone, dependency install | drives it | ✅ runs here | | |
| Agent execution | describes invocation | ✅ runs here | | |
| PTY capture, file-change watch | collects | ✅ produces | | |
| Screenshots | Playwright browser on host | app runs here | | |
| Branch + diff build | drives it | | ✅ capture helper | |
| Egress filtering | drives it | traffic originates | ✅ proxy sidecar | |
| Register / upload / status / submit | ✅ | | | ✅ API + Git host |

---

## 5. End-to-end run lifecycle

The order below is taken verbatim from the `run` command (confirmed with
`vaultshield run --help`). Optional stages and failure paths are called out.

```mermaid
sequenceDiagram
    actor Dev as Developer
    participant CLI as vaultshield run
    participant BE as Backend API
    participant DK as Docker
    participant SBX as Sandbox
    participant HLP as Capture helper

    Dev->>CLI: run --agent --prompt --repo-url ...
    CLI->>CLI: 1. load .vaultshield.yml, resolve project + egress policy
    CLI->>CLI: 2. generate run_id, create .vaultshield/runs/<id>/
    CLI->>BE: 2b. POST /api/runs/ (register; abort here if unreachable)
    CLI->>CLI: 3. build .env.sandbox (dummy values only)
    CLI->>CLI: 4. resolve agent adapter (before any container)
    opt egress not off
        CLI->>DK: 5a. create open + internal networks + proxy sidecar
    end
    CLI->>DK: 5. create sandbox, mount .env.sandbox (ro), clone repo, install deps
    opt egress not off
        CLI->>SBX: isolate: attach internal net, detach open, assert isolation
    end
    CLI->>DK: 5-base. record pre-agent commit + objects (Docker API)
    opt --interactive-auth
        CLI->>SBX: 5b. OAuth sign-in over stdin (never logged)
    end
    CLI->>SBX: 5c. snapshot agent credential files
    CLI->>SBX: 6. run agent under PTY + file-change watch → events.jsonl
    CLI->>SBX: 7. start app, poll ready_path, screenshot routes
    CLI->>DK: 8. kill sandbox; rebuild repo in helper; commit branch; diff.json
    CLI->>HLP: scan run outputs for the agent's credential bytes
    CLI->>DK: 9. teardown sandbox (always, success or failure)
    CLI->>BE: 10. upload events, diff, screenshots
    CLI->>BE: 11. PATCH status=awaiting_review (or failed on any error after 2b)
    Dev->>BE: review in dashboard → approve / reject
    Dev->>CLI: vaultshield submit <id> (approved) → opens PR/MR
```

**Key ordering and failure guarantees (from the source):**

- A `ConfigError` or an unresolved project stops the run **before anything is
  written to disk**.
- The run is **registered with the backend before any container exists** (step
  2b), so a failure in between can be recorded against it; any failure after 2b
  marks the run `failed` (gated to transition only from `running`).
- The agent adapter is constructed **before** `Sandbox.create`, so a bad
  invocation (empty prompt, missing API key) never creates a container.
- **Teardown runs in a `finally`** on every path — success, agent error,
  readiness timeout, screenshot failure, git-capture failure, `KeyboardInterrupt`.
- A **failed upload** leaves the run directory exactly as written, so artifacts
  can be re-uploaded (`diff.json`, `events.jsonl`, screenshots are preserved).
- Distinct failure classes stay distinguishable: `ReadinessTimeout` ("the app
  never came up", zero screenshots) vs `ScreenshotError` ("app answered 200 but
  visual capture failed", partial screenshots kept).

---

## 6. Security architecture

This is the most important section. Every control below is stated as the code
implements it. Where a control is **absent**, that is said plainly.

### 6.1 Sandbox isolation

**Container creation and teardown.** `Sandbox.create` names the container
`vaultshield-run-<run-id>`, keeps it alive with `tail -f /dev/null`, and runs the
agent via exec. `teardown` calls `remove(v=True, force=True)`, which SIGKILLs the
container and every process exec'd into it — so a detached dev server started for
screenshots cannot outlive the sandbox.

**Filesystem exposure.** Exactly one host path is bind-mounted into the sandbox:
the generated credential file, mounted **read-only** at `/workspace/.env.sandbox`:

```python
volumes={ str(env_file.resolve()): {"bind": "/workspace/.env.sandbox", "mode": "ro"} }
```

The repository is **cloned inside the container** (into `/workspace/repo`), not
mounted from the host. The developer's working tree, home directory, and real
`.env` are never visible to the agent.

**Host Docker socket.** The Docker socket is **never** mounted into the sandbox.
The CLI talks to the daemon from the host; the container has no Docker API access.

**Container privileges — the honest posture.** In the **default `egress: off`
mode**, the sandbox container is created with Docker's defaults and **no extra
hardening**:

- runs as **root** inside the container (no `user=` is set);
- **no** `cap_drop`, `security_opt` / `no-new-privileges`, seccomp/AppArmor
  profile, `read_only` root filesystem, `pids_limit`, or `mem_limit`;
- default bridge networking with outbound internet.

This is a deliberate, documented trade-off (the base run is Docker-default
isolation only), and it is the single most important residual risk to understand:
**in off mode, the agent runs as root in a container with broad capabilities and
unrestricted egress.** Isolation from the *host* comes from Docker's namespace and
cgroup boundaries; isolation of *behaviour within the container* is minimal in
this mode.

**Hardening is applied when egress is enabled.** With `--egress=log-only` or
`--egress=allowlist`, `Sandbox.create` additionally receives:

- `cap_drop=["NET_RAW"]` (the agent cannot craft raw frames to bypass the proxy);
- `sysctls={"net.ipv6.conf.all.disable_ipv6": "1"}` (no IPv6 side channel);
- attachment to a per-run **`internal: true`** Docker network with **no default
  route and no external DNS** after clone/install complete.

The **egress proxy sidecar** and the **trusted-capture helper** are hardened
regardless, with the full set: `cap_drop=["ALL"]` (proxy adds back only
`NET_BIND_SERVICE`), `security_opt=["no-new-privileges"]`, `read_only=True`,
`tmpfs={"/tmp": ""}`, `mem_limit="128m"`, `pids_limit=128`, and
`net.ipv4.ip_forward=0`.

**Image trust.** The default base image is `python:3.11` (chosen because the clone
and the in-container watcher both need `git` and `python`). The provided
`cli/sandbox-image/Dockerfile` builds a reproducible `vaultshield-sandbox:local`
on `node:20-bookworm-slim` with git, python, node/npm, and a **checksum-pinned**
Antigravity CLI (SHA-512 verified at build time). The egress proxy image is pinned
by digest. No credentials are baked into any image.

**Termination.** `kill_sandbox` (Docker API) stops the container before capture;
`teardown`/`remove(force=True)` destroys it. There is no in-container "stop"
method by design — the container's lifetime bounds every process in it.

### 6.2 Credential management

VaultShield handles two distinct credential classes and keeps them apart.

**Application credentials → shape-preserving dummies.** The developer's real
`.env` is never copied into the container. `credentials.build_sandbox_env` re-walks
the source file and writes `.env.sandbox` with the **same keys in the same order**
but **dummy values that preserve structural shape** (`credentials.py`):

- URL-shaped values → a fixed dummy URL;
- long key-shaped values → a fresh random token of the same length (from
  `secrets`, never derived from the real value);
- connection strings → userinfo replaced with `dummy_user:dummy_password`, and the
  **host replaced unless it is provably local** (loopback or RFC 1918);
- everything else → a fixed placeholder.

A verified example from a real run directory in this repo
(`.vaultshield/runs/.../.env.sandbox`):

```ini
VAULTSHIELD_MONGODB_URL=mongodb+srv://dummy_user:dummy_password@localhost/?retryWrites=true&w=majority
VAULTSHIELD_MONGODB_NAME=dummy-value
SECRET_KEY=dummy-value
```

The real Atlas username, password, and cluster hostname were all stripped; only
the scheme, port, and query shape survived — enough for an app to parse and start,
but pointing at nothing real. The file is written atomically (temp file +
`os.replace`) so the real value never touches disk even transiently.

**Repository-host token → one-shot git credential helper, never the agent.** A
private clone needs a token, but embedding it in the remote URL would write it into
`.git/config` (which the agent then reads) and putting it in the container env or
`.env.sandbox` would hand it to the agent. Instead (`sandbox.py`):

- the token is passed as the `environment` of a **single `git clone` exec**, which
  Docker applies to that process alone — no later exec (the agent included)
  inherits it;
- `argv` carries only `-c credential.helper=<snippet naming env vars>`, so the
  token never appears in `docker inspect` or any process list;
- the token's lifetime is exactly `git clone`.

The token is only ever sent to a host confirmed against a strict per-provider
allowlist (`github.com`/`gitlab.com` or a true subdomain) — a near-miss like
`github.com.evil.com` fails closed and the repo is cloned unauthenticated.

**Dashboard auth token.** `vaultshield login` exchanges a username/password for a
token stored at `~/.vaultshield/token` with mode `600`. `run` only ever *reads*
it; it is kept entirely separate from any git credential. The backend schema has
**no field capable of persisting a repository-host token** — only `Run.pr_url` (a
public URL) is ever written back.

**Leakage channels explicitly closed:** remote URL (helper, not URL), argv
(env/stdin, not args), container environment (whitelist only; `SandboxInvocation`
forbids copying `os.environ`), logs/exceptions (git output is `_redact`-ed, the
OAuth code is read with `getpass` and scrubbed from the echoed TTY stream), and
persisted artifacts (token-hygiene scan, below).

### 6.3 Network egress control

With egress enabled, `EgressGuard` builds a three-network topology per run
(`egress.py`):

- `vaultshield-open-<id>` — an ordinary bridge; clone and install run here.
- `vaultshield-int-<id>` — an `internal: true` bridge with **no default route and
  no external DNS**.
- The **egress proxy** sits on both; after clone/install the sandbox is switched
  onto the internal net only.

The proxy (`egress_proxy.py`) is a **custom stdlib CONNECT + TLS-SNI tunneling
proxy** — it does **not** terminate TLS and is not mitmproxy. It filters by
destination **host** against the effective allowlist (the agent's built-in list
unioned with `egress.allow`). In `log-only` mode unlisted hosts are recorded; in
`allowlist` mode they are denied. Decisions are written to `egress.jsonl` and
surfaced as `violation` events in the Event_Log (host-sanitized, secret-scrubbed).

**Why a root process in the sandbox cannot trivially undo it:** after the switch
the sandbox netns has one interface and no default route; changing routes/iptables
needs `CAP_NET_ADMIN` (not granted), attaching networks needs the Docker API (no
socket in the container), `NET_RAW` is dropped, and the proxy refuses to start with
IP forwarding enabled. `HTTPS_PROXY`/`/etc/hosts` entries are conveniences;
removing them only breaks the agent's own connectivity.

**Verified vs unverified allowlists:** `antigravity` is enforced against a real
log-only probe; `mock` is deny-all (verified); `codex` and `kiro` built-in lists
are marked **unverified** in the source.

### 6.4 Trusted capture

A subtle integrity problem: if the branch and diff were built inside the agent's
own sandbox, a hostile agent could tamper with git state to misrepresent its
changes. VaultShield avoids this (`trusted_capture.py`):

1. **Before any agent code runs**, `record_trusted_base` reads the pre-agent commit
   and objects through the **Docker API**.
2. After the agent finishes, `kill_sandbox` stops the sandbox and
   `archive_sandbox_repo` extracts a **filtered archive** of the work tree.
3. A **fresh, hardened capture-helper container** reconstructs the repository from
   the trusted base objects plus that archive, then commits the
   `vaultshield/run-<id>` branch and computes the diff.
4. Run outputs are scanned for the agent's credential bytes **before** anything is
   uploaded.

So the authoritative diff is produced by VaultShield's own tooling over a known-good
base, not by git commands the agent could have influenced.

### 6.5 Token-hygiene scanning

`token_hygiene.py` snapshots the agent's declared credential files (via
`Agent_Adapter.credential_paths()`) before and after the run, collects the secret
"needles" into a `SecretSet`, and scans the diff and event outputs for those bytes.
If a credential would have left the machine in an artifact, the run fails with a
`TokenHygieneViolation` whose message never contains the matched bytes. A failed
run's timeline is still uploaded, but **scrubbed** through `SecretSet.scrub`.

### 6.6 Threat model

**Mitigated by design:**

| Threat | Control |
|---|---|
| Accidental destructive changes to the developer's checkout | Agent runs against a container clone; host tree is never mounted. |
| Agent reading the developer's real secrets | `.env.sandbox` holds dummy values; host env is never copied in. |
| Repo-host token reaching the agent | One-shot credential helper scoped to the clone exec; never in argv/env/URL/artifacts. |
| Unreviewed changes landing in source | Changes captured on a branch; PR only via `submit` after human approval. |
| Writes to protected paths | `off_limits_paths` enforcement fails the run. |
| Unaudited outbound traffic | Optional egress proxy on an internal network; violations logged. |
| Tampered change evidence | Trusted capture builds the diff in a separate helper from Docker-API base objects. |
| Secret exfiltration in artifacts | Token-hygiene scan + scrubbed failure timelines. |

**Residual risks — not fully mitigated:**

- **Container escape.** A kernel/Docker vulnerability could let the agent escape;
  in `egress: off` mode the container runs as root with default capabilities,
  which widens this risk. VaultShield is **not** a substitute for a VM or
  gVisor-class boundary.
- **Egress off by default.** Unless `--egress` is set, the agent has unrestricted
  outbound network access, so network exfiltration is possible.
- **Allowlist is host-based.** Even in `allowlist` mode, an allowlisted host that
  offers a general-purpose upload API (e.g. a cloud storage JSON endpoint on an
  allowlisted domain) is an accepted residual exfiltration path, noted in the
  source.
- **Malicious dependencies.** `npm install` / `pip install -r requirements.txt`
  run arbitrary install hooks inside the sandbox; a poisoned dependency executes
  with the sandbox's privileges.
- **Compromised base image / Docker daemon.** Trust flows from the base image and
  the host daemon; a compromised daemon or image undermines the model.
- **Host-side orchestration.** The CLI itself runs with the developer's privileges
  and their git/`gh`/`glab` credentials; it is a trusted component.

### 6.7 Human review and trust boundaries

Screenshots, PTY logs, file-change events, egress logs, and the structured diff
give a reviewer the evidence to decide. They are exactly that — **evidence, not
proof.** They do not independently establish that a run was safe or that all
malicious behaviour was detected. The approval gate is a human decision informed by
observability, not an automated safety guarantee, and VaultShield should not be
described as a verified security boundary.

---

## 7. Run artifacts and observability

Every run writes a directory keyed by a sortable, collision-resistant id
(`<YYYYmmdd>-<HHMMSS>-<16 hex>`, UTC, drawn from `secrets`; filesystem- and
git-ref-safe). Layout (verified against real directories in this repo):

```
.vaultshield/runs/<run-id>/
├── .env.sandbox          # generated credential file (dummy values), ro-mounted into the sandbox
├── events.jsonl          # Event_Log: one JSON object per line
├── diff.json             # structured per-file change set
├── egress.jsonl          # proxy decision log (only when egress is enabled)
└── screenshots/
    └── <route-slug>.png   # one PNG per configured route
```

**`events.jsonl`** — JSON Lines, timestamp-ordered. Real excerpt from a `mock` run:

```json
{"event_type": "output", "timestamp": "2026-10-09T09:24:56.119915+00:00", "payload": "[mock-sandbox] cwd=/workspace/repo"}
{"event_type": "output", "timestamp": "2026-10-09T09:24:56.221944+00:00", "payload": "[mock-sandbox step=2] simulated output for prompt='add a file'"}
{"event_type": "output", "timestamp": "2026-10-09T09:24:56.272866+00:00", "payload": "[mock-sandbox] wrote vaultshield_mock_agent.txt"}
```

Event types are `output` (terminal/PTY), `file_change` (watcher), and `violation`
(off-limits/egress). JSON Lines is chosen so events can be appended incrementally
and a partial log is still parseable line-by-line.

**`diff.json`** — one object per changed file: `path`, `additions`, `deletions`,
and a `patch` (optionally `patch_truncated`). Real excerpt from the same run (the
`mock` agent wrote one file inside the sandbox, and the watcher + capture observed
it):

```json
{
  "files": [
    {
      "path": "vaultshield_mock_agent.txt",
      "additions": 1,
      "deletions": 0,
      "patch": "diff --git a/vaultshield_mock_agent.txt b/vaultshield_mock_agent.txt\nnew file mode 100644\n--- /dev/null\n+++ b/vaultshield_mock_agent.txt\n@@ -0,0 +1 @@\n+written by Mock_Adapter inside the Sandbox\n"
    }
  ]
}
```

**Screenshots** — `screenshots/<route-slug>.png`, one per `routes` entry
(`root.png` for `/`), captured after `ready_path` returns 200.

**Lifecycle.** The run directory outlives the run (`submit`, `teardown`, and a
retried upload all revisit it) and is never pruned by the CLI — a failed upload
preserves it so the same artifacts re-upload. On upload, events, diff, and
screenshots become a backend `Run` and its related rows; a failed-run timeline is
uploaded scrubbed of known secrets. Because dummy credentials and a redacted diff
are what get written, real credentials should not appear in artifacts — but
repository *content* in the diff is, by definition, the thing under review, so run
directories should be treated with the same confidentiality as the source repo.

---

## 8. Repository structure

```
VaultShield/
├── cli/                              # The vaultshield CLI (Python / Click)
│   ├── vaultshield/
│   │   ├── cli.py                    # Command group + the full `run` pipeline
│   │   ├── config.py                 # .vaultshield.yml schema + framework detection
│   │   ├── credentials.py            # Shape-preserving dummy .env.sandbox writer
│   │   ├── run_directory.py          # run_id generation + artifact path helpers
│   │   ├── sandbox.py                # Docker container lifecycle + exec primitives
│   │   ├── sandbox_monitor.py        # In-container supervise: PTY + file-change watch
│   │   ├── monitor.py                # Event_Log, off-limits enforcement
│   │   ├── browser_capture.py        # Playwright screenshots of the running app
│   │   ├── git_capture.py            # run branch + structured diff.json
│   │   ├── trusted_capture.py        # Diff built in a separate hardened helper
│   │   ├── token_hygiene.py          # Credential snapshot + output scanning
│   │   ├── egress.py                 # Per-run egress topology + policy
│   │   ├── egress_proxy.py           # CONNECT + SNI filtering proxy (stdlib)
│   │   ├── uploader.py               # login / create_run / upload / complete
│   │   ├── submit.py                 # Open PR/MR for an approved run
│   │   ├── teardown.py               # Clean up sandbox + rejected branch
│   │   ├── adapters/                 # base, mock, codex, kiro, antigravity
│   │   └── providers/                # github, gitlab remote-URL + API helpers
│   ├── sandbox-image/Dockerfile      # Reproducible vaultshield-sandbox:local image
│   ├── tests/                        # pytest suite (unit + property + docker-gated)
│   └── pyproject.toml
│
├── backend/                          # Django + DRF + MongoDB + Celery
│   ├── vaultshield_backend/          # settings, urls, celery, mongodb helpers
│   ├── accounts/                     # login view (token issuance)
│   ├── projects/                     # Project model + viewset
│   ├── runs/                         # Run/RunEvent/RunDiff/Screenshot/Approval, ingest, approve/reject
│   ├── security_scan/                # SecurityFinding model + Celery scan + rules
│   ├── sandbox_tracking/             # SandboxRecord for the orphan reaper
│   ├── tasks/                        # scheduled cleanup / stuck-approval tasks
│   ├── Dockerfile
│   └── pyproject.toml
│
├── frontend/                         # React 19 + Tailwind + Vite dashboard
│   └── src/                          # App, ProjectListView, RunHistoryView, RunDetailView, api.js
│
├── docker-compose.yml                # redis, web, channels, celery-worker, celery-beat, frontend
└── .vaultshield.yml                  # this repo's own run config (self-hosting example)
```

**Module relationships.** The CLI runs on the host and reaches the backend over
HTTP (`http://localhost:8000` by default); the CLI is deliberately **not** a
compose service because it needs host Docker and the developer's own git
credentials. The backend serves both the CLI and the dashboard; the dashboard
reaches the backend by proxying `/api` and `/media` over the compose network.

---

## 9. Installation and local execution

### Prerequisites

| Tool | Constraint | Notes |
|---|---|---|
| Python | **3.11+** | Both CLI and backend declare `requires-python = ">=3.11"`. |
| Docker | Running daemon | Required for `vaultshield run`; the CLI talks to the host daemon. |
| Git | Any recent version | Clone, branch, diff, push. |
| Node.js | 18+ (20 recommended) | Dashboard dev/build (Vite 7). |
| MongoDB | **Replica set / Atlas** | Backend uses MongoDB transactions; a standalone `mongod` cannot run them. |
| Playwright Chromium | one-time install | `playwright install chromium` for screenshots. |

### Install the CLI

```bash
git clone <your-fork-or-origin>/VaultShield.git
cd VaultShield/cli

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # installs the `vaultshield` console script

# Screenshots need a browser binary (separate from the pip package):
playwright install chromium

# Optional reproducible sandbox image (adds node/npm + the Antigravity CLI):
docker build -t vaultshield-sandbox:local sandbox-image
```

### Install the backend

```bash
cd ../backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"         # app + pytest/hypothesis
# or: pip install -e ".[server]"  # adds gunicorn + daphne for the compose stack
```

### Install the dashboard

```bash
cd ../frontend
npm install
```

### Configuration

**Backend / compose environment** (root `.env`, consumed by `docker-compose.yml`):

| Variable | Required | Meaning |
|---|---|---|
| `VAULTSHIELD_MONGODB_URL` | **yes** | MongoDB connection string (replica set / Atlas). No default — the backend raises at import if unset. |
| `VAULTSHIELD_MONGODB_NAME` | no (`vaultshield`) | Database name inside the cluster. |
| `VAULTSHIELD_SECRET_KEY` | prod | Django secret. Dev default is insecure; set a real one for anything shared. |
| `VAULTSHIELD_DEBUG` | no (`1` in dev) | Set `0` outside local dev. |
| `VAULTSHIELD_ALLOWED_HOSTS` | no | Defaults to `localhost,127.0.0.1,web`. |
| `WEB_PUBLISHED_PORT` / `FRONTEND_PUBLISHED_PORT` / `REDIS_PUBLISHED_PORT` | no | Published ports (`8000` / `5173` / `6379`). |

> **Never commit real secrets.** Use placeholders in examples and keep real values
> in an untracked `.env`.

**Project config** (`.vaultshield.yml` at the repo root; written by
`vaultshield init`):

| Field | Required | Meaning |
|---|---|---|
| `start_command` | yes | How the app is started inside the sandbox for screenshots. |
| `port` | yes | App port (1–65535). |
| `ready_path` | yes | Path polled for a 200 before screenshots. |
| `agent` | no | Default adapter placeholder. |
| `routes` | no | Routes to screenshot (a bare string is rejected — must be a list). |
| `off_limits_paths` | no | Paths that fail the run if written (must be a list). |
| `project` | no | Backend Project id (overridden by `--project`). |
| `project_dir` | no | Nested app root inside the clone; strictly validated (no `..`, no absolute). |
| `egress` | no | `{mode: off\|log-only\|allowlist, allow: [host[:port], ...]}`. Default `off`. |

`vaultshield init` detects Django (`manage.py`), Node (`package.json`), a
`Procfile` `web:` line, or `docker-compose.yml`, in that precedence, and writes
sensible defaults.

### Running VaultShield

```bash
# 1) Start backend + dashboard (and Redis/Celery) with the compose stack:
docker compose up --build
#   web (API):      http://localhost:8000
#   dashboard:      http://localhost:5173

# 2) Authenticate the CLI once (stores ~/.vaultshield/token, mode 600):
vaultshield login --backend-url http://localhost:8000 --username <user>

# 3) Initialize a project config in the target repo (if not present):
cd /path/to/target/repo && vaultshield init

# 4) Execute a sandboxed run (the mock agent is the safest end-to-end smoke test):
vaultshield run \
  --agent=mock \
  --prompt="add a file" \
  --repo-url="$REPO" \
  --base-image=vaultshield-sandbox:local \
  --project "$PID"
```

**Argument reference** (verified against `vaultshield run --help`):

| Argument | Required | Purpose |
|---|---|---|
| `--agent {antigravity,codex,kiro,mock}` | yes | Adapter to launch. |
| `--prompt TEXT` | yes | Prompt handed to the agent. |
| `--repo-url TEXT` | yes | Repository the sandbox clones. |
| `--base-image TEXT` | no (`python:3.11`) | Sandbox image; use `vaultshield-sandbox:local` for Node/Antigravity. |
| `--env-file PATH` | no (`.env`) | Source env whose *keys* are mirrored into `.env.sandbox` as dummies. |
| `--ready-timeout INT` | no (`60`) | Seconds to wait for `ready_path` before failing with no screenshots. |
| `--interactive-auth` | no | In-sandbox OAuth sign-in (only Antigravity needs it). |
| `--project TEXT` | no | Backend Project id (else `project:` in config). |
| `--egress {off,log-only,allowlist}` | no | Egress policy (else config, else `off`). |
| `--backend-url TEXT` | no (`http://localhost:8000`) | Backend; env `VAULTSHIELD_BACKEND_URL`. |

**Inspecting a run:**

```bash
ls -t .vaultshield/runs/            # newest run id first (ids sort chronologically)
RID=$(ls -t .vaultshield/runs/ | head -1)
cat   .vaultshield/runs/$RID/diff.json
head  .vaultshield/runs/$RID/events.jsonl
ls    .vaultshield/runs/$RID/screenshots/
```

A successful `mock` run prints the run id and artifact path, "Sandbox created",
"Committed run ... to branch vaultshield/run-<id>", "Sandbox torn down", and
"Run ... complete", then uploads and marks the run `awaiting_review`.

### Testing

```bash
# CLI (unit + property; docker-gated integration tests skip without a daemon):
cd cli && pytest -q

# Backend (needs a reachable MongoDB replica set; see settings_test.py):
cd backend && pytest -q

# Dashboard:
cd frontend && npm test
```

### Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Cannot reach the Docker daemon` | Docker isn't running. Start Docker Desktop / `dockerd`. |
| Clone fails on a private repo | No usable token for the host, or host not on the credential allowlist. Authenticate `gh`/`glab`; only `github.com`/`gitlab.com` (+ true subdomains) receive a clone token. |
| `ScreenshotError` naming `playwright install` | Chromium not installed. Run `playwright install chromium`. |
| `ReadinessTimeout`, zero screenshots | App never answered 200 on `ready_path` within `--ready-timeout`. Check `start_command`/`port`/`ready_path`. |
| `SandboxMonitorError` | Base image lacks `python` on PATH (watcher can't start). Use `vaultshield-sandbox:local` or an image with python. |
| `Cannot start run ... (no Sandbox was created)` | Backend unreachable/unauthenticated at registration. Check `--backend-url` and `vaultshield login`. |
| Backend exits citing MongoDB timeout | `VAULTSHIELD_MONGODB_URL` unreachable or a standalone (non-replica-set) `mongod`. |
| Port already in use (`8000`/`5173`) | Set `WEB_PUBLISHED_PORT` / `FRONTEND_PUBLISHED_PORT`. |
| Upload failed but artifacts kept | Expected: the run directory is preserved for re-upload. |

---

## 10. Backend and dashboard

### API surface (from `vaultshield_backend/urls.py` + `runs/views.py`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/login/` | Exchange username/password for a Dashboard_Auth_Token (only unauthenticated route). |
| `POST` / `GET` | `/api/projects/` | Create / list projects. |
| `POST` | `/api/runs/` | Register a run (CLI, before any container). |
| `GET` | `/api/runs/?status=&submitted=` | Filtered run list (paginated, page size 50). |
| `GET` | `/api/runs/:id/` | Full run detail: events, diff, screenshots, findings. |
| `PATCH` | `/api/runs/:id/` | Gated status transitions (`running→awaiting_review`, `running→failed`) + `pr_url`. |
| `POST` | `/api/runs/:id/events/` | Bulk Event_Log ingest (order preserved by `sequence`). |
| `POST` | `/api/runs/:id/diff/` | Store the diff; enqueues the Celery security scan. |
| `POST` | `/api/runs/:id/screenshots/` | Upload one screenshot (multipart). |
| `POST` | `/api/runs/:id/approve/` \| `/reject/` | Record the review decision. |

`:id` is the **CLI-generated `run_id`**, not the database pk (`lookup_field =
"run_id"`). There is no `DELETE` and no `PUT` by design.

**Auth/authz.** DRF `TokenAuthentication` + `IsAuthenticated` are set **globally**
in settings, so every `/api/` path except login requires `Authorization: Token
<token>`. The dashboard's `api.js` attaches that header for every call except login
(DRF's keyword is literally `Token`, not `Bearer`).

**Data model (`runs/models.py`).** `Run` is the hub. `RunEvent`, `Screenshot`, and
`SecurityFinding` are many-per-run foreign keys; `RunDiff` and `Approval` are
one-to-one. The `Approval` one-to-one is what makes "a run is decided at most once"
a schema-level guarantee. `Run.status` is a six-state machine (`running`,
`awaiting_review`, `approved`, `rejected`, `submitted`, `failed`).

**The approval state machine is race-safe.** `apply_decision` performs the gate as
a single conditional update (`filter(status__in={"awaiting_review"}).update(...)`)
inside a MongoDB transaction, retried only on `TransientTransactionError`. Of two
concurrent approve/reject requests exactly one wins; the loser gets a `409` naming
the run's actual status — not a `500`.

### Dashboard (`frontend/`)

A React 19 SPA (`App`, `ProjectListView`, `RunHistoryView`, `RunDetailView`,
`TabPanel`) over a hand-written `fetch` client (`api.js`). The run detail view
renders the four evidence surfaces — timeline (events), diff, screenshots, and
security findings — and exposes approve/reject. Vite proxies `/api` and `/media`
to the backend so the browser stays same-origin (no CORS middleware needed). The
token is persisted in `localStorage` with a memory fallback for private-mode
browsers.

### Execute vs collect vs store vs review

- **Execute** = the CLI runs the agent in the sandbox.
- **Collect** = the Monitor, browser capture, git capture, and egress log produce
  artifacts in the run directory.
- **Store** = the Uploader POSTs artifacts to the backend, which persists them in
  MongoDB and media storage.
- **Review** = a human inspects them in the dashboard and approves or rejects; only
  then does `submit` open a PR/MR.

These are deliberately separate so that producing evidence never implies trusting
it.

---

## 11. Engineering decisions and trade-offs

- **A CLI, not a service, for execution.** Agent runs are repeatable, scriptable,
  and CI-friendly as a command. The CLI is intentionally *not* a compose service
  because it needs host Docker and the developer's own git credentials — putting it
  in the network would mean mounting the Docker socket, which the model forbids.
- **Docker for isolation, with an honest default.** Namespaces/cgroups bound the
  host blast radius cheaply. The base `off` mode uses Docker defaults (root, full
  caps) — a documented trade-off — while the egress-enabled path and the proxy /
  capture-helper sidecars add `cap_drop`, `no-new-privileges`, read-only rootfs, and
  resource limits. The honesty is the point: the README states exactly what each
  mode does and does not harden.
- **Per-run directories keyed by a sortable id.** `ls` lists runs oldest-first
  without reading metadata; the id is filesystem- and git-ref-safe and carries 64
  bits of entropy, so concurrent runs never collide and the same id names the
  backend row, the branch, and the directory.
- **Structured `diff.json` over raw patch text.** A per-file list with counts and
  patches is what both the automated security scan and the dashboard's diff view
  consume — it makes review automation (and the scan-on-ingest) possible.
- **JSON Lines for events.** Append-friendly and incrementally parseable: a crashed
  run still leaves a readable log, and ingest preserves order via an explicit
  `sequence`.
- **Credential shape-preservation instead of blanking.** Replacing a value with
  `dummy-value` would turn "unset" into "set" and break apps that parse connection
  strings at import. Preserving scheme/port/shape lets the app boot in the sandbox
  while pointing at nothing real.
- **One-shot credential helper for the clone token.** The token lives only in one
  exec's environment and never in argv, the container env, the remote URL, or any
  artifact — the narrowest exposure that still allows a private clone.
- **Trusted capture in a separate container.** The diff is authoritative precisely
  because the agent could not touch the tooling that produced it.
- **Backend and dashboard separate execution from review.** Storing a run is not
  approving it; the one-to-one `Approval` and the gated state machine make the
  review gate enforceable at the schema and API level.
- **Teardown in `finally` everywhere.** Reliability over cleverness: no failure
  path leaves a container, network, or detached server behind, and a server-side
  reaper backstops a killed CLI.

**Implemented optimizations vs potential improvements.** Implemented: atomic
`$inc` event-sequence allocation (no row lock on MongoDB), conditional-update
approval gate, digest-pinned proxy image, checksum-pinned agent binary. Potential:
stronger sandbox hardening by default, seccomp/gVisor, and per-run resource quotas
(see below).

---

## 12. Impact, limitations, and future work

### Practical value

- **Reduces the blast radius** of letting an agent modify a repository: isolated
  execution, a dummy-credential environment, and a review gate before any PR.
- **Makes agent activity observable** through PTY logs, file-change events,
  egress decisions, and screenshots.
- **Improves review traceability** with a structured diff on a dedicated branch
  and a persisted, queryable run history.
- **Produces reproducible run artifacts** keyed by a stable id, re-uploadable on
  failure.
- **Separates execution from approval**, giving teams a human-in-the-loop control
  point for AI-assisted changes.

### Known limitations

- Default `egress: off` means unrestricted outbound network and a root container
  with default capabilities.
- The egress allowlist is host-based; some allowlisted hosts expose general upload
  APIs (an accepted residual exfiltration path).
- `codex`/`kiro` built-in allowlists are unverified against real traffic.
- The backend requires a MongoDB replica set / Atlas (transactions are
  load-bearing); a standalone `mongod` will not work.
- Evidence supports — but does not prove — that a run was safe.

### Future work (realistic, from in-tree notes)

- Harden the sandbox by default (non-root user, `cap_drop`, `no-new-privileges`,
  seccomp/AppArmor, resource quotas) rather than only when egress is enabled.
- Verify the `codex`/`kiro` egress allowlists and consider stronger network
  isolation (e.g. default-deny egress).
- Additional agent adapters behind the existing dispatch-table contract.
- Automated diff/policy analysis to assist (not replace) human review.
- A server-side reaper surface for runs stranded in `running` by a killed CLI.

---

## 13. Verification status

What was checked while writing this document:

**Verified (run locally):**

- `vaultshield --version` → `vaultshield, version 0.1.0`; `vaultshield --help` and
  `vaultshield run --help` — the commands and every `run` option above match the
  actual CLI output.
- CLI test suite: **927 passed, 37 skipped** (`pytest` in `cli/`; the skipped tests
  are the Docker-daemon-dependent integration tests).
- Real run-directory artifacts inspected in-repo: `diff.json`, `events.jsonl`, a
  `screenshots/root.png`, and a redacted `.env.sandbox` (the examples in sections 6
  and 7 are from these real files, with secrets stripped).
- Docker security posture read directly from `sandbox.py` / `egress.py` /
  `trusted_capture.py` (container kwargs, the hardened proxy/helper settings, the
  read-only credential mount, and the absence of a Docker-socket mount).
- Backend API surface and data model read from `vaultshield_backend/urls.py` and
  `runs/` (models, views, serializers); dashboard client from `frontend/src/api.js`.

**Not verified here (requires external services):**

- A full live `vaultshield run` against a running Docker daemon + backend +
  MongoDB was **not** executed in this environment; the lifecycle in section 5 is
  documented from the source and the CLI help, and from previously-captured run
  directories, not from a fresh end-to-end execution.
- The backend `pytest` suite and the frontend `npm test` suite were not run here
  (the backend needs a reachable MongoDB replica set).

---

*VaultShield makes AI-agent execution isolated, observable, and reviewable. It is a
control point and an evidence trail, not a guarantee of containment — use it with
that understanding.*
# VaultShield2
