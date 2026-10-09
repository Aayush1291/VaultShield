# VaultShield CLI

The `vaultshield` command-line tool: sandboxes AI coding agent runs in
Docker, captures every action, and uploads the result to the VaultShield
dashboard for human review before anything reaches a real branch or pull
request.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```

## Running a real agent: the Sandbox image

`vaultshield run` defaults to the `python:3.11` base image, which has `git` and
`python` — enough to clone a repository and to run the Monitor's file-change
watcher — but no agent CLI and no Node.

`sandbox-image/` builds the image the real Antigravity CLI needs:

```bash
cd cli
docker build -t vaultshield-sandbox:local sandbox-image
```

It contains, and nothing much beyond:

| tool | needed by |
|---|---|
| `agy` | the Antigravity agent, pinned to a checksum-verified Linux build |
| `git` | `Sandbox` clones into `/workspace/repo`; git capture commits and diffs there |
| `python` | the Monitor's in-container file-change watcher, exec'd as `python` |
| `node` / `npm` | a Node target application's `start_command` and dependency install |

Then point a Run at it:

```bash
vaultshield run \
  --agent=antigravity \
  --base-image=vaultshield-sandbox:local \
  --prompt="add a health check endpoint" \
  --repo-url=https://github.com/<owner>/<repo> \
  --project=<project-id>
