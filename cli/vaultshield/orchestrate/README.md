# VaultShield Orchestrate

Before an AI coding agent starts, `vaultshield ask` reads your project, asks you a few
clarifying questions, and builds one enriched prompt, so the agent guesses less.

It calls an AI model (Gemini) to do this, so the one rule is: **the model never gets a
secret value or the contents of a secret file.** This page says exactly what it does get,
how that is enforced, and where the enforcement is only a best effort.

It does **not** start any agent. It writes a prompt; you give it to the agent.

## Usage

```
vaultshield config set-key gemini    type your Gemini API key at a hidden prompt
vaultshield config show-key gemini   is a key set, and its last 4 characters
vaultshield ask "Add a login page"   questions, then the prompt
```

`ask` options:

- `--yes` skips the questions (and the model call that writes them). For scripts.
- `--answers FILE` reads answers from a JSON file, `{"question id": "answer"}`, instead of
  asking. The ids come from the questions the model writes; the fallback questions use
  `scope`, `constraints` and `off_limits`.
- `--copy` also puts the prompt on the clipboard (macOS `pbcopy`).

The prompt is saved as `.vaultshield/context/<timestamp>.md` and printed to stdout. The
questions and notes go to stderr, so `vaultshield ask "..." --yes | pbcopy` copies only the
prompt. Run it from the project folder or any folder below it.

### The prompt has five parts

Task, Project summary, Answers, Constraints, and Suggested approach. The approach comes from
a second model call; if that call fails the section is simply left out. Constraints always
contains this, word for word:

> Secret values are protected placeholders like vs_protected. Never ask for, print, or
> hardcode real secrets; refer to variables by NAME and read them from the environment. If a
> command needs real values, tell the user to run it with `vaultshield exec`.

### The key

Looked up in this order: the environment variable `GEMINI_API_KEY`, then the macOS Keychain
(service `vaultshield`, account `model-key:gemini`). With no key, `ask` stops with one line
telling you to run `set-key`. The key is read at a hidden prompt, never from the command line
(a key typed as an argument is refused without being repeated), and only its last 4
characters are ever shown, and only for keys of 8 or more characters.

### The model

Default `gemini-3.8-flash`, the model Google's docs list as Stable and use in all their
current examples. Change it in `~/.vaultshield/config.yml`:

```yaml
orchestrator:
  model: gemini-3.5-flash-lite
```

There is no command to set it. The id is checked, because it goes into the request.

### The call log

Every model call adds one line to `.vaultshield/orchestrator.jsonl`: time, provider, model,
size of the request in bytes, and the outcome (`ok`, `http_503`, `timeout`,
`network_error`, `invalid_reply`). No content. Writing it is best effort.

## What is sent to the model

- Your task and your answers.
- The project folder's name; the start command and port that `vaultshield init`'s detection
  finds.
- The **names** of dependencies from `package.json`, `requirements.txt` and `pyproject.toml`
  (a version is included only if it is a plain one like `^4.18.0`; URLs and git references
  are dropped).
- A file listing: names only, down to 3 levels, at most 200 entries. It skips `node_modules`,
  `.git`, `.venv`, `__pycache__`, `dist`, `build` and `.vaultshield`, and symlinks.
  **This includes the names of your secret files** (`.env`, `certs/server.pem`).
- The first 4000 characters of the README.
- The last 10 git commit subjects.
- The **names** of the variables VaultShield protects, grouped by file.
- Fixed instructions, and the model id.

The API key is sent in the `x-goog-api-key` header, as Google's docs show, never in the URL
or the body. Every request also sets `store: false`: Google's Interactions API otherwise
stores requests (55 days on the paid tier, 1 day on the free tier, per its docs).

## What is never sent

