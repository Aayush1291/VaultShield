# VaultShield explained: CLI, sandbox, and website login

Read-only investigation. No source files were changed. I did not open the real `.env`, any `.env.sandbox`, or any token file. Where a claim could not be confirmed from code, it says so.

---

## Summary (the short answer)

**CLI.** `vaultshield` is a Click program (`cli/vaultshield/cli.py`) with five real commands: `init`, `login`, `run`, `submit`, `teardown` (plus a debug-only `_sandbox-smoke`). The main one is `run`: it starts a throwaway Docker container, lets an AI agent work inside it, records everything, then uploads the evidence to the backend so a human can approve or reject it on the dashboard.

**Sandbox and `.env`.** Think of the sandbox as a clean hotel room built from scratch for each run. The agent only gets what is delivered to the room:
- a fresh `git clone` of the repo (committed files only),
- a read-only `.env.sandbox` file with fake values, and
- (for Codex/Kiro) that agent's own API key.

The real `.env` is never mounted, copied, or passed in. It is gitignored (`.gitignore` line 9, `git check-ignore` confirms, and `git ls-files` shows it is not tracked), so a clone can't contain it. The only code that reads the real `.env` is the host-side CLI itself, in `build_sandbox_env`, and it writes only fake values.

So the real `.env` is protected mainly by **where the agent runs** (in a container with no host mounts), not by a filter that tries to hide it. That is the strongest kind of protection. But it is not airtight. See the limits section: network is open by default, the agent's own API key is real, comments in `.env` are copied verbatim, and `off_limits_paths` only detects after the fact.

**Website login.**
- The dashboard has **no login page** in the code today. `frontend/src/api.js` has a working `api.login()` function, but no component calls it. The only login-related UI text is an error message ("Sign in to load…").
- The backend side is complete: `POST /api/auth/login/` (`accounts/views.py: LoginView`) checks username and password with Django's auth system and returns a DRF auth token.
- The browser would store that token in `localStorage` and send it as `Authorization: Token <token>` on every call.
- There is **no signup endpoint**. Users must be created by an admin (Django admin or `manage.py`).

**CLI login vs website login.** They use the **same endpoint** and get the **same kind of token**. DRF's `Token` is one per user, so the CLI and the browser end up holding the identical token string. It has no expiry, and there is no server-side logout. The CLI stores it in `~/.vaultshield/token` (mode 600). The `interactive-auth-*.md` notes in `.agents/tasks/` are about something else: signing the *Antigravity agent* into Google inside the sandbox. They are not about dashboard login.

---

## CLI overview

Entry point: `cli/vaultshield/cli.py`, Click group `cli()`, console script `vaultshield` (see `cli/pyproject.toml`).

| Command | What it does | Where |
|---|---|---|
| `init` | Looks at the current folder (`detect_framework`: package.json, manage.py, Procfile, docker-compose) and writes `.vaultshield.yml`. | `cli.py: init`, `config.py: detect_framework, write_config` |
| `login --backend-url URL [--username]` | Asks for username and password (password hidden), POSTs them to `<URL>/api/auth/login/`, and stores the returned token at `~/.vaultshield/token`. | `cli.py: login` → `uploader.py: login, store_token` |
| `run --agent --prompt --repo-url [--project --env-file --egress --interactive-auth --base-image --ready-timeout --backend-url]` | The full pipeline (below). | `cli.py: run` |
| `submit RUN_ID` | After a human approves a run, pushes branch `vaultshield/run-<id>` and opens a PR/MR using your local `gh`/`glab` or `~/.vaultshield/credentials.yml`. Your git credential never goes to the backend. | `cli.py: submit` → `submit.py` |
| `teardown RUN_ID` | Removes a leftover container, and deletes the pushed branch if the run was rejected. | `cli.py: teardown` → `teardown.py` |
| `_sandbox-smoke` | Debug-only: make and destroy a sandbox. | `cli.py: sandbox_smoke` |

Agents available (`_AGENT_ADAPTERS` in `cli.py`): `antigravity`, `codex`, `kiro`, `mock`.

### Project config: `.vaultshield.yml`

