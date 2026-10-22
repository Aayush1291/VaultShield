#!/usr/bin/env bash
# hook_proof.sh: prove whether the Antigravity App fires a PreToolUse hook.
#
# Kiro did NOT run this script. You run it once, in your own terminal.
#
#   spike/hook_proof.sh             STAGE 1. Touches nothing in your real ~/.gemini.
#   spike/hook_proof.sh --global    STAGE 2 (optional). Edits ~/.gemini/config/hooks.json,
#                                   after showing you the diff and asking you to type YES.
#   spike/hook_proof.sh --log       print the log (the one-line way to read it)
#   spike/hook_proof.sh --cleanup   remove the proof project, and undo STAGE 2 if you ran it
#
# What it proves
#   STAGE 1 puts a logging hook in a throw-away project folder (.agents/hooks.json, the
#   "workspace level" the docs describe). If the App fires it, the log gets a line for
#   every tool call. That answers: does the App call hooks at all, before or after you
#   approve a command, and does a "deny" from a hook actually block it?
#   STAGE 2 repeats it with the GLOBAL file, ~/.gemini/config/hooks.json, which is where
#   `vaultshield agents install antigravity` puts the real hook.
#
# What the hook writes (it never writes command text): the time, the tool name, the value of
# BypassSandbox if the call has one, and the NAMES of the fields in the payload. It denies a
# call only if the payload contains the marker VS_HOOK_PROOF_DENY_ME.

set -u

PREFIX="vs-hook-proof"
STATE="${TMPDIR:-/tmp}/${PREFIX}.state"
GLOBAL_HOOKS="$HOME/.gemini/config/hooks.json"
GLOBAL_BACKUP="${GLOBAL_HOOKS}.vaultshield-proof.bak"
GLOBAL_ABSENT="${GLOBAL_HOOKS}.vaultshield-proof.absent"
HOOK_KEY="vaultshield-hook-proof"
MARKER="VS_HOOK_PROOF_DENY_ME"

say() { printf '%s\n' "$*"; }

# --- the hook itself --------------------------------------------------------------------
# Written to a file by write_hook. Kept between these two markers so it can be checked on its own.
# --- hook body begins ---
hook_body() {
  cat <<'HOOK'
#!/bin/bash
LOG="__LOG__"
payload="$(cat)"
tool="$(printf '%s' "$payload" | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/^[^:]*:[[:space:]]*//')"
bypass="$(printf '%s' "$payload" | grep -o '"BypassSandbox"[[:space:]]*:[[:space:]]*[^,}]*' | head -1 | sed 's/^[^:]*:[[:space:]]*//')"
keys="$(printf '%s' "$payload" | grep -o '"[A-Za-z_][A-Za-z0-9_]*"[[:space:]]*:' | tr -d '": ' | sort -u | tr '\n' ',')"
marker=no
if printf '%s' "$payload" | grep -q 'VS_HOOK_PROOF_DENY_ME'; then marker=yes; fi
printf '%s tool=%s bypass=%s marker=%s keys=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${tool:-unknown}" "${bypass:-none}" "$marker" "$keys" >> "$LOG"
if [ "$marker" = yes ]; then
  echo '{"decision":"deny","reason":"VaultShield hook proof: the deny marker was seen, so this hook was called and can block."}'
else
  echo '{"decision":"allow"}'
fi
HOOK
}
# --- hook body ends ---

hooks_json() {  # $1 = path of the hook script
  cat <<JSON
{
  "$HOOK_KEY": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          { "type": "command", "command": "$1", "timeout": 10 }
        ]
      }
    ]
  }
}
JSON
}

write_hook() {  # $1 = hook script path, $2 = log path
  hook_body | sed "s|__LOG__|$2|" > "$1"
  chmod 755 "$1"
}

proof_dir() { [ -f "$STATE" ] && cat "$STATE"; }

safe_proof_dir() {  # only ever delete a folder this script made
  case "$1" in
    /tmp/${PREFIX}.*|/private/tmp/${PREFIX}.*|"${TMPDIR:-/nonexistent}"/${PREFIX}.*) return 0 ;;
    *) return 1 ;;
  esac
}

# --- --log ------------------------------------------------------------------------------
if [ "${1:-}" = "--log" ]; then
  dir="$(proof_dir)"
  if [ -z "$dir" ] || [ ! -f "$dir/hook.log" ]; then say "No log yet. Run STAGE 1 first, then use the App."; exit 1; fi
  cat "$dir/hook.log"
  exit 0
fi

# --- --cleanup --------------------------------------------------------------------------
if [ "${1:-}" = "--cleanup" ]; then
  if [ -f "$GLOBAL_BACKUP" ]; then
    cp -p "$GLOBAL_BACKUP" "$GLOBAL_HOOKS" && rm -f "$GLOBAL_BACKUP" && say "Restored $GLOBAL_HOOKS from its backup."
  elif [ -f "$GLOBAL_ABSENT" ]; then
    rm -f "$GLOBAL_HOOKS" "$GLOBAL_ABSENT" && say "Removed $GLOBAL_HOOKS (it did not exist before STAGE 2)."
  else
    say "STAGE 2 was not applied (or already cleaned up): $GLOBAL_HOOKS left alone."
  fi
  dir="$(proof_dir)"
  if [ -n "$dir" ] && safe_proof_dir "$dir" && [ -d "$dir" ]; then
    rm -rf "$dir" && rm -f "$STATE" && say "Removed the proof project $dir."
  else
    say "No proof project to remove."
  fi
  say "Also remove the proof project from the App's project list if you added it (its own Settings > Projects)."
  exit 0
fi