```

The image contains **no credentials of any kind** — no agent token, no
repository token, no Google session — and no host path is copied into it. Each
credential reaches the container scoped to the one process that needs it: the
repository-host token as the environment of the single `git clone` exec, and the
application's variables as the read-only `.env.sandbox` mount, which holds dummy
values only.

### Antigravity authentication

`agy` authenticates through Google OAuth and reads no API-key environment
variable (`GEMINI_API_KEY` and `GOOGLE_API_KEY` are both ignored). A Sandbox
built from a clean image is therefore unauthenticated, and a Run will reach
`agy`'s consent prompt and stop there.

Its session is stored as a file under `~/.gemini/` on the host that ran `agy`
interactively. A real sign-in probe confirmed the OAuth token is
`~/.gemini/antigravity-cli/antigravity-oauth-token` (mode 600), next to the
session state `antigravity-cli/jetski_state.pbtxt` (mode 600); token hygiene
reads those two files. `jetski_state.pbtxt` was inspected structurally
(onboarding steps, an installation UUID, migrations; no OAuth value) and is
kept as a precaution. `config/config.json`, `cache/*.json`, lock files,
`brain/` transcripts and `conversations/*.db` are not read, and
`antigravity-cli/cli.log` (a symlink) is never followed.

Making a Sandbox authenticated means placing that file inside the container.
VaultShield does **not** do this implicitly, and the reason is worth
understanding before you do it by hand: that token carries broad Google scopes
(`cloud-platform`, `userinfo.email`, `userinfo.profile`), so it is a much wider
credential than the per-service API keys the Codex and Kiro adapters inject. It
is your call, not a default.

## Registering the Run: `--project`

`vaultshield run` registers its Run with the backend before it does anything
else, so it needs a backend Project id. It is resolved in this order:

1. `--project <project-id>`
2. `project: <project-id>` in `.vaultshield.yml` (`init` does not write it)
3. otherwise the Run stops before anything is created:
   `No project for this Run: pass --project <project-id> or set ...`

The id is the Project's 24-character hex id. `GET /api/projects/` lists each
Project's `id`; on the dashboard it is the `data-project-id` attribute of the
project card (not shown as text). The CLI does not check its format; an
unknown id is refused by the backend and reported.

The backend must be reachable, and you must have run `vaultshield login`,
**before** anything runs. Right after the Run_Directory is created, `run` loads
the token and POSTs the Run (`POST /api/runs/`). If the token is missing, the
backend is unreachable, the token is rejected, or the Run is refused, `run`
stops with one line ending `(no Sandbox was created)`. No container is created.
The empty `.vaultshield/runs/<run_id>/` directory is left behind.

A registered Run moves `running → awaiting_review` or `running → failed`:

- **Success:** after the last artifact (events, diff, then screenshots) is
  uploaded, `run` PATCHes the Run to `awaiting_review`, and it is ready for a
  reviewer.
- **Any failure after registration** (bad `--env-file`, Docker not running,
  Sandbox, sign-in, agent, off-limits violation, screenshots, git capture,
  upload, the completion PATCH, or Ctrl-C): the Sandbox is torn down first,
  then `run` makes one best-effort PATCH to `failed`. That PATCH never hides
  the original error. If it fails itself, you get at most one extra
  `Warning: could not mark run ... as failed` line. Artifacts stay on disk.
  `failed` is final: the backend only moves a Run to `awaiting_review` from
  `running`.

Some cases still leave a Run stuck in `running`, because no code gets a
chance to mark it: a `run` that is SIGKILLed or loses power, a registration
whose response was lost after the backend saved it (a timeout), and Ctrl-C
during registration itself. A server-side reaper for these is a follow-up.

## Configuration: `project_dir`

`.vaultshield.yml` assumes the application lives at the repository root. When it
does not — a repo whose `package.json` sits in a subdirectory — name that
subdirectory:

```yaml
agent: antigravity
project_dir: citizen-samadhan-setu-main
start_command: npm run dev
port: 3000
ready_path: /
routes:
  - /
off_limits_paths: []
```

`project_dir` is optional; omitting it means the repository root, which is what
every configuration written before the field existed meant implicitly.

It changes three things, all inside the container:

- dependency install runs there, so a nested `package.json` is actually found;
- the agent process starts there;
- `start_command` runs there.

It deliberately changes two other things not at all:

- **cloning.** The clone always lands at `/workspace/repo`, so `.git` stays where
  git capture commits and diffs it, and the whole repository is still what gets
  reviewed.
- **the file-change watcher.** It stays rooted at the repository, so a change the
  agent makes *outside* the application directory is still captured, and
  `off_limits_paths` keeps its documented repository-relative meaning rather than
  silently switching base.

Because the value becomes a container working directory, it is validated before
anything is created — a Run with a bad `project_dir` stops at step 1, with no
Run_Directory, no credential file and no container. Absolute paths, Windows
paths, and any `..` segment are refused, and the resolved path is independently
re-checked to be inside `/workspace/repo`.

## Screenshots and `routes`

Browser capture runs as part of the pipeline, but an empty `routes: []` skips it
entirely — the application is not started and readiness is not polled, since
there is nothing to capture. Configure `routes` to capture the running
application.

## Diff capture: patches and size limits

`diff.json` has one entry per changed file with `path`, `additions` and
`deletions`. Each text file also carries `patch`, its `git diff -U0` text,
which the backend's security scan reads. Binary files have no `patch`.

- **Per file:** a patch is capped at `MAX_PATCH_BYTES` (200 KiB of UTF-8). A
  longer one is cut at a line boundary, so no line is split, and the entry gets
  `"patch_truncated": true`. The counts still describe the whole file.
- **Whole diff:** the upload body is capped at `MAX_DIFF_REQUEST_BYTES` (2 MiB),
  sized under the backend's 2.5 MiB `DATA_UPLOAD_MAX_MEMORY_SIZE`. Patches fill
  the budget in git's file order. Once it is spent, later text files get
  `"patch": ""` with `"patch_truncated": true`, and the scan does not see them.
- **Very many files:** if the per-file metadata alone (thousands of changed
  files) is over `MAX_DIFF_REQUEST_BYTES`, `vaultshield run` fails with a clear
  error naming the size and the cap. It writes no `diff.json`. It never drops
  entries or uploads an oversized body.
- **NUL characters:** git treats a file as text if its first 8000 bytes have no
  NUL, so a text patch can contain one later on. Each NUL is sent as U+FFFD,
  because the backend rejects strings containing NUL.
- **Known gap:** "binary" is decided by git, which honours `.gitattributes`. A
  committed `path -diff` attribute makes a text file look binary, so it gets no
  `patch` and the security scan does not read it.
- **Known gap: non-UTF-8 file names.** git reports the raw name, but it cannot
  be passed back to git, so the entry has no `patch` and has
  `"patch_truncated": true`. The scan does not read that file, and the flag
  shows it. The stored `path` spells the bad bytes as U+FFFD.

The diff base is the capture commit's parent. When the capture commit has no
parent (the Sandbox's clone was an empty repository), the base is git's empty
tree, so every committed file is reported as added.

## Egress control and token hygiene (Batch 3)

### `egress` / `--egress`

```yaml
egress:
  mode: allowlist          # off | log-only | allowlist   (default: off)
  allow:                   # optional, unioned with the agent's built-in list
    - api.example.com
    - "*.example.org:8443"
```

`run --egress off|log-only|allowlist` overrides `mode` (flag, then config, then
`off`). Entries are `host[:port]` (default port 443): a DNS name with at least
two labels, `*.` plus such a name, or a global IP literal (`[v6]` in brackets).
Schemes, paths, userinfo and private IPs are rejected when the config loads.

With `off` (the default) nothing changes. With `log-only` or `allowlist`:

1. Before the Sandbox exists, VaultShield creates `vaultshield-open-<id>`,
   `vaultshield-int-<id>` (`internal: true`) and the filtering proxy
   `vaultshield-egress-<id>` (in-repo stdlib proxy in the pinned
   `python:3.11.16-slim@sha256:e416…b41e` image; pulled on first use).
2. The Sandbox is created on the open network, so `git clone` and the
   dependency install have network.
3. Before any agent code (the `agy` sign-in included) runs, the Sandbox is
   moved to the internal network only. Its one peer is the proxy, which allows
   `CONNECT host:port` (via `HTTPS_PROXY`) or TLS by SNI only for allowlisted
   hosts that resolve to public addresses. `NET_RAW` is dropped and IPv6 is off.
   A root process in the Sandbox cannot undo this: it has no `NET_ADMIN`, no
   Docker socket and no route, and the proxy refuses to forward packets.
   The internal bridge has no gateway address (`inhibit_ipv4`), so the Docker
   VM's own listeners (rpcbind :111, a Docker Desktop service on :63289,
   measured open on a plain internal bridge) are unreachable too.
4. The app at step 7 is screenshotted through the proxy's published port.

In `allowlist` mode the agent cannot install new dependencies and the app has
no internet. Every denial (and in `log-only`, every unlisted host) becomes a
`violation` event in `events.jsonl` and the dashboard timeline; denials never
fail a Run. The raw proxy log is kept in `egress.jsonl` in the Run_Directory.
An app `port` of 443 or 3128 is refused in these modes (the proxy's own ports).

| Agent | Built-in list | Status |
|---|---|---|
| antigravity | `oauth2.googleapis.com` (sign-in), `daily-cloudcode-pa.googleapis.com` (model API), `cloudcode-pa.googleapis.com` (production endpoint; not contacted by the probe, kept from the binary's strings), `www.googleapis.com` (post-sign-in eligibility check, `/oauth2/v2/userinfo`; see the risk below) | enforced; hosts from the real log-only and allowlist probes |
| codex | `api.openai.com` | unverified |
| kiro | `runtime.us-east-1.kiro.dev`, `management.us-east-1.kiro.dev` | unverified |
| mock | none (deny-all) | verified |

**Risk of `www.googleapis.com`:** without it `agy`'s sign-in fails
(`Eligibility check failed: … userinfo: Forbidden`). The proxy filters by host,
not URL path, so listing it also allows that host's other Google REST APIs,
including the Cloud Storage JSON upload API. An agent holding the
`cloud-platform` token could upload repository data to a bucket in your own
Google project, and no `violation` event would be recorded.

Seen in the probes but deliberately not listed: `storage.googleapis.com`
(Cloud Storage upload), `play.googleapis.com`,
`antigravity-unleash.goog` and the `*.run.app` auto-updater (telemetry, flags,
updates), `lh3.googleusercontent.com` (avatars), and the
`playwright*.azureedge.net` CDNs (browser downloads). Their denials show up as
`violation` events.

### Trusted capture and token hygiene (every mode)

Step 8 kills the Sandbox, then rebuilds its repository in a fresh
`vaultshield-capture-<id>` helper container (same image ID, no network, no
capabilities) from the pre-agent base commit and a filtered Docker-API copy of
the work tree; the branch and `diff.json` come from that helper's git, never
from the agent's. Before upload, `diff.json`, `events.jsonl` and the changed
files are scanned on the host for the agent's credential bytes (snapshotted
from the agent's credential files through the Docker API; symlinks there are
never followed) and for `/root/.gemini` path
literals. On a hit the Run fails, a `violation` event is recorded, the diff
and screenshots are not uploaded, and `diff.json` becomes
`diff.quarantined.json`. Event payloads are always uploaded with credential
needles replaced by `***`, and a failed Run still uploads its timeline.

Not covered: a credential rendered into a screenshot (images are not
scanned), and deliberately encoded exfil.

### Isolation probe (real `agy`)

```
.venv/bin/python isolation_probe.py --agent antigravity --interactive-auth --egress log-only
.venv/bin/python isolation_probe.py --agent antigravity --interactive-auth --egress allowlist
```

Results land in `~/.vaultshield/probes/`. `vaultshield teardown <run-id>` also
removes a Run's capture helper, proxy, networks and proxy staging dirs.