The current file has: `start_command: 'python -m http.server 8000'`, `port: 8000`, `ready_path: '/'`, `agent: mock`, `routes: ['/']`, `off_limits_paths: ['protected.txt']`.
- `start_command`, `port`, `ready_path` are required (`config.py: REQUIRED_FIELDS`).
- I found no use of `config.agent` in the CLI. `--agent` is required on `run`, so the `agent:` line is effectively decorative. I only grepped for `config.agent`, so treat this as likely rather than certain.

### A typical `vaultshield run` (in order, from `cli.py: run`)

1. **Load config** (`load_config`), resolve the project id (`--project` or `project:` in YAML), and resolve the egress policy. A bad config stops here before anything is created.
2. **Make the run folder** `.vaultshield/runs/<run-id>/` (`run_directory.py: generate_run_id, create_run_dir`).
3. **Register the run with the backend** (`uploader.load_token` then `uploader.create_run` → `POST /api/runs/`). No token or no backend means it stops here, before any Docker call.
4. **Build `.env.sandbox`** (`_build_run_credential_file` → `credentials.py: build_sandbox_env`). Details in the next section.
5. **Pick the agent adapter** (before any container exists).
6. **Start egress guard** (only if `--egress` is not `off`), then **create the sandbox** (`Sandbox.create`): pull image, start container, `git clone`, install dependencies.
7. **Isolate the network** (`guard.isolate`, only if egress is on).
8. **Record the trusted base** (`record_trusted_base`): the pre-agent git state, read through the Docker API.
9. **Optional agent sign-in** (`--interactive-auth`, Antigravity only: `_authenticate_in_sandbox`).
10. **Snapshot the agent credential** (`token_hygiene.snapshot`).
11. **Run the agent inside the container**, monitored (`sandbox_monitor.supervise_in_sandbox`). Terminal output and file changes become `events.jsonl`.
12. **Start the app inside the container and screenshot it** (`browser_capture.start_and_capture`, using host-side Playwright against the published port).
13. **Capture the result safely**: kill the sandbox, rebuild the repo in a fresh helper container (`trusted_capture.TrustedCapture`), commit to branch `vaultshield/run-<id>`, write `diff.json` (`git_capture.commit_run_branch, build_structured_diff`), then scan outputs for the agent's token (`token_hygiene.check_run_outputs`).
14. **Tear down** the container, the helper, and the egress proxy and networks (the `finally` in `run`).
15. **Upload** events, diff, and screenshots (`uploader.upload_run`), then `PATCH status=awaiting_review` (`_complete_run`). On any failure after step 3, the run is marked `failed` (`_mark_run_failed_best_effort`).

### What lands in `.vaultshield/runs/<run-id>/` and why

| File | Why it exists | Written by |
|---|---|---|
| `.env.sandbox` | The fake-values file mounted into the container. It is the "input" to the run, not an artifact. Gitignored (`.gitignore`). Created mode 0600 via `tempfile.mkstemp`. | `credentials.py: build_sandbox_env`, `_atomic_write` |
| `events.jsonl` | Timeline: every line of agent terminal output, every file created/modified/deleted, and violations. | `sandbox_monitor.py`, `monitor._append_event_log` |
| `diff.json` | Per-file additions/deletions and patches of what the agent changed. | `git_capture.build_structured_diff` |
| `screenshots/<route>.png` | What the app looked like after the agent's changes. | `browser_capture.py` |

Constants for the three artifact names are in `run_directory.py` (`EVENT_LOG_FILENAME`, `DIFF_FILENAME`, `SCREENSHOTS_DIRNAME`). `_SANDBOX_ENV_FILENAME` is in `cli.py`.

Side note: the `events.jsonl`, `diff.json`, and `screenshots` of some existing runs are **tracked in git** (`git ls-files .vaultshield`). `.env.sandbox` files are not, because they are gitignored. That is fine as long as agent output never contains real secrets (see gaps).

### What `credentials.py` really is

You have `credentials.py` open, so a clarification: **it does not store tokens.** It is the "fake `.env` maker" (the Credentials_Module). Dashboard tokens live in `uploader.py` (`store_token`, `load_token`, `token_path`).

