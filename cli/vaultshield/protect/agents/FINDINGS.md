# What the agents' docs say (read 2026-10-22)

Why this file exists: Day 3 adds Claude Code, Kiro and Codex next to Antigravity. Nothing in
the code may rest on a guess about a config key, so every fact below was read from the
agent's own current docs, and each row says where.

- **CONFIRMED**: the cited page states it.
- **UNCONFIRMED**: the page does not state it, or two pages disagree. Nothing is built on it,
  or the code is written so it does not depend on it, and the row says which.
- Nothing here was run against a real agent. Every row is a reading of documentation, not a
  measurement. Antigravity is the only agent with measured results (`spike/RESULT.md`).
- The Codex docs moved: `developers.openai.com/codex/...` redirects to
  `learn.chatgpt.com/docs/...`. The cited URLs are the ones that were served.

## Layers per agent

| Agent | Placeholders | File-read deny rules | Pre-tool hook for shell | Do the deny rules cover the shell? |
|---|---|---|---|---|
| Claude Code | yes (agent independent) | yes, `permissions.deny`, absolute form `Read(//path)` | yes, `PreToolUse` on `Bash` | partly: `cat`, `head`, `tail`, `sed`, `tee` and redirections, not scripts or `grep -r` |
| Kiro (IDE 1.x, CLI V3) | yes | yes, `permissions.yaml` `fs_read` deny, and `.kiroignore`; absolute-path form UNCONFIRMED | yes, `PreToolUse`; user-level files are read by the CLI only, the IDE reads project hooks only; the `execute_bash` payload is UNCONFIRMED | UNCONFIRMED; only with the local command sandbox, which is off by default |
| Codex | yes | yes, beta permission profile, absolute `"/path" = "deny"`; only active when the profile is selected | yes, `PreToolUse` on `Bash`; must be trusted in `/hooks` before it runs | yes: the profile limits what sandboxed commands can read (macOS Seatbelt) |

## Claude Code

