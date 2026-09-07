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
  --repo-url=https://github.com/<owner>/<repo>
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

Its session is stored as a single file on the host that ran `agy` interactively:

```
~/.gemini/antigravity-cli/jetski_state.pbtxt      (mode 600)
```

Making a Sandbox authenticated means placing that file inside the container.
VaultShield does **not** do this implicitly, and the reason is worth
understanding before you do it by hand: that token carries broad Google scopes
(`cloud-platform`, `userinfo.email`, `userinfo.profile`), so it is a much wider
credential than the per-service API keys the Codex and Kiro adapters inject. It
is your call, not a default.

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