What it does:
- `parse_env_file` reads `KEY=value` lines.
- `classify_shape` decides what kind of value each one is.
- `generate_dummy` swaps in a fake of the same shape.
- `build_sandbox_env` writes the new file atomically.

### How the CLI talks to the backend

All through `cli/vaultshield/uploader.py` (HTTP via `httpx`), always with the header `Authorization: Token <token>`. The token comes from `~/.vaultshield/token`.
- `POST /api/auth/login/`: `login`
- `POST /api/runs/`: `create_run`
- `POST /api/runs/<id>/events/`, `/diff/`, `/screenshots/`: `upload_run`
- `PATCH /api/runs/<id>/`: `complete_run`, `mark_run_failed`
- `GET /api/runs/<id>/`: used by `submit.py` and `teardown.py`

The default backend URL is `http://localhost:8000` (`_DEFAULT_BACKEND_URL`; env var `VAULTSHIELD_BACKEND_URL`). `login` deliberately has **no default** so a password can't be sent to the wrong host by accident.

---

## Sandbox: how it works

Analogy: each run builds a new, empty hotel room (a Docker container). Staff deliver only what is on the room-service list. The agent can't walk into the owner's house (your laptop) because the room has no door to it.

All code is in `cli/vaultshield/sandbox.py: class Sandbox`.

### Creating a sandbox (`Sandbox.create`)
1. `_ensure_image`: reuse the image if it is cached locally, otherwise pull it. The default is `python:3.11`. `cli/sandbox-image/Dockerfile` builds a pinned image with git, python, node, and the `agy` binary. That image has no credentials baked in, and the Dockerfile says so.
2. `self._client.containers.create(...)` with:
   - command `tail -f /dev/null` (keeps it alive),
   - working dir `/workspace`,
   - **one and only one volume**: the host file `<run_dir>/.env.sandbox` bound **read-only** at `/workspace/.env.sandbox`,
   - an optional published port (an ephemeral host port) for the app.
3. `_clone_repo`: runs `git clone <repo_url> /workspace/repo` inside the container. For a private repo on github.com or gitlab.com, the token is passed only as the environment of that single clone exec, through a one-shot git credential helper. It is not in the command line, not in the container's environment, and not in `.git/config`. Hosts are checked strictly (`_CREDENTIAL_HOSTS`), so `github.com.evil.com` gets no token.
4. `_install_dependencies`: runs `npm install` or `pip install -r requirements.txt` **inside** the container. Nothing is mounted from the host (the docstring says "there is no host mount of any kind", and the `volumes=` argument above confirms it).
5. If anything fails, `_cleanup_after_failure` removes the container.

### Running the agent (`sandbox_monitor.supervise_in_sandbox`)
- The adapter doesn't start a process; it returns a **description** (`adapters/base.py: SandboxInvocation(argv, env)`). `Sandbox.exec_stream` runs it inside the container with a pseudo-terminal.
- `env` is a **whitelist**, not a copy of the host environment:
  - Codex gets only `CODEX_API_KEY` (`codex.py: sandbox_invocation`).
  - Kiro gets only `KIRO_API_KEY` (`kiro.py: sandbox_invocation`).
  - Antigravity and mock get `{}`.
- The base class `Agent_Adapter.sandbox_invocation` **refuses** rather than falling back to running on the host.
- A small Python watcher is uploaded and run inside the container (`_WATCHER_PROGRAM`). It polls the file tree every 0.15 s and records created/modified/deleted files. It skips `.git`, `node_modules`, `.venv`, `__pycache__`, and the `.mypy_cache` and `.pytest_cache` directories (`_WATCH_EXCLUDES`).

### Getting the result out (`trusted_capture.py`)
The agent runs as **root inside the container**, so everything in it is treated as untrusted, including its `git`. After the run, VaultShield:
- kills the container,
- copies the files out through the Docker API (`get_archive`) rather than running anything inside it,
- rebuilds the repo in a **new** helper container (`network_mode="none"`, no capabilities) and runs git there.

