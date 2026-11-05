# VaultShield Project Guide

This guide explains what VaultShield does, how a run flows through the system, where each part lives in the repository, which functions do which job, and which team member owns each part. Paths are relative to the repository root.

## 1. What VaultShield Is

VaultShield lets a developer run an AI coding agent (Codex, Kiro, Antigravity, or a mock agent for testing) on a Git repository without trusting it. The agent runs in a disposable Docker container on a fresh clone with only dummy credentials. Everything it does is recorded, its change is captured on a dedicated branch by a component it cannot tamper with, and the change becomes a pull or merge request only after a human approves it in a web dashboard.

The system has three parts:

| Part | Location | Technology | Job |
|---|---|---|---|
| CLI | `cli/vaultshield/` | Python 3.11+, Click, Docker SDK, Playwright | Runs the whole pipeline on the developer's machine: sandbox, monitoring, capture, upload, submit |
| Backend | `backend/` | Django 5.2, Django REST Framework, MongoDB, Celery, Redis | Stores runs and evidence, runs the security scan, enforces the approval state machine |
| Dashboard | `frontend/src/` | React 19, React Router 7, Vite 7, Tailwind CSS 4 | Lets a reviewer inspect evidence and approve or reject a run |

Supporting folders:

| Folder | Contents |
|---|---|
| `cli/sandbox-image/Dockerfile` | Image used for the per-run sandbox and the capture helper |
| `cli/tests/` | CLI test suite (pytest and Hypothesis), including Docker-backed integration tests |
| `docker-compose.yml` | Local stack: `web`, `celery-worker`, `celery-beat`, `redis`, `channels`, `frontend` |
| `spike/` | Feasibility spike scripts and results, including the secret-exposure experiment (`spike/RESULT.md`) and the dashboard screenshot tool (`spike/ui_shots.py`) |
| `docs/blackbook/` | The project report (`blackbook.tex`), diagrams, images and presentation content |
| `.vaultshield/runs/` | Run directories produced by real runs (events, diff, screenshots, dummy credential file) |

## 2. How a Run Works, End to End

The developer types:

```
vaultshield run --agent=mock --prompt="add a file" --repo-url="$REPO" --project "$PROJECT_ID"
```

The `run` command in `cli/vaultshield/cli.py` then performs these steps in order. The function that does each step is named on the right.

| Step | What happens | Function |
|---|---|---|
| 1 | Load `.vaultshield.yml` (agent, routes, off-limits paths, egress settings) | `config.load_config` |
| 2 | Work out the egress policy for this agent | `egress.resolve_policy` |
| 3 | Create a run ID and a run directory | `run_directory.generate_run_id`, `run_directory.create_run_dir` |
| 4 | Register the run with the backend before any container exists, so a later failure can still be recorded | `uploader.create_run` |
| 5 | Write `.env.sandbox`: the same keys as `.env` but every value replaced by a dummy of the same shape | `credentials.parse_env_file`, `credentials.build_sandbox_env` |
| 6 | If egress control is on, create the per-run networks and the filtering proxy | `EgressGuard.start` |
| 7 | Create the container, mount only `.env.sandbox` read-only, clone the repo inside with a one-shot token, install dependencies | `Sandbox.create` (`_clone_repo`, `_clone_credential`, `_install_dependencies`) |
| 8 | Move the sandbox onto the internal network whose only peer is the proxy, and check that direct internet fails | `EgressGuard.isolate` |
| 9 | Record the pre-agent commit through the Docker API as a trusted base | `trusted_capture.record_trusted_base` |
| 10 | Take a snapshot of the agent's own credential files, used later to detect leaks | `token_hygiene.snapshot` |
| 11 | Run the agent under a pseudo-terminal while a watcher inside the container records file changes; write everything to `events.jsonl`; enforce off-limits paths | `Monitor.supervise`, `sandbox_monitor.supervise_in_sandbox` |
| 12 | Start the application inside the sandbox and screenshot each configured route | `browser_capture.start_and_capture` |
| 13 | Kill the sandbox and copy out a filtered archive of its work tree | `trusted_capture.kill_sandbox`, `trusted_capture.archive_sandbox_repo` |
| 14 | In a separate helper container with no network, rebuild the repo from the trusted base and commit the change as one commit on the run branch | `TrustedCapture.start`, `git_capture.commit_run_branch` |
| 15 | Build `diff.json` with per-file additions, deletions and capped patches | `git_capture.build_structured_diff` |
| 16 | Scan the diff, event log and changed files for the agent's credential bytes; on a hit, fail the run and quarantine the diff | `token_hygiene.check_run_outputs` |
| 17 | Export the run branch as a Git bundle with its SHA-256 | `trusted_capture.export_run_bundle` |
| 18 | Remove the sandbox, helper, proxy and networks on every exit path | `Sandbox.teardown`, `trusted_capture.remove_capture_helper`, `EgressGuard.teardown` |
| 19 | Upload events, diff and screenshots, then move the run to `awaiting_review` (or `failed`) | `uploader.upload_run`, `uploader.complete_run`, `uploader.mark_run_failed` |

