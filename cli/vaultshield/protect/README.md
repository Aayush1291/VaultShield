# VaultShield Protect

An AI coding agent should see the **names** of your secrets, never their
**values**. Protect does that by taking the values out of the project:

- the real values live in the macOS Keychain;
- your project files hold the placeholder `vs_protected`;
- your app gets the real values only through `vaultshield exec`;
- agent-specific rules (deny rules, a command hook) are extra backstops.

The placeholder is the part that works. The spike (`spike/RESULT.md`) found that
a placeholder file was the only layer that held up on every surface; the deny
rules and the hook have gaps (see [Honest limits](#honest-limits)).

## Usage

```
vaultshield protect --dry-run    list what would be handled; changes nothing
vaultshield protect              move values into the Keychain, write placeholders
vaultshield status               protected files and variable NAMES (never values)
vaultshield exec -- npm start    run a command with the real values in its environment
vaultshield unprotect            put the real values back
vaultshield agents render antigravity   PRINT deny rules and hook config (installs nothing)
vaultshield agents install antigravity [--home PATH]   write them into the CLI and App settings
vaultshield agents uninstall antigravity [--home PATH] put every touched file back exactly
vaultshield agents doctor antigravity   is each backstop active, per CLI and App
vaultshield hook antigravity     the hook Antigravity calls; not for typing by hand
```

Run these from the project folder (subfolders work too; the project is the
nearest folder up with `.vaultshield/protect.json`). Always put `--` before the
command in `exec`.

### What `protect` does

1. Finds `.env` and `.env.*` (not `.env.example` or `.env.sample`), skipping
   `node_modules`, `.git`, `.venv`, `.vaultshield` and symlinks.
2. Reads each file. If any line is not `KEY=value`, a comment or blank (for
   example `export KEY=...`, `KEY: value`, or a multi-line value), the **whole
   file is refused**: nothing in it is changed, the line numbers are reported,
   and the file is recorded as "detected" because it still holds real values.
   Other files are still protected, and the command exits with status 1.
3. Stores every non-empty value in the Keychain and **reads each one back and
   compares it** before touching the file.
4. Writes `.vaultshield/protect.json` (names and paths only).
5. Replaces the file atomically (temp file, then `os.replace`), writing
   `NAME=vs_protected`. Order, comments, blank lines, line endings (LF, CRLF) and
   file permissions are kept. If anything fails, the Keychain and the manifest
   are rolled back and the file is untouched.

Running it again is safe. Files that are already protected are left alone, and
a value you changed (a rotated key) is stored again.

Choices worth knowing:

- **Empty values** (`KEY=`) are left as they are. Nothing is stored.
- **Duplicate keys** are all replaced; `exec` uses the last one.
- A variable that is **partly** a placeholder and partly a real value in the same
  file is refused, because it is unclear which one wins.
- Files that are not `.env` format (`*.pem`, `*.key`, `id_rsa*` but not
  `.pub`, `credentials.json`, `.npmrc`, `.pypirc`, `.netrc`, `.aws/credentials`)
  are **detected and reported, never moved or changed**. They are recorded in the
  manifest so the deny rules and the hook cover them.
- `.env.template` **is** treated as a secret file; only `.example` and `.sample`
  are skipped.

### Where things live

| What | Where |
|---|---|
| Values | Keychain service `vaultshield`, account `<project-id>:<file>:<VAR>`. The item holds the variable's original line(s) as JSON, so quotes, spacing and duplicates come back exactly. |
| Manifest | `.vaultshield/protect.json`: a random project id, protected files with variable names, detected files. No values. |
| Blocked commands | `.vaultshield/violations.jsonl`: one JSON line per block (reason code and file path, never the command and never a value). |

The project id is stored in the manifest, so moving or renaming the folder is
fine. Deleting `.vaultshield/protect.json` loses the id and **orphans the
Keychain items** (remove them in Keychain Access, searching for `vaultshield`).

### `exec`

```
vaultshield exec [--dir .] -- <command> [args...]
```

- Loads the protected files **directly in `--dir`** (default: the current
  folder) and runs the command there. For a monorepo, run it per folder:
  `vaultshield exec --dir backend -- python app.py`.
- Within one folder, files are applied in name order and a later file overrides
  an earlier one (`.env` then `.env.local`). `.env.development` and
  `.env.production` would both load, production last.
- **A variable already set in your environment wins** and is not overridden. That
  is also how `python-dotenv` and Node's `dotenv` behave, so they will not replace
  the real value with `vs_protected` from the file.
- Output is scrubbed: a real value in stdout or stderr is replaced with `***`,
  including a value split across two reads. A value is redacted when it is at
  least 4 characters, not only digits, and not one of
  `true/false/yes/no/on/off/1/0` (any case). So `PORT=3000` and `DEBUG=true`
  stay readable, and `NODE_ENV=production` shows as `***`.
- Returns the command's exit code (128+N if a signal N ended it, 127 if the
  command is not found). SIGTERM and SIGHUP are forwarded to the command; in a
  terminal Ctrl-C reaches it directly.
- Writes nothing to disk. stdin is passed through; stdout and stderr are pipes,
  so the command does not see a terminal (colours and prompts may change).
- A missing Keychain value is named on stderr and the run continues without it.

### Antigravity (`agents render`, `hook`)

`vaultshield agents render antigravity` **only prints**:

1. `read_file(<absolute path>)` deny rules for every protected and detected
   file. They are exact paths because Antigravity's `read_file(.env*)` glob
   does not match anything (measured in the spike).
2. The `PreToolUse` hook config for `run_command`.

#### `agents install` / `uninstall` / `doctor`

What the App test measured, and what these commands are built on: placeholders
were safe; deny rules **alone leaked** (the agent's `read_file` was blocked, then it
ran `cat` in the terminal with `BypassSandbox:true`); and the sandbox rejected our
rules as "non-absolute file path". So the rules are exact **absolute** paths, and the
hook is there to catch the terminal route.

`vaultshield agents install antigravity [--home PATH]` writes, for every file in the
manifest (protected and detected-not-moved):

- `read_file(<absolute path>)` deny rules into the CLI's
  `~/.gemini/antigravity-cli/settings.json` (`permissions.deny`) **and** the App's
  `~/.gemini/config/config.json` (`userSettings.globalPermissionGrants.deny`);
- one `PreToolUse` hook on `run_command` into the global `~/.gemini/config/hooks.json`.

It is merge-only. Files are edited by inserting text, never by re-writing the JSON,
so every other key and every existing grant stays **byte for byte** identical (checked
by tests across many layouts). It is idempotent. Before a file is first changed, a copy
is kept next to it as `<file>.vaultshield.bak` (or `<file>.vaultshield.absent` if the
file did not exist). Quit Antigravity first: a running App can overwrite `config.json`.

- A missing `config.json` is **not invented** (the App owns it) and is skipped. A missing
  CLI `settings.json` or `hooks.json` is created if the folder around it exists.
- The hook command is `<python> -m vaultshield.cli hook antigravity`, with the absolute
  path of the Python that ran `install`. Not the bare name `vaultshield` (hooks may not
  get your shell's PATH), and not the `vaultshield` script (it can name a different
  Python; a hook that cannot start gives Antigravity no decision, and it then denies
  every command in every project). If you move or delete that Python, run `install`
  again; `doctor` notices.
- The rules hold absolute paths of the project at the time you run `install`. Run it again
  after `protect` adds files.

`vaultshield agents uninstall antigravity [--home PATH]` puts each touched file back
exactly from its copy, then removes the copy. It restores the file **as it was at the
first install**. If Antigravity changed the file since (for example it added a grant
when you approved a command), that later version is first saved next to the file as
`<file>.vaultshield.before-uninstall` so nothing is lost for good.

`vaultshield agents doctor antigravity` prints, for the CLI and for the App, whether
the placeholders, the deny rules and the hook are active, and exits 1 if any is not. It
also warns when command grants in `config.json` **look like they contain credentials**,
and says only how many and at which positions (0-based, in
`userSettings.globalPermissionGrants.allow`), never what they contain. That check is a
heuristic: it can miss some and flag some that are not.

The hook reads the tool call on stdin (`toolCall.args.CommandLine`,
`toolCall.args.Cwd`, `workspacePaths`) and answers `{"decision": "allow"}` or
`{"decision": "deny", "reason": ...}`. It denies a command that:

- names a protected or detected file (absolute path, relative path, or just the
  file name; case-insensitive; quoting such as `.e""nv` is removed first);
- dumps the environment: `env`, `printenv`, `export` or `export -p`,
  `declare`/`typeset` with `-x`/`-p` or alone, a bare `set`, `ps` with `e` or `E`;
- reads the Keychain: `security find-*-password`, `security dump-keychain`, or
  Python `keyring` code;
- runs `vaultshield exec` or `vaultshield unprotect` (those are for you, not the
  agent).

It looks inside `sh -c '...'`, `eval`, `sudo`, `nohup`, `timeout` and similar. In a
folder with no manifest it allows everything, so a global hook does not get in
the way of other projects. A payload it cannot read, or a manifest it cannot
read, is denied (fail closed). It never reads the Keychain.

## Honest limits

These were checked by tests unless marked otherwise.

**The output scrubber is not a safety boundary.** It replaces a value only when
the whole value appears as-is.

- A value the command prints **base64-encoded is not scrubbed**. Neither are hex,
  reversed, spaced-out, or character-swapped forms
  (`tests/test_protect_leak.py`, `test_KNOWN_LIMIT_*`). Anyone who sees the
  encoded text can decode it.
- A **partial** value is not scrubbed.
- A command can write the value to a file or send it over the network. `exec`
  hands real values to the program you run; it cannot control what that program
  does with them. Only run commands you trust under `exec`.
- Values under 4 characters, digits-only values and true/false/yes/no/on/off are
  not redacted. Other values are redacted everywhere they appear, so ordinary
  words that happen to match a value become `***`.

**Placeholders cover what Protect moved, and nothing else.**

- A value copied into a comment, into another variable of the same file, into
  another file, into git history, or into your shell history is not touched.
- Files outside the discovery list (for example `env.bak`, `.env~`) are not found.
- `protect` reads `.env` with the same parser as the rest of VaultShield, which
  keeps a trailing `# comment` as part of the value (`KEY=abc # note` is
  `abc # note`). Many dotenv libraries drop the comment, so the value `exec`
  injects can differ from what your app's own loader would have read.
- `vaultshield run` (the Docker pipeline) builds its dummy credentials from
  `.env`. After `protect` it sees placeholders, so its dummies lose their shapes.
  That pipeline was not changed.
- Two `protect` runs at the same time are not coordinated.

**The Keychain is not a hard boundary against an agent running as you.** The
hook denies the obvious ways to read it (`security`, Python `keyring`), but
anything else running as your user can try. macOS may ask you to approve access
for programs that are not on an item's access list; **this was not tested**,
because the tests use an in-memory fake and the development never wrote to the
real Keychain. Treat the real-Keychain behaviour, including any permission
prompts on first use, as unverified until you try it.

**The hook and deny rules are backstops** (from the spike):

- A `PreToolUse` hook only runs for commands that would otherwise run. In
  Antigravity's default headless mode a command that needs approval is refused
  before the hook sees it.
- The hook matches text. It does not catch `cat .e*`, `cat *`, a command built
  from variables, `python -c "print(os.environ)"`, `node -e "console.log(process.env)"`,
  or a script that reads the file for the agent. `ps -ef` is denied even though it
  may not show the environment.
- `read_file` rules are exact paths: a new `.env.something` is not covered until
  you run `protect` and then `agents install` again.
- **Deny rules alone are not enough.** In the App test they blocked `read_file` and the
  agent read the file through the terminal anyway. That is why the placeholders are the
  real protection and the hook is added.
- **Not proven yet: that the App calls the hook.** Hooks fire in the CLI (checked in the
  spike). For the App, the docs say so, but it has not been shown on this machine. Run
  `spike/hook_proof.sh` once to find out. Until then, treat the hook as unproven in the
  App. Whether an absolute deny rule is enforced end to end in the App is also not
  confirmed; the App only showed that it accepts absolute paths and rejects relative ones.
- `install` was tested only against invented files in a temporary home. It has not been
  run against a real `~/.gemini`.
- The `.vaultshield.bak` copy of `config.json` is a full copy, so it holds whatever the
  original holds, including any credentials in your command grants. It has the same file
  permissions as the original. Delete it after `uninstall` if you keep it (uninstall
  removes it).
- If the App rewrites `config.json` while it is running, it can drop the rules. `doctor`
  will show them as missing; run `install` again with the App closed.

**Scope.** macOS and Antigravity only. Claude Code, Kiro and Codex have no
renderer or installer yet. On Linux `keyring` uses whatever backend it finds; that was not
tested.