| # | Fact | Finding | Status | Source |
|---|---|---|---|---|
| C1 | User settings file | `~/.claude/settings.json`. Strict JSON: a comment or trailing comma is an error. | CONFIRMED | https://code.claude.com/docs/en/settings |
| C2 | Project settings files | `.claude/settings.json` (shared) and `.claude/settings.local.json` (personal). List keys such as `permissions.deny` are merged across files, not replaced. | CONFIRMED | https://code.claude.com/docs/en/settings |
| C3 | Surfaces and their config | The CLI and the VS Code extension share `~/.claude/settings.json`; the desktop app reads the same settings files as the CLI. | CONFIRMED | https://code.claude.com/docs/en/vs-code , https://code.claude.com/docs/en/desktop |
| C4 | Config-directory override | `CLAUDE_CONFIG_DIR` replaces `~/.claude`. Credentials are keyed to that directory (separate macOS Keychain entry), so an isolated directory is signed out unless `ANTHROPIC_API_KEY` is set. | CONFIRMED | https://code.claude.com/docs/en/env-vars , https://code.claude.com/docs/en/authentication |
| C5 | File-read deny rules exist | `permissions.deny` is a list of rule strings; `Read(<pattern>)` blocks reads. A `Read` deny also blocks Edit and Write on that path. | CONFIRMED | https://code.claude.com/docs/en/permissions , https://code.claude.com/docs/en/settings-reference |
| C6 | Path form | gitignore syntax. `//path` is an absolute path; `~/path` is under home; a single leading `/` is relative to the settings source (NOT absolute); `path` and `./path` are relative to the working directory. | CONFIRMED | https://code.claude.com/docs/en/permissions |
| C7 | Escaping gitignore characters in a path | The docs say the rules Claude Code writes itself escape `[`, `]` and `*`. They do not show the escape form for a hand-written rule. The code uses the gitignore backslash escape. | UNCONFIRMED | https://code.claude.com/docs/en/permissions |
| C8 | Order | `deny`, then `ask`, then `allow`; a deny in any file wins; a hook that returns `allow` does not lift a deny rule. | CONFIRMED | https://code.claude.com/docs/en/permissions , https://code.claude.com/docs/en/hooks |
| C9 | Deny rules and the shell | `Read` deny rules apply to file commands Claude Code recognises in Bash (the docs name `cat`, `head`, `tail`, `sed`, `tee`) and to redirection targets. They do not apply to a command that reads files without naming them (`grep -r pattern .`) or to a script that opens files itself. The docs point to the sandbox for OS-level enforcement. | CONFIRMED | https://code.claude.com/docs/en/permissions , https://code.claude.com/docs/en/settings-reference |
| C10 | Which other commands are "recognised" | Only the five above are named. | UNCONFIRMED | https://code.claude.com/docs/en/permissions |
| C11 | Hook location and shape | A `hooks` key in the same settings files: `hooks.PreToolUse` is a list of `{ "matcher", "hooks": [ { "type": "command", "command", "timeout" } ] }`. User-level hooks apply to all projects. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C12 | Shell tool and matcher | Tool name `Bash`. A matcher made only of letters, digits, `_`, `-`, spaces, `,` and `|` is an exact string, so `Bash` matches only the Bash tool. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C13 | Hook stdin | One JSON object: `session_id`, `cwd`, `hook_event_name`, `permission_mode`, `tool_name`, `tool_input` (for Bash: `command`, and optional `description`, `timeout`, `run_in_background`), `tool_use_id`. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C14 | Hook response | Exit 0 with JSON `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`; or exit 2 with the reason on stderr. Exit 2 blocks even when JSON is printed. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C15 | When a hook fails | Any exit code other than 0 or 2, a hook that cannot start, a timeout, and unparseable JSON are non-blocking: the tool call runs. So a plain hook fails OPEN. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C16 | Making a hook fail closed | Handler field `"onFailure": "block"` makes a failure, a crash or a timeout block the call. Requires Claude Code v2.1.295 or later. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C17 | What an older Claude Code does with `onFailure` | Not stated. The settings page says an individual invalid entry is skipped with a warning; whether this field counts as invalid on an old version is not documented. | UNCONFIRMED | https://code.claude.com/docs/en/settings |
| C18 | What the hook does not see | `@file` mentions in a prompt are inserted without a tool call, so no hook runs for them. A `Read` deny rule does cover them. | CONFIRMED | https://code.claude.com/docs/en/hooks |
| C19 | Headless | `claude -p "prompt"`; `claude auth status` exits 0 when signed in; `--allowedTools`, `--permission-mode` exist. | CONFIRMED | https://code.claude.com/docs/en/cli-reference |

## Kiro