- **The content of any secret file**: anything `vaultshield protect` discovery flags
  (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials.json`, `.npmrc`, `.pypirc`, `.netrc`,
  `.aws/credentials`) or that is listed in `.vaultshield/protect.json`. These are never
  opened. The scanner reads only a fixed list of files, and refuses symlinks, so a `README.md`
  that points at a `.env` is not read.
- **Any value stored in the vault** for this project.
- The API key, except in the header.

### How that is enforced

1. Secret files are not read (above). A test records every file the scan opens.
2. Every piece of text then goes through `sanitize`, which replaces with `***`:
   every value stored in the vault for this project (loaded from the Keychain, in memory only),
   your API key, and anything shaped like a secret (`sk-…`, `AKIA…`, `ghp_`/`gho_…`,
   `github_pat_…`, `xox…`, `AIza…`, `ya29.…`, JWTs, `-----BEGIN … PRIVATE KEY-----` blocks,
   `Bearer …`, `user:password@` in URLs, `password=…`/`token: …` style assignments, and
   tokens of 32+ letters, digits and `-_+=` that mix upper case, lower case and digits).
   The README is cut to 4000 characters **after** sanitizing, so a key cut in half by the limit
   cannot leave a fragment.
3. `assert_clean` re-checks the text and raises if anything slipped through. Every model request
   goes through one function that sanitizes and then runs `assert_clean` on the system and user
   text before the provider is called. If it fires, nothing is sent and nothing is saved.
4. If the project has protected variables but the Keychain cannot be read, the run stops: the
   sanitizer would not know the values, so it does not guess.

## Honest limits

**The sanitizer is a heuristic plus an exact match. It is not a guarantee.**

- It knows your values only if you ran `vaultshield protect`. In a project that was never
  protected, the values in `.env` are not known to it (the file is not read, so they are not
  sent from there, but a secret **pasted into the README or a commit message** is only caught if
  it has one of the shapes above).
- A secret in an **unusual shape** inside README text, a commit subject, a dependency name or a
  file name can slip through: a short password, a passphrase in plain words, a token in a format
  not listed, something split across lines, something encoded (base64, hex).
- Vault values that are **under 4 characters, only digits, or true/false/yes/no/on/off/1/0** are
  not replaced (the same rule as `vaultshield exec`), because replacing them would wreck ordinary
  text. A short secret is therefore not caught by its value.
- It also **over-redacts**. A long identifier that mixes upper case, lower case and digits
  (32+ characters) becomes `***`, and a README line like `password = "..."` is masked.
- **File names are sent**, including those of secret files. A secret inside a file name is
  caught only by shape.
- Framework detection (shared with `vaultshield init`) opens `package.json`, `manage.py`,
  `Procfile` and `docker-compose.yml`. These are not secret files by VaultShield's rules. Only the
  start command and port it finds are used, and they are sanitized, but a `command:` line in a
  compose file that embeds a secret in an unusual shape would reach the model.
- The model's questions are shown to you, and its suggested approach is **written into the
  prompt**. A README with hostile instructions in it can steer them. Read the prompt before you
  hand it to an agent. The Constraints section is fixed text and cannot be changed by the model.
- What you type as an answer goes to the model. It is sanitized, but do not type secrets.
- The prompt file under `.vaultshield/context/` is plain text and contains the project summary.
  Nothing adds it to `.gitignore` for you.
- Your API key is in the Keychain next to the project secrets, and `GEMINI_API_KEY`, if you use
  it, is visible to every program started from that shell. The Keychain is not a boundary against
  another program running as you. The first read may make macOS ask for permission; that has
  not been tested here.

**What has not been checked against the real service.** Every test uses a mocked HTTP
transport and an in-memory Keychain. The request and response format, the model id and the
`store: false` setting come from Google's documentation (read 2026-10-22), not from a live call.
In particular: that the service accepts a structured-output `schema`, that `thinking_level`
`low` is accepted for the model, and what Google does with the request after receiving it, have
not been confirmed. Run `spike/orchestrator_smoke.sh` once with a real key to find out.

**Scope.** Gemini only. macOS only (`--copy` needs `pbcopy`). No agent is started.
