#!/usr/bin/env bash
# orchestrator_smoke.sh: one real run of `vaultshield ask` against the live Gemini API.
#
# Kiro did NOT run this script. It makes REAL calls with YOUR key, so you run it, once.
#
# Run it after you have stored your key:
#     <python> -m vaultshield.cli config set-key gemini       (typed at a hidden prompt)
#
# What it does
#   1. checks a key is set (shows only whether, and its last 4 characters)
#   2. makes a throw-away project in /tmp with INVENTED secrets: a fake .env, and a fake token
#      "accidentally" pasted into the README
#   3. asks you to confirm, then runs `vaultshield ask` on it. That is TWO real API calls:
#      one for the questions, one for the suggested approach
#   4. prints the questions the model asked, and checks:
#        - the questions really came from the model (not the generic fallback)
#        - the suggested approach was produced
#        - none of the invented secrets is in the prompt
#   5. prints what was recorded in the call log, which shows how the live API answered
#
# What it does NOT do: it never runs `vaultshield protect`, so it does not write to your
# Keychain; it never reads your real projects; it writes only inside its own /tmp folder, which
# it removes at the end (KEEP=1 keeps it). The macOS Keychain may ask once to let Python read
# your Gemini key; that is expected.
#
# If a check fails, the message after it says what to look at, and the script then says how
# each model call ended: "api rejected" (an HTTP error; a 400 on the first call usually means
# the live API did not accept something in the request format), "reply not understood" (the
# service answered but the tool could not use the reply) or "timeout". No content is printed.
#
# Needs: a Python with VaultShield's dependencies (default: cli/.venv/bin/python;
#        override with  VS_PYTHON=/path/to/python spike/orchestrator_smoke.sh)

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${VS_PYTHON:-$REPO/cli/.venv/bin/python}"
PREFIX="vs-orch-smoke"

# the invented secrets, in one place so the checks below use the same strings
FAKE_ENV_KEY="sk-test-AAAA1111BBBB2222"
FAKE_ENV_DB="postgres://u:p@localhost/db"
FAKE_ENV_PLAIN="plainvalue-without-any-known-shape"
FAKE_README_TOKEN="ghp_fakeFAKEfake0123456789abcdefABCDEF"

say() { printf '%s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=1; }
FAILED=0

vs() { "$PY" -m vaultshield.cli "$@"; }

if [ ! -x "$PY" ] || ! "$PY" -c 'import keyring, httpx, click, yaml' >/dev/null 2>&1; then
  say "Need a Python that has VaultShield's dependencies. Tried: $PY"
  say "Set VS_PYTHON to one that has them, for example:  VS_PYTHON=/path/to/python $0"
  exit 1
fi

# --- classify begins ---
# Reads the call log ($1) and prints one line per failed call, saying how it failed, or one line
# saying nothing failed. It prints the outcome class only: never the model's text, the request
# or anything else found in the log.
classify_ledger() {
  "$PY" - "$1" <<'PYEOF'
import json
import re
import sys

LABELS = {
    "invalid_reply": "reply not understood",
    "timeout": "timeout",
    "network_error": "could not reach the service",
}


def label(outcome):
    if outcome in LABELS:
        return LABELS[outcome]
    if re.fullmatch(r"http_[0-9]{3}", outcome):
        return "api rejected (HTTP " + outcome[5:] + ")"
    return "unknown outcome"


try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        lines = handle.read().splitlines()
except OSError:
    lines = []
outcomes = []
for line in lines:
    try:
        record = json.loads(line)
    except ValueError:
        continue
    outcome = record.get("outcome") if isinstance(record, dict) else None
    if isinstance(outcome, str):
        outcomes.append(outcome)
failed = [(number, outcome) for number, outcome in enumerate(outcomes, 1) if outcome != "ok"]
if not outcomes:
    print("no calls were recorded")
elif not failed:
    print("all %d recorded calls ended ok" % len(outcomes))
for number, outcome in failed:
    print("call %d of %d: %s" % (number, len(outcomes), label(outcome)))
PYEOF
}
# --- classify ends ---

say "== 1. key =="
if ! vs config show-key gemini; then
  say ""
  say "No key yet. Store it first (typed at a hidden prompt, never on the command line):"
  say "    $PY -m vaultshield.cli config set-key gemini"
  exit 1
fi

# --- fixture begins ---
make_fixture() {  # $1 = folder
  mkdir -p "$1/src"
  cat > "$1/.env" <<EOF
FAKE_KEY=$FAKE_ENV_KEY
DB_URL=$FAKE_ENV_DB
SHAPELESS=$FAKE_ENV_PLAIN
PORT=3000
EOF
  cat > "$1/README.md" <<EOF
# Demo shop

A small Express web app that sells mugs.

Setup (pasted here by accident, as people do):

    export GITHUB_TOKEN=$FAKE_README_TOKEN
    npm install && npm start
EOF
  cat > "$1/package.json" <<'EOF'
{
  "name": "demo-shop",
  "version": "1.0.0",
  "scripts": { "start": "node src/app.js" },
  "dependencies": { "express": "^4.18.0", "pg": "^8.11.0" }
}
EOF
  printf "const express = require('express');\nconst app = express();\napp.get('/', (req, res) => res.send('mugs'));\napp.listen(3000);\n" > "$1/src/app.js"
}
# --- fixture ends ---

safe_dir() {  # only ever delete a folder this script made
  case "$1" in
    /tmp/${PREFIX}.*|/private/tmp/${PREFIX}.*) return 0 ;;
    *) return 1 ;;
  esac
}

