#!/bin/sh
# install_smoke.sh: a real end-to-end install of VaultShield into a TEMP prefix.
#
# Kiro did NOT run this. It does a real `pip install` (dependencies come from
# PyPI, so it needs the network and a minute or two), so you run it once.
#
# It never touches your real install: VAULTSHIELD_HOME and VAULTSHIELD_BIN point
# at a throw-away /tmp prefix, so your ~/.vaultshield/venv and ~/.local/bin are
# left alone. It builds the wheel, serves the dashboard's public dir on :8080,
# runs install.sh the way a user would, checks `vaultshield --version`, then
# uninstalls and cleans up.
# It does not run `secure` or `protect`, so it never writes to your Keychain or
# any agent config.

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${VS_PYTHON:-$REPO/cli/.venv/bin/python}"
PREFIX="vs-install-smoke"
PORT=8080

say() { printf '%s\n' "$*"; }

[ -x "$PY" ] || { say "No cli/.venv at $PY (override with VS_PYTHON)."; exit 1; }

say "== build the wheel =="
"$REPO/website/build.sh"

safe_dir() { case "$1" in /tmp/${PREFIX}.*|/private/tmp/${PREFIX}.*) return 0 ;; *) return 1 ;; esac; }
DIR="$(mktemp -d "/tmp/${PREFIX}.XXXXXX")" || { say "Could not make a temp folder."; exit 1; }

SERVER_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" >/dev/null 2>&1 || true
    if safe_dir "$DIR" && [ -d "$DIR" ]; then rm -rf "$DIR"; fi
}
trap cleanup EXIT

say ""
say "== serve the dashboard public dir (install.sh + dist/) on :$PORT =="
( cd "$REPO/frontend/public" && "$PY" -m http.server "$PORT" >/dev/null 2>&1 ) &
SERVER_PID=$!
sleep 2

VHOME="$DIR/venv"
VBIN="$DIR/bin/vaultshield"

say ""
say "== install into the temp prefix $DIR =="
VAULTSHIELD_HOME="$VHOME" VAULTSHIELD_BIN="$VBIN" VAULTSHIELD_BASE="http://localhost:$PORT" \
    sh "$REPO/frontend/public/install.sh"

say ""
say "== check the launcher works =="
"$VBIN" --version

say ""
say "== uninstall (temp prefix only) =="
VAULTSHIELD_HOME="$VHOME" VAULTSHIELD_BIN="$VBIN" sh "$REPO/frontend/public/install.sh" --uninstall

if [ -e "$VHOME" ] || [ -e "$VBIN" ]; then
    say "RESULT: FAIL — the temp venv or launcher was not removed."
    exit 1
fi
say ""
say "RESULT: OK — installed into a temp prefix, ran, and uninstalled cleanly."
say "Your real ~/.vaultshield/venv and ~/.local/bin were never touched."