This stops a hostile agent from tampering with the diff through git hooks or config.

### How `.env.sandbox` is made (`credentials.py`)
For each `KEY=value` in the source `.env` (read on the host, by the CLI process only):

| Real value looks like | Becomes |
|---|---|
| empty | stays empty |
| `http(s)://…` with no user:pass | `https://example.invalid/dummy` |
| 16+ chars of `[A-Za-z0-9_-]` (API-key-like) | fresh random string of the same length (`secrets.choice`) |
| `postgres://`, `mongodb://`, `mongodb+srv://`, `redis://`, `mysql://`, etc., or http(s) with `user:pass@` | `scheme://dummy_user:dummy_password@HOST:port/path…`. HOST is kept only if it is `localhost`, loopback, or RFC1918 private; otherwise replaced with `localhost` (`_is_preservable_host`). |
| anything else | `dummy-value` |

Other rules:
- Key names and order are kept.
- Blank lines and `#` comments are copied **verbatim**.
- Lines that aren't `KEY=value` are dropped.

Tests covering this are in `cli/tests/test_credentials.py` (for example, the "Atlas cluster hostname that leaked no longer survives" case, and the no-secret-fragment-survives properties). I did not run them.

**Important accuracy point:** the file is mounted at `/workspace/.env.sandbox`, which is *outside* the repo folder `/workspace/repo`. I found no code that loads it into the app's environment. `browser_capture._start_app` starts the app with only `PORT`, `HOST`, and `HOSTNAME`. So the fake file is simply "there to be found". Whether your app reads it depends on your app. I did not verify that.

### Optional: network lockdown (`egress.py`, `egress_proxy.py`)
Off by default. With `--egress log-only` or `allowlist`:
- clone and install run on a normal network,
- then the container is moved to an `internal` network whose only exit is a filtering proxy container (`EgressGuard.isolate`),
- the proxy lets through only allowlisted hosts (`BUILTIN_ALLOWLISTS`: for example, Google API hosts for Antigravity, `api.openai.com` for Codex),
- an in-container check proves direct internet fails before the agent starts (`_ISOLATION_CHECK`).

Root in the container can't undo this: no `NET_ADMIN`, no Docker socket, `NET_RAW` dropped. Codex and Kiro allowlists are marked "unverified" in the code (`BUILTIN_STATUS`).

---

## How the real .env stays protected (and limits)

### Mechanisms that exist (each with its file and function)

1. **Process isolation: the agent never runs on the host.**
   `sandbox_monitor.supervise_in_sandbox` runs the agent through `Sandbox.exec_stream`. The module docstring records that an earlier design ran the agent on the host with the real `.env` in reach, and this was deliberately replaced. `cli.py: run` says the host repo root "is never the agent's workdir".

2. **No host mount except the fake file.**
   `Sandbox.create` has exactly one bind mount: `<run_dir>/.env.sandbox` at `/workspace/.env.sandbox`, read-only. There is no mount of the repo, home directory, or Docker socket. The `isolation_probe.py` script (in `cli/`) is a manual test that tries things like reading `/host/.env` and the Docker socket from inside a sandbox.

3. **The real `.env` can't come in through git.**
   The sandbox gets code by `git clone <repo_url>`, so only committed files arrive. The root `.gitignore` ignores `.env` and `.env.*`. I confirmed the following without reading contents:
   - `git check-ignore -v .env` matches `.gitignore:9`.
   - `git ls-files` lists only `frontend/.env.example`.
   - Git history (all refs) has no `.env` file names.

4. **Fake values only reach the container (`credentials.py: build_sandbox_env`).**
   Real values are held in host memory only to classify them. The output is built from `generate_dummy`, which never derives from the input. If a dummy happened to contain the real value, it falls back to a constant. The file is written atomically and mode 0600.

5. **Env whitelist for the agent process.**
   `SandboxInvocation.env` carries only the one agent key. `Sandbox.exec_stream` documents that the host environment must never be copied in. Host `os.environ` is not forwarded. The Kiro adapter's host-side `_build_env` does copy `os.environ`, but that is only used by `launch()`, which `run` never calls.