| # | Fact | Finding | Status | Source |
|---|---|---|---|---|
| K1 | Permissions file, user level | `~/.kiro/settings/permissions.yaml`: `rules:` list of `{ capability, match: [globs], effect }`. Capabilities include `fs_read`, `fs_write`, `shell`. Effects `deny`, `ask`, `allow`; deny beats ask beats allow, whichever scope it came from. IDE and CLI both read it. | CONFIRMED | https://kiro.dev/docs/permissions/ |
| K2 | Permissions file, workspace level | Stored outside the repo at `~/.kiro/workspace-roots/<hash(workspaceRoot)>/permissions.yaml`. The hash function is not given, so the code cannot write this file. | CONFIRMED / hash UNCONFIRMED | https://kiro.dev/docs/permissions/ |
| K3 | Path patterns for `fs_read` | `*` within a component, `**` across components, `{a,b}`, `[abc]`; a pattern with no wildcard also matches everything under it; `~/temp` form is shown. Examples: `**/.env`, `secrets/**`. | CONFIRMED | https://kiro.dev/docs/permissions/ |
| K4 | Absolute `/path` in a V3 `fs_read` match | The V3 page shows relative globs and `~/` only. The CLI 2.x pages show absolute paths in `deniedPaths`. For V3 not stated; also not stated what a relative pattern is relative to. | UNCONFIRMED | https://kiro.dev/docs/permissions/ , https://kiro.dev/docs/reference/built-in-tools/ |
| K5 | Tools that apply `fs_read` | All built-in read tools (`read_file`, `list_directory`, `file_search`, `grep_search`, `code`). | CONFIRMED | https://kiro.dev/docs/tools/ |
| K6 | Agent cannot edit its own rules | Kiro always refuses agent writes to `~/.kiro/settings/`, `.kiro/settings/`, `~/.kiro/workspace-roots/` and `.kiroignore`. | CONFIRMED | https://kiro.dev/docs/permissions/ |
| K7 | `.kiroignore` | gitignore syntax, relative to the file's folder; project root (or any subfolder) and a global file `~/.kiro/settings/kiroignore`. IDE and CLI V3. Blocks direct reads and filters search results. In the IDE the file name must be in the `kiroAgent.agentIgnoreFiles` setting; its default is not stated. | CONFIRMED / IDE default UNCONFIRMED | https://kiro.dev/docs/kiroignore/ |
| K8 | Do deny rules or `.kiroignore` cover shell commands | `shell` has its own rules. `fs_read` and `fs_write` rules shape what sandboxed commands can see, but the local command sandbox is off by default. `.kiroignore` says "across the agent's tools" without naming the shell. | UNCONFIRMED | https://kiro.dev/docs/sandbox/ , https://kiro.dev/docs/kiroignore/ |
| K9 | Hook files | `{"version": "v1", "hooks": [ { "name", "trigger": "PreToolUse", "matcher", "action": { "type": "command", "command" }, "timeout" } ] }`. Project: `.kiro/hooks/*.json` (IDE and CLI V3). Global `~/.kiro/hooks/*.json`: CLI V3 only. Global and project hooks both run. | CONFIRMED | https://kiro.dev/docs/hooks/ |
| K10 | Shell tool and matcher | Tool id `execute_bash` (`execute_pwsh` on Windows). Matcher: category `shell`, or a regex on the id such as `^(execute_bash\|execute_pwsh)$`. The IDE's `shell` category also includes `control_bash_process`. | CONFIRMED | https://kiro.dev/docs/tools/ , https://kiro.dev/docs/custom-agents/configuration-reference/ |
| K11 | Hook stdin | JSON with `hook_event_name`, `cwd`, `session_id`, `tool_name`, `tool_input` (shown for an MCP tool). The field names inside `tool_input` for `execute_bash` are not documented. The code reads every string inside `tool_input` and so does not depend on the name. | CONFIRMED / `execute_bash` shape UNCONFIRMED | https://kiro.dev/docs/hooks/types/ |
| K12 | Hook response | There is no JSON decision. Exit 0: stdout is added to the agent's context (so an allow must print nothing). For `PreToolUse` a non-zero exit blocks the tool and stderr goes to the agent. | CONFIRMED | https://kiro.dev/docs/hooks/actions/ |
| K13 | Conflict on which exit codes block | The 2.x reference says only exit 2 blocks and other codes are a warning. The current actions page says any non-zero blocks. A deny therefore exits 2 (blocks under both). What a crash or a timeout does is UNCONFIRMED. | UNCONFIRMED | https://kiro.dev/docs/cli/2x-reference/ , https://kiro.dev/docs/hooks/actions/ |
| K14 | Hooks and trust | CLI 2.33 and later asks to trust a folder before loading its hooks; non-interactive runs are not gated. Whether `~/.kiro/hooks` is gated is not stated. | CONFIRMED / user-level UNCONFIRMED | https://kiro.dev/docs/permissions/ |
| K15 | Config-directory override | `KIRO_HOME` (absolute path) replaces the global Kiro directory in V3, including sign-ins. Whether the IDE honours it is not stated. | CONFIRMED (CLI V3) / IDE UNCONFIRMED | https://kiro.dev/docs/reference/settings/ |
| K16 | Headless | `kiro-cli chat --v3 --no-interactive [--trust-tools=...]`; auth with `KIRO_API_KEY` (paid plans). A blocking hook is final under `--trust-all-tools` too. | CONFIRMED | https://kiro.dev/docs/cli/headless/ |

## Codex