After that:

- The backend's Celery worker runs the static security scan on the added lines of the diff (`security_scan.tasks.run_security_scan`).
- A reviewer opens the run in the dashboard, inspects the Diff, Timeline, Screenshots and Security tabs, and clicks Approve or Reject (`POST /api/runs/:id/approve/` or `/reject/`).
- `vaultshield submit <run-id>` checks with the backend that the run is approved, imports the branch from the bundle, pushes it and opens a pull or merge request (`submit.submit`).
- `vaultshield teardown <run-id>` deletes the branch of a rejected run (`teardown.teardown`).

Run status moves through: `running` → `awaiting_review` or `failed`, then `awaiting_review` → `approved` or `rejected`, then `approved` → `submitted`.

## 3. CLI: Files and Functions

All files are in `cli/vaultshield/`.

### 3.1 Commands and Configuration

| File | Purpose | Key functions and classes |
|---|---|---|
| `cli.py` | Click entry point and the pipeline orchestration | `init` writes `.vaultshield.yml`; `login` stores the API token; `run` runs the pipeline in Section 2; `submit` and `teardown` wrap the modules below; `sandbox_smoke` checks Docker works |
| `config.py` | Reads and validates `.vaultshield.yml` | `ProjectConfig`, `EgressConfig`, `load_config`, `write_config`, `detect_framework` (guesses the app's framework for screenshots) |
| `run_directory.py` | Layout of each run's local folder | `generate_run_id`, `validate_run_id`, `create_run_dir`, `event_log_path`, `diff_path`, `screenshots_dir`, `branch_bundle_path` |

### 3.2 Sandbox and Credentials

| File | Purpose | Key functions and classes |
|---|---|---|
| `credentials.py` | Shape-preserving dummy credentials | `classify_shape` sorts a value into url, connection string, key or generic; `generate_dummy` makes a dummy that never contains the real value; `parse_env_file`; `build_sandbox_env` writes `.env.sandbox`. Hosts in connection strings are replaced unless loopback or private (`_is_preservable_host`) |
| `sandbox.py` | One Docker container per run | `Sandbox.create` (container, read-only mount, clone, install), `Sandbox.exec`, `exec_stream`, `exec_interactive`, `Sandbox.teardown`; `_clone_credential` gives the token only to the `git clone` process, for github.com and gitlab.com |
| `cli/sandbox-image/Dockerfile` | The sandbox image | Toolchain the agent and the capture helper use |

### 3.3 Monitoring and Screenshots

| File | Purpose | Key functions and classes |
|---|---|---|
| `monitor.py` | Host side of monitoring and the event log | `Monitor.supervise` runs the agent and merges streams; `PtyOutputCapture` (terminal output); `FileChangeWatcher`; `EventLogRecord` (one line of `events.jsonl`: type, timestamp, payload); `OffLimitsViolation` raised after the log is written |
| `sandbox_monitor.py` | Watcher that runs inside the container | `supervise_in_sandbox`, `snapshot` (polls the work tree every 0.15 s), `emit` |
| `browser_capture.py` | Screenshots with headless Chromium | `start_and_capture`, `wait_until_ready` (60 s default), `capture_route_screenshots`, `route_slug` |

### 3.4 Network Egress Control

| File | Purpose | Key functions and classes |
|---|---|---|
| `egress.py` | Builds the per-run network topology | `resolve_policy` (built-in allowlist per agent plus `egress.allow`); `EgressGuard.start` (networks and proxy), `isolate`, `teardown`; `proxy_lines_to_events` turns proxy decisions into `violation` events |
| `egress_proxy.py` | The filtering proxy that runs in its own container | `ProxyServer`, `decide` (allow or deny per host), `host_listed` (exact or `*.` wildcard), `parse_connect_request` (port 3128), `parse_client_hello_sni` (port 443), modes off, log-only and allowlist |

### 3.5 Trusted Capture and Leak Detection

| File | Purpose | Key functions and classes |
|---|---|---|
| `trusted_capture.py` | Builds the branch where the agent cannot interfere | `record_trusted_base` (pre-agent commit), `kill_sandbox`, `archive_sandbox_repo`, `TrustedCapture` (helper with no network, all capabilities dropped, 1 GB, 256 processes), `export_run_bundle`, `remove_capture_helper` |
| `git_capture.py` | Branch commit and structured diff | `run_branch_name`, `commit_run_branch` (one commit), `build_structured_diff` (200 KiB per-file and 2 MiB total patch caps), `UnsafeGitConfig` (refuses filter and diff driver settings) |
| `token_hygiene.py` | Detects the agent leaking its own credentials | `snapshot` builds a `SecretSet` of needles; `check_run_outputs` scans the diff, event log and changed files; `TokenHygieneViolation`; the diff is renamed `diff.quarantined.json` on a hit |

### 3.6 Agent Adapters

All in `cli/vaultshield/adapters/`. Every adapter implements the same `Agent_Adapter` interface in `base.py` (`launch`, `sandbox_invocation`, `auth_invocation`, `credential_paths`), so the pipeline never needs to know which agent it is running.

| File | Agent | How it is invoked |
|---|---|---|
| `mock.py` | `Mock_Adapter` | Deterministic Python program that writes a file; used for tests and the benchmark |
| `codex.py` | `Codex_Adapter` | `codex exec <prompt> --full-auto --ephemeral` (`build_codex_argv`) |
| `kiro.py` | `Kiro_Adapter` | `kiro-cli chat --no-interactive <prompt>` (`build_kiro_argv`) |
| `antigravity.py` | `Antigravity_Adapter` | `agy -p ...` with an interactive sign-in step (`build_antigravity_argv`) |

### 3.7 Backend Upload, Submission and Teardown

| File | Purpose | Key functions and classes |
|---|---|---|
| `uploader.py` | All HTTP calls to the backend | `login`, `store_token`, `load_token`, `create_run`, `upload_run`, `complete_run`, `mark_run_failed`, `upload_events_best_effort` |
| `submit.py` | Turns an approved run into a PR or MR | `submit` (refuses unless approved, checks the checkout matches the repo, imports the bundle, pushes without force, opens the PR or MR, records the URL); `detect_provider`; `resolve_git_credential` |
| `providers/github.py`, `providers/gitlab.py` | Repository host APIs | `open_pull_request`, `open_merge_request`; shared helpers in `providers/__init__.py` (`parse_remote_url`, `request_json`) |
| `teardown.py` | Cleans up a rejected run | `teardown`, `teardown_sandbox`, `delete_run_branch`, `sync_rejected_branch` |

### 3.8 Protect Module

All in `cli/vaultshield/protect/`. Protect covers agents that developers still run outside the sandbox.

| File | Purpose | Key functions and classes |
|---|---|---|
| `commands.py` | The `protect`, `exec`, `status`, `unprotect` commands and the `agents` and `hook` groups | `protect_command`, `exec_command`, `register_protect_commands` |
| `protector.py` | Moves `.env` values into the Keychain and leaves placeholders | `analyze`, `protect_project`, `unprotect_project` |
| `vault.py` | macOS Keychain storage through `keyring` | `Vault` |
| `execute.py` | `vaultshield exec`: starts the app with the real values injected and scrubs them from its output | `prepare`, `run_command`, `StreamScrubber` |
| `envfile.py`, `sync.py`, `discovery.py`, `manifest.py` | `.env` parsing, syncing new entries, finding env files, recording what was protected | `split_lines`, `perform`, `collect` |
| `agents/` | Per-agent deny rules and hooks (Claude Code, Codex, Kiro, Antigravity) | `install`, `doctor` |

## 4. Backend: Files and Functions

All files are in `backend/`.

### 4.1 Apps

| App | File | Purpose | Key functions and classes |
|---|---|---|---|
| `runs` | `models.py` | Data model of a run | `Run` (status, agent, prompt, branch, PR URL), `RunEvent`, `RunDiff`, `Screenshot`, `Approval` |
| `runs` | `views.py` | REST API for runs and the state machine | `RunViewSet` (`perform_create`, `partial_update`, `events`, `diff`, `screenshots`, `approve`, `reject`); `apply_decision` (conditional update inside a MongoDB transaction); `apply_status_transition` (gated `running` → `awaiting_review` or `failed`); `StateTransitionError` (HTTP 409 with the current status) |
| `runs` | `serializers.py` | Validation of uploads and responses | `RunSerializer`, `RunDetailSerializer` (nests events, diff, screenshots and findings), `RunEventIngestSerializer`, `RunDiffIngestSerializer`, `ScreenshotIngestSerializer` |
| `runs` | `sequences.py` | Ordering of events across uploads | `allocate_event_sequences` |
| `projects` | `models.py`, `views.py` | Projects that runs belong to | `Project`, `ProjectViewSet` |
| `accounts` | `views.py` | Token login | `LoginView` |
| `security_scan` | `rules.py`, `tasks.py`, `models.py` | Static scan of added diff lines | `scan_diff` with 5 rules (secret literal, destructive SQL, `rm -rf`, disabled auth, outbound call); `run_security_scan` Celery task; `SecurityFinding` |
| `tasks` | `scheduled.py` | Periodic jobs run by Celery Beat | `cleanup_orphaned_sandboxes` (every 15 min, sandboxes older than 30 min); `flag_stuck_approvals` (hourly, approved runs not submitted within 24 h) |
| `sandbox_tracking` | `models.py` | Record of live sandboxes for the reaper | `SandboxRecord` |

### 4.2 Project Settings and Infrastructure

| File | Purpose |
|---|---|
| `vaultshield_backend/settings.py` | MongoDB connection (`VAULTSHIELD_MONGODB_URL`), token auth, Celery broker and Beat schedule |
| `vaultshield_backend/urls.py` | Routes: `/api/auth/login/`, `/api/projects/`, `/api/runs/` and the run actions `events/`, `diff/`, `screenshots/`, `approve/`, `reject/` |
| `vaultshield_backend/mongodb.py` | `retry_on_transient_transaction_error` (up to 5 attempts), `to_mongo_precision` |
| `vaultshield_backend/drf_mongodb.py` | ObjectId fields for Django REST Framework |
| `vaultshield_backend/celery.py` | Celery app |
| `Dockerfile`, `../docker-compose.yml` | Container build and the local stack |

### 4.3 API Summary

| Method and path | Used by | Effect |
|---|---|---|
| `POST /api/auth/login/` | CLI, dashboard | Returns an API token |
| `GET, POST /api/projects/` | Dashboard | List and create projects |
| `POST /api/runs/` | CLI | Register a run (`running`) |
| `PATCH /api/runs/:id/` | CLI | Move to `awaiting_review` or `failed`, record the PR URL |
| `POST /api/runs/:id/events/`, `diff/`, `screenshots/` | CLI | Upload evidence |
| `GET /api/runs/`, `GET /api/runs/:id/` | Dashboard, `submit` | Run history and run detail |
| `POST /api/runs/:id/approve/`, `reject/` | Dashboard | Record the decision; 409 if not `awaiting_review` |

## 5. Dashboard: Files and Components

All files are in `frontend/src/`.

| File | Purpose | Key components and functions |
|---|---|---|
| `App.jsx` | Routes: `/`, `/login`, `/signup`, `/dashboard`, `/dashboard/projects/:projectId`, `/dashboard/projects/:projectId/runs/:runId` | `App` |
| `api.js` | The API client used by every page | `createApiClient`, `createBrowserTokenStore`, `buildQueryString` |
| `auth.jsx` | Login state and route guards | `AuthProvider`, `RequireAuth`, `RedirectIfSignedIn` |
| `Landing.jsx`, `SeeItWork.jsx` | Public landing page and the animated walkthrough | `Landing` |
| `LoginView.jsx`, `Login.jsx`, `Signup.jsx`, `AuthLayout.jsx` | Sign-in pages | `LoginView` |
| `DashboardLayout.jsx` | Sidebar, header and project deletion dialog | `DashboardLayout` |
| `ProjectsOverview.jsx`, `ProjectListView.jsx`, `NewProjectForm.jsx` | Projects overview and creation | `ProjectList`, `ProjectCard` |
| `ProjectRunsPage.jsx`, `RunHistoryView.jsx` | Filterable run history for a project | `RunList`, `RunRow`, `RunStatusBadge` |
| `RunDetailPage.jsx`, `RunDetailView.jsx` | Run detail with four tabs and the decision buttons | `DiffTab`, `TimelineTab`, `ScreenshotsTab`, `SecurityTab`, `DecisionControls` |
| `TabPanel.jsx` | Shared tab body with empty-state handling | `TabPanel` |
| `ThemeSwitch.jsx`, `theme.js`, `format.js`, `motion.js` | Light and dark theme, formatting, animation | |
| `*.test.jsx`, `contrast.test.js` | Jest and React Testing Library tests, including the colour-contrast check | |

## 6. Team Members and Their Work

The responsibilities below follow the team roles table in the project report. The Git history has only one committer, so it cannot be used to tell who wrote which part.

### Aayush Subhash Balip: Backend and Data Engineering

- Designed the MongoDB data model: `Run`, `RunEvent`, `RunDiff`, `Screenshot`, `Approval`, `Project`, `SecurityFinding`, `SandboxRecord`.
- Built the REST API in `backend/runs/views.py` and `serializers.py`, including evidence upload (`events`, `diff`, `screenshots`) and event ordering in `sequences.py`.
- Implemented the run state machine: `apply_decision`, `apply_status_transition`, `StateTransitionError`, and the transaction retry in `vaultshield_backend/mongodb.py` that turned 54 of 72 spurious HTTP 500 responses into the correct 409.
- Migrated the backend to MongoDB and wrote `drf_mongodb.py`.
- Built the Celery tasks: `run_security_scan` with the 5 rules in `security_scan/rules.py`, `cleanup_orphaned_sandboxes` and `flag_stuck_approvals`.
- Set up token authentication (`accounts/views.py`) and the Docker Compose stack.
- Main files: `backend/` (all apps), `docker-compose.yml`, `backend/Dockerfile`.

### Krish Mahesh Patil: Frontend and User Experience

- Built the React dashboard: routing in `App.jsx`, layout in `DashboardLayout.jsx`, and authentication in `auth.jsx` and `LoginView.jsx`.
- Built the projects overview and creation form, and the filterable run history (`RunHistoryView.jsx`).
- Built the run detail page with the Diff, Timeline, Screenshots and Security tabs and the Approve and Reject controls (`RunDetailView.jsx`, `TabPanel.jsx`).
- Wrote the API client (`api.js`) that shows the backend's 409 message to the reviewer.
- Handled accessibility and responsive layout: light and dark themes with contrast of at least 4.77:1, and no horizontal overflow across 63 page and width combinations.
- Built the landing page (`Landing.jsx`, `SeeItWork.jsx`).
- Main files: `frontend/src/`.

### Muhammad Anis Momin: Security Research and Sandbox Engineering

- Built the sandbox lifecycle in `cli/vaultshield/sandbox.py`: one container per run, read-only credential mount, clone inside the container, guaranteed teardown, and the one-shot clone credential.
- Built shape-preserving credential substitution in `credentials.py`, including the rule that hosts are replaced unless provably local.
- Built egress control in `egress.py` and `egress_proxy.py`: internal network with no gateway, hardened proxy, per-agent allowlists and logged decisions.
- Built trusted capture in `trusted_capture.py` and `git_capture.py`: trusted base, hardened helper, one-commit run branch, capped structured diff and verified bundle.
- Built token hygiene in `token_hygiene.py`: credential snapshot, output scan and diff quarantine.
- Ran the secret-exposure feasibility spike (`spike/`) and wrote the threat model.
- Built the Protect module (`cli/vaultshield/protect/`) for agents run outside the sandbox.

### Sameer Hasan Saiyed: Integration, Testing and Documentation

- Integrated the CLI pipeline in `cli/vaultshield/cli.py` (`init`, `login`, `run`, `submit`, `teardown`) and the project configuration in `config.py` and `run_directory.py`.
- Built the agent adapter interface and the four adapters in `cli/vaultshield/adapters/`.
- Built monitoring and screenshots: `monitor.py`, `sandbox_monitor.py` and `browser_capture.py`.
- Built backend upload (`uploader.py`), submission to GitHub and GitLab (`submit.py`, `providers/`) and teardown (`teardown.py`).
- Designed the test strategy and maintained the suites: 2,080 CLI tests, 341 backend tests and 283 frontend tests, with 2,702 of 2,704 passing.
- Ran the 40-run performance benchmark and wrote the project report.

## 7. How to Run It

1. Copy `.env.example` to `.env` and set `VAULTSHIELD_MONGODB_URL`.
2. Start the backend and dashboard with `docker compose up --build`. The API is at http://localhost:8000 and the dashboard at http://localhost:5173.
3. Create a user: `docker compose exec web python manage.py createsuperuser`.
4. Install the CLI: `cd cli && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && playwright install chromium`.
5. Log in: `vaultshield login --backend-url http://localhost:8000 --username <user>`. Create a project in the dashboard and copy its ID.
6. In the target repository, run `vaultshield init`, then `vaultshield run --agent=mock --prompt="add a file" --repo-url="$REPO" --project "$PROJECT_ID"`.
7. Review the run in the dashboard and approve or reject it.
8. For an approved run, run `vaultshield submit <run-id>`. For a rejected run, run `vaultshield teardown <run-id>`.

## 8. Tests

| Suite | Command | Count |
|---|---|---|
| CLI | `cd cli && pytest` | 2,080 tests, 2,078 pass |
| Backend | `cd backend && pytest` | 341 tests, all pass |
| Frontend | `cd frontend && npm test` | 283 tests, all pass |

The two failing CLI tests are known open issues. One is recorded in the report: a hanging interactive sign-in is not bounded by its deadline on Docker Desktop 29.2.1.

## 9. Known Limitations

- Containers share the host kernel, and the default sandbox runs as root. gVisor or Firecracker isolation is planned.
- Egress control is off unless enabled, and the allowlist filters by host, not by path.
- In the single-developer MVP, a token holder can set a run to `approved` through the status PATCH route without an `Approval` record.
- The polling file watcher missed the agent's write in 8 of 40 benchmark runs. The trusted diff is the authoritative record.
- The Diff tab shows per-file counts, not the patch text.
- Objective 2, the Agentic AI Orchestrator, is planned for future work and is not part of the delivered system.