6. **Git token for private clones is one-shot.**
   See `Sandbox._clone_repo` and `submit.git_credential_args`. It is in the environment of one `exec` only and is redacted from error messages (`_redact`).

7. **Container never gets the Docker socket and the capture is done outside it.**
   `trusted_capture.py`, as above.

8. **Leak detection on the way out.**
   `token_hygiene.py` snapshots the agent's own credential file(s) (Antigravity OAuth token) through the Docker API, then scans `diff.json`, `events.jsonl`, and changed files for those bytes before upload. A match raises `TokenHygieneViolation` and the run fails. `uploader.upload_run(..., scrub=secrets.scrub)` also masks them in event payloads.

9. **`off_limits_paths` (tripwire, not a wall).**
   `.vaultshield.yml` lists paths (currently `protected.txt`). If the in-container watcher sees one changed, the run is recorded as a `violation` event and fails (`_collect_file_changes`, `_match_container_path`, `OffLimitsViolation`).

### Honest gaps and limits (things that are NOT protected)

- **Network is open by default.** `egress` defaults to `off`. An agent can send anything it can see to any server. That includes the fake `.env.sandbox` (harmless) and the **real agent API key** (not harmless; see next point). Turning on `--egress allowlist` narrows this a lot, but the Codex and Kiro allowlists are marked "unverified", and for Antigravity the allowlist includes `www.googleapis.com`, which the code comments admit is an accepted residual exfiltration path.
- **The agent's own API key is real.** For Codex and Kiro, `CODEX_API_KEY` / `KIRO_API_KEY` is read from your host environment and injected into the agent process. The agent can read it. Token hygiene does **not** cover API keys in the exec env. `adapters/base.py: credential_paths` says an API key in the exec env "is not covered". So if the agent prints it, it can end up in `events.jsonl` / `diff.json` and be uploaded.
- **Antigravity's Google session is real and broad** (`cloud-platform` scope). `cli.py: _authenticate_in_sandbox` prints this warning itself: the agent can read its session and VaultShield can't stop it from using it.
- **Comments in `.env` are copied verbatim into `.env.sandbox`** (`build_sandbox_env`: blank and `#` lines are appended unchanged). A real secret in a comment (for example `# old key: sk-…`) would leak into the sandbox. This is by design in the code but is a real hole.
- **Connection-string path and query are kept** (`_generate_dummy_connection_string` keeps `rest`). A secret in a query string such as `redis://localhost:6379/0?password=…` would survive. Only userinfo and non-local hosts are replaced.
- **Key-shaped values keep their exact length** (random, same length). This is a small information leak. Key *names* (such as which services you use) are always visible.
- **`off_limits_paths` is detective, not preventive.** It reports after the agent finishes, it only sees *changes* (not reads), and it can't see inside the excluded directories (`.git`, `node_modules`, etc.). It is also repo-relative, so it protects nothing about files outside the clone. It does not protect the real `.env` at all, because that file isn't in the container.
- **No container hardening beyond Docker defaults.** `containers.create` sets no `user`, `mem_limit`, `pids_limit`, `read_only`, or `security_opt`. The agent is root inside the container. The only extra restriction is `cap_drop=["NET_RAW"]` and `sysctls`, and only when egress is on. Container-escape bugs or resource exhaustion are not addressed by VaultShield. Docker Desktop's VM is the real outer wall on macOS.
- **Host-side browser touches agent-controlled content.** Playwright on your machine opens `http://127.0.0.1:<port>` for the app the agent changed (`browser_capture._playwright_session`). That is a small attack surface. I did not assess it further.
- **The earlier sandbox variant is documented as a bug.** If anyone calls `Monitor.supervise` (still in `monitor.py`) directly, the adapter's `launch()` runs on the host. `run` does not call it. Only tests use it.
- **Uploaded artifacts are only as clean as what the agent prints.** Anything non-token-shaped that the agent echoes (for example your fake file, or real data it pulled over the network) is uploaded and, for some existing runs, committed to git.