| # | Fact | Finding | Status | Source |
|---|---|---|---|---|
| X1 | Config files | User `~/.codex/config.toml`; project `<repo>/.codex/config.toml` (trusted projects only); system `/etc/codex/config.toml`. The CLI and IDE extension share the same layers. | CONFIRMED | https://learn.chatgpt.com/docs/config-file/config-basic |
| X2 | Config-directory override | `CODEX_HOME`. Credentials are cached in `$CODEX_HOME/auth.json` or the OS credential store, so an isolated home is signed out unless `CODEX_API_KEY` is set (`codex exec`). `codex login status` reports the method. | CONFIRMED | http://developers.openai.com/codex/llms-full.txt , https://learn.chatgpt.com/docs/config-file/config-reference |
| X3 | File-read deny rules exist | Beta permission profiles: `[permissions.<name>.filesystem]` with `"/absolute/path" = "deny"`, `"~/path" = "deny"`, or globs under `":workspace_roots"`. `deny` blocks reads and writes under the path. | CONFIRMED (beta, "may change") | https://learn.chatgpt.com/docs/permissions |
| X4 | Enforcement | For commands Codex runs: macOS Seatbelt; Codex refuses to run a command rather than run it unsandboxed if the policy cannot be enforced. Glob matches are listed when a sandboxed process starts (Linux, Windows); exact paths are the safer form. | CONFIRMED | https://learn.chatgpt.com/docs/permissions |
| X5 | When a profile is active | Only when `default_permissions = "<name>"` selects it. Profiles do not combine with the older sandbox settings: if `sandbox_mode` is set in any loaded config file, by `--sandbox`, or by the selected config profile, the old settings are used and the profile is ignored. | CONFIRMED | https://learn.chatgpt.com/docs/permissions |
| X6 | Hook files | `~/.codex/hooks.json` or `<repo>/.codex/hooks.json`, or inline `[[hooks.PreToolUse]]` in `config.toml`. JSON shape as for Claude: `{"hooks": {"PreToolUse": [ { "matcher", "hooks": [ { "type": "command", "command", "timeout" } ] } ] } }`. Hooks from all sources run. | CONFIRMED | https://learn.chatgpt.com/docs/hooks |
| X7 | Shell tool and matcher | `Bash` (also matches unified exec, `exec_command`); the matcher is a regex. `apply_patch`, MCP and other function tools also pass through `PreToolUse`; hosted tools do not. | CONFIRMED | https://learn.chatgpt.com/docs/hooks |
| X8 | Hook stdin | One JSON object: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, `permission_mode`, `turn_id`, `tool_name`, `tool_use_id`, `tool_input` (for Bash: `command`). | CONFIRMED | https://learn.chatgpt.com/docs/hooks |
| X9 | Hook response | Deny with stdout JSON `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`, or the older `{"decision": "block", "reason": "..."}`, or exit 2 with the reason on stderr. Plain text on stdout is ignored. `ask`, `continue: false` and similar are not supported yet: the run is marked failed and the tool call continues. | CONFIRMED | https://learn.chatgpt.com/docs/hooks |
| X10 | Hook trust | A non-managed hook is skipped until the user reviews and trusts it (`/hooks` in the CLI); trust is recorded against a hash of the hook definition, so an edited hook needs review again. `--dangerously-bypass-hook-trust` skips the check for one run. Where the trust is stored is not stated, so `doctor` cannot tell whether a hook is trusted. | CONFIRMED / storage UNCONFIRMED | https://learn.chatgpt.com/docs/hooks |
| X11 | When a `PreToolUse` command hook fails | Unsupported output fields mark the run failed and the tool call continues. For MCP hooks the docs say an error, timeout or malformed answer does not block. For a command hook that crashes or times out on `PreToolUse` it is not stated; treated as fail-open. Default timeout 600 s. | UNCONFIRMED (fail-open assumed) | https://learn.chatgpt.com/docs/hooks |
| X12 | The docs' own caveat | Tool hooks are "a useful guardrail, not a complete enforcement boundary". | CONFIRMED | https://learn.chatgpt.com/docs/hooks |
| X13 | Surfaces that read `~/.codex/hooks.json` | The hooks page gives no per-surface table. The IDE extension shares config layers (X1). | UNCONFIRMED for hooks in the IDE extension and the app | https://learn.chatgpt.com/docs/hooks |