# --- STAGE 1 ----------------------------------------------------------------------------
if [ "${1:-}" != "--global" ]; then
  dir="$(mktemp -d "/tmp/${PREFIX}.XXXXXX")" || { say "Could not make a temp folder."; exit 1; }
  mkdir -p "$dir/.agents"
  write_hook "$dir/hook.sh" "$dir/hook.log"
  : > "$dir/hook.log"
  hooks_json "$dir/hook.sh" > "$dir/.agents/hooks.json"
  printf '%s' "$dir" > "$STATE"
  printf '# hook proof\nThrow-away project for spike/hook_proof.sh\n' > "$dir/README.md"

  say "STAGE 1 is ready. Nothing in your real ~/.gemini was touched."
  say ""
  say "  Project folder: $dir"
  say "  Log file:       $dir/hook.log"
  say ""
  say "Do this in the Antigravity App (manual):"
  say "  1. New Project > Add Folder, and pick the project folder above."
  say "     (Tip: in the folder dialog press Cmd+Shift+G and paste the path.)"
  say "     The App will record the project in its own files; that is the App, not this script."
  say "  2. Settings (Cmd+,) > Customizations > Hooks: look for '$HOOK_KEY' and make sure it is ON."
  say "     If it is NOT listed, the App did not read the workspace hook file. That is a result:"
  say "     tell Kiro, and run STAGE 2 below."
  say "  3. In that project, ask the agent:   Run  echo hello-proof  in the terminal."
  say "     If the App asks you to approve it, DO NOT click yet. Run the log command below first."
  say "  4. Then ask the agent:               Run  echo $MARKER  in the terminal."
  say ""
  say "Read the log (one line):   cat \"$dir/hook.log\""
  say "  or:                      spike/hook_proof.sh --log"
  say ""
  say "How to read it:"
  say "  A line with tool=run_command after step 3  -> the App FIRES hooks."
  say "  That line already there BEFORE you approved -> the hook sees a command even if you would reject it."
  say "  No line at all                              -> the App did not call the hook (or it is switched off)."
  say "  marker=yes after step 4, and the agent says the command was blocked -> a hook 'deny' is ENFORCED."
  say "  marker=yes but the command ran anyway                               -> a hook 'deny' is NOT enforced."
  say "  bypass=true                                 -> the payload carries BypassSandbox:true for that call."
  say "  keys=...                                    -> the field names of the payload (no values)."
  say ""
  say "When you are done:  spike/hook_proof.sh --cleanup"
  exit 0
fi

# --- STAGE 2 (--global) -----------------------------------------------------------------
dir="$(proof_dir)"
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  say "Run STAGE 1 first (spike/hook_proof.sh) so there is a log folder."
  exit 1
fi
if [ ! -d "$HOME/.gemini/config" ]; then
  say "$HOME/.gemini/config does not exist, so the App is not set up here. Nothing to do."
  exit 1
fi
if [ -f "$GLOBAL_BACKUP" ] || [ -f "$GLOBAL_ABSENT" ]; then
  say "STAGE 2 is already applied. Run spike/hook_proof.sh --cleanup first."
  exit 1
fi
if [ ! -t 0 ]; then
  say "STAGE 2 needs you to confirm in a terminal. Run it directly, not through a pipe."
  exit 1
fi

write_hook "$dir/hook.sh" "$dir/hook.log"
proposed="$(mktemp "$dir/proposed.XXXXXX")"

if [ -f "$GLOBAL_HOOKS" ]; then
  py="$(command -v python3 || true)"
  if [ -z "$py" ]; then say "python3 is needed to merge into the existing hooks.json."; rm -f "$proposed"; exit 1; fi
  hooks_json "$dir/hook.sh" > "$dir/entry.json"
  "$py" - "$GLOBAL_HOOKS" "$dir/entry.json" "$proposed" <<'PY' || { say "The existing hooks.json is not valid JSON; leaving it alone."; rm -f "$proposed"; exit 1; }
import json, sys
current = json.load(open(sys.argv[1]))
entry = json.load(open(sys.argv[2]))
current.update(entry)
with open(sys.argv[3], "w") as out:
    json.dump(current, out, indent=2)
    out.write("\n")
PY
  say "STAGE 2 would change your REAL file: $GLOBAL_HOOKS"
  say "(The diff may also show re-indentation, because this merge re-writes the file.)"
  say ""
  diff -u "$GLOBAL_HOOKS" "$proposed"
else
  hooks_json "$dir/hook.sh" > "$proposed"
  say "STAGE 2 would CREATE your REAL file: $GLOBAL_HOOKS (it does not exist now)"
  say ""
  diff -u /dev/null "$proposed"
fi

say ""
say "This installs a hook that logs, and allows everything except '$MARKER', in EVERY project"
say "until you run:  spike/hook_proof.sh --cleanup"
say "A backup is made next to the file first: ${GLOBAL_HOOKS}.vaultshield-proof.bak"
say ""
printf 'Type YES (capitals) to apply, anything else cancels: '
read -r answer
if [ "$answer" != "YES" ]; then
  say "Cancelled. Nothing was changed."
  rm -f "$proposed"
  exit 0
fi

if [ -f "$GLOBAL_HOOKS" ]; then
  cp -p "$GLOBAL_HOOKS" "$GLOBAL_BACKUP" || { say "Could not make the backup. Nothing was changed."; rm -f "$proposed"; exit 1; }
else
  : > "$GLOBAL_ABSENT"
fi
cp "$proposed" "$GLOBAL_HOOKS" && rm -f "$proposed"
say "Applied. Quit and reopen the App, then repeat steps 3 and 4 in ANY project."
say "Read the log:  cat \"$dir/hook.log\"      Undo:  spike/hook_proof.sh --cleanup"