### Observation (not investigated further)
`.vaultshield/runs/20261009-092644-1f5539e91e1492b6/.env.sandbox` is an empty **directory**, not a file. That's the classic result of Docker auto-creating a missing bind-mount source. The current `Sandbox.create` checks `env_file.is_file()` first, so it is probably leftover from an earlier run or another script. I did not trace which. It does not expose anything.

---

## Website login flow

### What exists

**Backend** (Django + Django REST Framework, MongoDB):
- `backend/vaultshield_backend/urls.py`: `path("api/auth/login/", LoginView.as_view())`.
- `backend/accounts/views.py: LoginView(ObtainAuthToken)`:
  - `permission_classes = [AllowAny]` and `authentication_classes = []`. Login needs no token, and a stale token in the headers can't block it.
  - DRF's `AuthTokenSerializer` calls Django's `authenticate()`, so a bad username, bad password, or inactive user returns HTTP 400 with no token.
  - On success it runs `Token.objects.get_or_create(user=…)` and returns `{"token": "<key>"}`.
- `settings.py: REST_FRAMEWORK`:
  - `DEFAULT_AUTHENTICATION_CLASSES = [TokenAuthentication]`
  - `DEFAULT_PERMISSION_CLASSES = [IsAuthenticated]`
  - Session and Basic auth are deliberately **not** enabled for `/api/`.
  - Every API route except login therefore needs `Authorization: Token <key>`. Projects, runs, events, diff, screenshots, approve, and reject all inherit this (see comments in `projects/views.py`, `runs/views.py`).
- Users are Django's built-in `User` (no custom model). Apps in `settings.py`: `rest_framework.authtoken`, plus Mongo-flavored auth and sessions.

**Frontend** (React + Vite, in `frontend/`):
- `src/api.js: createApiClient`:
  - `login({username, password})` POSTs JSON to `/api/auth/login/` with `auth: false`, then stores the token via `tokenStore.set`.
  - Token store (`createBrowserTokenStore`): `localStorage` under the key `vaultshield.dashboard_auth_token`, with an in-memory fallback.
  - `request()` adds `Authorization: Token <token>` to every call except login.
  - `logout()` only clears the local copy ("DRF tokens have no revoke endpoint in the MVP").
- Dev server proxies `/api` and `/media` to the backend (`vite.config.js`), so the browser stays same-origin and no CORS is configured.

### What is missing (the important finding)
- **No login form.** `App.jsx` renders only `ProjectListView`, `RunHistoryView`, `RunDetailView`. A search of `frontend/src/*.jsx` finds no password input, no form, and no caller of `api.login`, `api.logout`, or `isAuthenticated`.
- On a 401 the views just show text like "Sign in to load projects. Every API path except login requires a dashboard auth token." (`format.js: describeLoadFailure`) and "Sign in to record a decision" (`RunDetailView.jsx: describeDecisionFailure`). Nothing tells you how.
- So today, a browser only works if a valid token already sits in `localStorage` (for example, pasted by hand in DevTools, or `api.login` invoked in a console). `frontend/README.md` documents `api.login` only as a code snippet, and says "before signing in" without any UI. I could not run the app, so this is from reading the code. I'm confident because the exhaustive greps for login callers found only `api.js` and tests.
- **No signup.** I found no registration endpoint. Create users with Django admin (`/admin/`, which uses its own session-cookie login) or `python manage.py createsuperuser`. I did not find that command documented in the READMEs.
- **Passwords:** the app never stores or hashes passwords itself. Django's `User` model does. `settings.py` has no `PASSWORD_HASHERS` override, so Django's default applies (PBKDF2-SHA256 in current Django; I did not open Django's source to confirm for the installed version). `AUTH_PASSWORD_VALIDATORS` includes similarity and minimum-length checks (those only apply when passwords are set through Django's validators, such as in admin forms).

### Step by step (how it is designed to work)
1. User types username and password in a form (**form not built yet**).
2. Frontend: `api.login()` → `POST /api/auth/login/` with JSON body, no auth header.
3. Backend `LoginView` → `AuthTokenSerializer` → `django.contrib.auth.authenticate` verifies the password hash.
4. On success: get or create the user's `Token`, return `{"token": "…"}`. On failure: HTTP 400, no token.
5. Frontend stores the token in `localStorage`.
6. Every later call sends `Authorization: Token …`.
7. Backend `TokenAuthentication` looks the token up in the token table. Unknown token → 401. No header → 401. Valid → the view runs.
8. Approve/reject buttons call `POST /api/runs/<id>/approve/` or `/reject/` with the same header (`RunViewSet._record_decision`).

