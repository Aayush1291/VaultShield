#!/usr/bin/env bash
# sync_smoke.sh: prove `vaultshield protect sync` reaches YOUR dashboard.
#
# Kiro did NOT run this. It talks to YOUR local backend and uses YOUR
# `vaultshield login` token and YOUR Keychain, so you run it.
#
# What it does, all inside a throw-away /tmp project:
#   1. makes a temp project with a FAKE .env (never a real secret);
#   2. runs `vaultshield protect` (moves the fake value into the Keychain,
#      namespaced by this throw-away project's own id);
#   3. writes one FAKE violation line into its .vaultshield/violations.jsonl;
#   4. runs `vaultshield protect sync`, which creates one Run (agent "protect")
#      on the backend and marks it awaiting review;
#   5. prints the run id and the URL to open it at.
# It cleans up with `vaultshield unprotect` (which forgets the Keychain items)
# and deletes the temp folder. It never runs `agents install` and never touches
# your real agent configs (~/.claude, ~/.kiro, ~/.codex, ~/.gemini).
#
# Needs: a reachable backend (default http://localhost:8000; override with
# VAULTSHIELD_BACKEND_URL), a `vaultshield login` already done, and a backend
# project id in VS_PROJECT (the 24-hex id GET /api/projects/ lists) because a
# Run must be registered under an existing Project.
#
# Python: default cli/.venv/bin/python (override with VS_PYTHON). The login
# token at ~/.vaultshield/token is read whichever interpreter is used.

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${VS_PYTHON:-$REPO/cli/.venv/bin/python}"
BACKEND="${VAULTSHIELD_BACKEND_URL:-http://localhost:8000}"
PREFIX="vs-sync-smoke"

FAKE_ENV_KEY="sk-test-SYNCSMOKE-1111-2222-3333"

say() { printf '%s\n' "$*"; }
vs() { "$PY" -m vaultshield.cli "$@"; }

if [ ! -x "$PY" ] || ! "$PY" -c 'import click, httpx' >/dev/null 2>&1; then
  say "Need a Python with VaultShield's dependencies. Tried: $PY (override with VS_PYTHON)."
  exit 1
fi
if [ -z "${VS_PROJECT:-}" ]; then
  say "Set VS_PROJECT to a backend project id (the ids GET $BACKEND/api/projects/ lists),"
  say "then run again. A Run is always registered under an existing Project."
  exit 1
fi

safe_dir() { case "$1" in /tmp/${PREFIX}.*|/private/tmp/${PREFIX}.*) return 0 ;; *) return 1 ;; esac; }

DIR="$(mktemp -d "/tmp/${PREFIX}.XXXXXX")" || { say "Could not make a temp folder."; exit 1; }
cleanup() {
  # Forget the Keychain items this smoke created, then remove the folder.
  if [ -d "$DIR/.vaultshield" ]; then ( cd "$DIR" && vs unprotect >/dev/null 2>&1 || true ); fi
  if [ "${KEEP:-0}" = "1" ]; then say ""; say "Kept: $DIR"; return; fi
  if safe_dir "$DIR" && [ -d "$DIR" ]; then rm -rf "$DIR"; fi
}
trap cleanup EXIT

cat > "$DIR/.env" <<EOF
FAKE_KEY=$FAKE_ENV_KEY
PORT=3000
EOF

say "== 1. protect the throw-away project =="
( cd "$DIR" && vs protect ) || { say "protect failed"; exit 1; }

say ""
say "== 2. record one FAKE violation =="
mkdir -p "$DIR/.vaultshield"
TS="$("$PY" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
"$PY" - "$DIR/.vaultshield/violations.jsonl" "$TS" <<'PYEOF'
import json, sys
path, ts = sys.argv[1], sys.argv[2]
line = {"event_type": "violation", "timestamp": ts,
        "payload": "antigravity shell denied: protected_path .env"}
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(line) + "\n")
PYEOF

say ""
say "== 3. sync to the dashboard =="
OUT="$DIR/sync.out"
( cd "$DIR" && vs protect sync --project "$VS_PROJECT" --backend-url "$BACKEND" ) | tee "$OUT"
STATUS=${PIPESTATUS[0]}
if [ "$STATUS" -ne 0 ]; then
  say "sync did not complete (see the line above)."
  exit 1
fi

RUN_ID="$(sed -n 's/.*to run \([0-9a-f-]*\) .*/\1/p' "$OUT")"
say ""
if [ -n "$RUN_ID" ]; then
  say "Run id: $RUN_ID"
  say "Open it in the dashboard (run history for project $VS_PROJECT),"
  say "or fetch it directly: $BACKEND/api/runs/$RUN_ID/"
else
  say "(nothing to sync, or no run id in the output above)"
fi