DIR="$(mktemp -d "/tmp/${PREFIX}.XXXXXX")" || { say "Could not make a temp folder."; exit 1; }
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then say ""; say "Kept: $DIR"; return; fi
  if safe_dir "$DIR" && [ -d "$DIR" ]; then rm -rf "$DIR"; fi
}
trap cleanup EXIT

make_fixture "$DIR"

say ""
say "== 2. project =="
say "  throw-away folder: $DIR"
say "  .env holds INVENTED values; the README has an invented token pasted in it."
say ""
say "== 3. confirm =="
say "  This makes 2 REAL calls to the Gemini API with your key (quota or billing may apply)."
say "  It sends this throw-away project's file names, README text and package names."
say "  It does NOT send the .env, and it never runs 'protect', so your Keychain is not written."
if [ -t 0 ]; then
  printf '  Press Enter to continue, Ctrl-C to cancel: '
  read -r _
fi

say ""
say "== 4. the questions the model asked =="
# Six blank lines answer up to five questions with "skip"; the prompts are printed to stderr.
( cd "$DIR" && printf '\n\n\n\n\n\n' | vs ask "Add a login page" > "$DIR/prompt.txt" 2> "$DIR/questions.txt" )
STATUS=$?
sed 's/^/  /' "$DIR/questions.txt"
say "  (exit status of 'vaultshield ask': $STATUS)"

say ""
say "== 5. checks =="
if [ "$STATUS" -ne 0 ]; then
  fail "vaultshield ask did not finish. Read the lines above."
fi
if grep -q "the model was unavailable" "$DIR/questions.txt"; then
  fail "the questions are the generic fallback, so the model call failed. How it failed is listed below the checks."
else
  grep -q '\[1/' "$DIR/questions.txt" && pass "the questions came from the model" || fail "no questions were shown"
fi
if grep -q '^## Suggested approach' "$DIR/prompt.txt" 2>/dev/null; then
  pass "a suggested approach was produced (second call)"
else
  fail "no suggested approach. How each call ended is listed below the checks."
fi
LEAK=0
for secret in "$FAKE_ENV_KEY" "$FAKE_ENV_DB" "$FAKE_ENV_PLAIN" "$FAKE_README_TOKEN"; do
  if grep -qF -- "$secret" "$DIR/prompt.txt" "$DIR/questions.txt" 2>/dev/null; then LEAK=1; fi
done
[ "$LEAK" = 0 ] && pass "none of the invented secrets is in the prompt or the questions" || fail "an invented secret reached the prompt. Tell Kiro."
grep -q 'demo-shop\|Express' "$DIR/prompt.txt" 2>/dev/null && pass "the project context is in the prompt (README, package names)" || fail "the project context is missing from the prompt"

if [ "$FAILED" != 0 ]; then
  say ""
  say "  How each model call ended (from the call log; no content is shown):"
  classify_ledger "$DIR/.vaultshield/orchestrator.jsonl" | sed 's/^/    /'
  say "    api rejected = the service answered with an error status (400 request, 403 key, 429 quota)"
  say "    reply not understood = the service answered, but not with a JSON object the tool could use"
  say "    timeout = no answer within 30 seconds"
  say "    could not reach the service = no connection was made (network, DNS, proxy)"
fi

say ""
say "== 6. the call log (.vaultshield/orchestrator.jsonl; no content, only how each call ended) =="
if [ -f "$DIR/.vaultshield/orchestrator.jsonl" ]; then
  sed 's/^/  /' "$DIR/.vaultshield/orchestrator.jsonl"
  say "  outcome 'ok' = the live API accepted the request and answered with usable JSON."
  say "  outcome 'http_400' = it rejected something in the request; 'http_403' = the key; 'http_429' = quota."
else
  say "  (no calls were recorded)"
fi

say ""
say "== the prompt that was built (first 25 lines) =="
head -25 "$DIR/prompt.txt" 2>/dev/null | sed 's/^/  /'

say ""
if [ "$FAILED" = 0 ]; then say "RESULT: all checks passed."; else say "RESULT: at least one check FAILED. Send Kiro the output above."; fi
exit "$FAILED"