### Weak spots in the backend auth (verified by reading settings, not by testing)
- Token has **no expiry and no revocation**. Logout doesn't invalidate it server-side.
- I found **no throttling** of the login endpoint (no `THROTTLE` settings in `backend/`).
- Defaults are dev-friendly: `DEBUG` defaults on, `SECRET_KEY` defaults to a `django-insecure-…` value (`VAULTSHIELD_SECRET_KEY`, `VAULTSHIELD_DEBUG`). With `DEBUG` on, the browsable API is enabled.
- Token in `localStorage` is readable by any script on the page (XSS). Tokens go over plain `http://localhost` by default; no TLS setup is in the repo.
- No per-user scoping: I found no `request.user` use in `runs/`, `projects/`, or `accounts/`. Any valid token can see and decide any run, and I found no record of *which* user approved. I only grepped for `request.user`, so confirm in `runs/views.py: _record_decision` if this matters to you.

---

## CLI login vs website login

| | CLI (`vaultshield login`) | Website |
|---|---|---|
| Code | `uploader.py: login` → `_request_token`, `store_token` | `api.js: login` (no UI calls it yet) |
| Endpoint | `POST /api/auth/login/` | same |
| Credentials | username + password, prompted (password is never a CLI option, to keep it out of shell history) | username + password (form missing) |
| Result | DRF `Token` | the same DRF `Token` |
| Stored at | `~/.vaultshield/token`, file mode 0600 inside a 0700 dir, written via `os.open` + `fchmod` before any content | `localStorage["vaultshield.dashboard_auth_token"]` |
| Sent as | `Authorization: Token <t>` (`uploader._auth_headers`) | `Authorization: Token <t>` |
| Used for | register/upload/complete runs, `submit`, `teardown` | list projects/runs, view detail, approve/reject |

Because DRF issues one token per user (`get_or_create`), logging in from the CLI and the browser as the same user gives the **same token string**. Log in once in either place and you hold the same key. There is no per-device token.

The CLI never prompts for credentials mid-run. `run` only reads the stored token, and if it is missing, `run` stops before creating a sandbox ("Run `vaultshield login` first").

Not the same thing: `--interactive-auth` on `vaultshield run` is the Antigravity agent's **Google OAuth** inside the sandbox (`cli.py: _authenticate_in_sandbox`, `sandbox.py: exec_interactive, InteractiveExec`). The code is typed through a socket, never put in argv, env, or the event log, and is masked if the terminal echoes it. The notes `interactive-auth-diagnosis.md` and `interactive-auth-review.md` are about repairing the tests for that feature. They say nothing about dashboard login.

---

## Quick file map

```
.vaultshield.yml                 project config for the CLI (start_command, port, routes, off_limits_paths)
.vaultshield/runs/<id>/          per-run output: events.jsonl, diff.json, screenshots/, .env.sandbox
.gitignore                       ignores .env, .env.*, .env.sandbox
cli/vaultshield/
  cli.py                         commands + the `run` pipeline
  config.py                      read/validate/write .vaultshield.yml, framework detection
  credentials.py                 .env -> fake .env.sandbox (NOT token storage)
  sandbox.py                     Docker container lifecycle, clone, exec, teardown
  sandbox_monitor.py             run agent in container, in-container file watcher, off_limits_paths check
  adapters/{base,codex,kiro,antigravity,mock}.py   what to run + env whitelist per agent
  egress.py, egress_proxy.py     optional network allowlist (proxy + internal network)
  trusted_capture.py             take results out via Docker API, rebuild repo in a helper
  git_capture.py                 commit branch vaultshield/run-<id>, write diff.json
  token_hygiene.py               find the agent's credential in outputs before upload
  browser_capture.py             start app in container, Playwright screenshots
  run_directory.py               run id + folder layout
  uploader.py                    login, token file, all HTTP to the backend
  submit.py, teardown.py, providers/{github,gitlab}.py   PR/MR creation and cleanup
  monitor.py                     event log format; older host-side Monitor (not used by `run`)
cli/sandbox-image/Dockerfile     pinned image with git, python, node, agy; no credentials
cli/isolation_probe.py           manual adversarial test of the sandbox boundary
cli/tests/                       test_credentials, test_sandbox_execution, test_egress, test_token_hygiene, ...
backend/
  accounts/views.py              LoginView (POST /api/auth/login/)
  vaultshield_backend/urls.py    routes; settings.py REST_FRAMEWORK auth/permission defaults
  runs/views.py, projects/views.py   token-protected API (create/ingest/approve/reject)
frontend/src/
  api.js                         API client, token in localStorage, login()/logout()
  App.jsx                        shell (no login UI)
  ProjectListView / RunHistoryView / RunDetailView .jsx   the dashboard views
frontend/vite.config.js          /api proxy to backend
docker-compose.yml               redis, web (gunicorn :8000), channels, celery-worker, celery-beat, frontend (:5173)
```

---

## Conclusions and recommendations

Nothing below was implemented.

**Conclusions**
1. The sandbox design is sound at its core. The real `.env` is protected because the agent runs in a container that has no path to it. The code arrives by `git clone`, `.env` is gitignored and untracked, and the only file mounted in is a read-only fake. I verified this by reading `Sandbox.create`, `.gitignore`, and git metadata, not by running an attack.
2. The protection is about the *file*, not about *all secrets*. The agent API key and any Google session are real inside the sandbox. With egress off, the network is open.
3. The website cannot be logged into from the UI today. The backend is ready; the frontend login screen is missing.

**Recommendations (highest value first)**
1. **Build a dashboard login page** that calls `api.login`, shows a "log out" button (`api.logout`), and switches to it when any request returns 401. Without it the dashboard is effectively unusable for new browsers.
2. **Default `--egress` to `allowlist`** for agents with a verified list, or at least print a loud warning when `off` is used with a real API key. Verify the Codex and Kiro allowlists (marked "unverified" in `egress.py: BUILTIN_STATUS`).
3. **Fix the two `.env.sandbox` leak paths** in `credentials.py`: drop comments instead of copying them (or replace them with a blank line), and drop or sanitize the query string and path of connection strings.
4. **Add the agent API key to the token-hygiene needles** (`SecretSet`) so `KIRO_API_KEY` / `CODEX_API_KEY` get scrubbed from events and diffs and trigger `TokenHygieneViolation`.
5. **Harden the container** in `Sandbox.create`: run as a non-root `user`, set `mem_limit`, `pids_limit`, `cap_drop=["ALL"]` (add back only what is needed), and `security_opt=["no-new-privileges"]`. Check each against the agents' needs first.
6. **Backend auth hardening:** add login throttling (`DEFAULT_THROTTLE_*` or a per-view throttle), token expiry or rotation plus a logout endpoint that deletes the token, and record `request.user` on approvals. Require `VAULTSHIELD_SECRET_KEY` and `DEBUG=0` outside development, and serve behind TLS.
7. **Document user creation** (for example `python manage.py createsuperuser`, or a `create_user` command) in `backend/README.md`, since there is no signup.
8. **Optional:** use an `httpOnly` cookie or short-lived tokens instead of `localStorage` if the dashboard will ever be exposed beyond localhost.

**What I could not verify**
- No sandbox was run, no attack was attempted, and no tests were executed. All findings come from reading code and configuration.
- I did not read the real `.env` or any `.env.sandbox`, so I cannot say which keys it holds. `backend/README.md` says the repo-root `.env` carries the MongoDB connection string for docker compose; I took that from the README only.
- Django's default password hasher and `Token` behavior come from the code's own comments and Django's documented defaults. I did not open the installed Django source (the brief excluded `backend/.venv`).
- Whether your app actually reads `/workspace/.env.sandbox` is not determinable from the CLI code.
