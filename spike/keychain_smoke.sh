#!/usr/bin/env bash
# keychain_smoke.sh: does the real macOS Keychain work for VaultShield, and does it prompt?
#
# Kiro did NOT run this script. It touches your REAL login Keychain, so you run it, once.
#
# What it does, with ONE fake item (service "vaultshield-smoke", account "smoke-test",
# a made-up value, never a real secret):
#   1. store it       with Python's keyring (the same code path VaultShield uses)
#   2. read it back   with Python's keyring, and compare
#   3. read it again  with the `security` command, which is how another program (an agent
#                     in a terminal) would try to read it
#   4. delete it      with Python's keyring, and check it is gone
# If anything is left behind at the end (even after an error or Ctrl-C), it deletes the item.
#
# macOS has no way to tell a script "a dialog was shown". So the script TIMES each step: a
# step that takes several seconds was almost certainly waiting for you to answer a dialog.
# Your own eyes are the real answer; the final line is the script's inference.
#
# Usage:  spike/keychain_smoke.sh
# Needs:  macOS, and a Python that has `keyring` (default: cli/.venv/bin/python;
#         override with  VS_PYTHON=/path/to/python spike/keychain_smoke.sh)

set -u

SERVICE="vaultshield-smoke"
ACCOUNT="smoke-test"
SLOW_MS=2500          # a step slower than this probably waited on a dialog
SECURITY_TIMEOUT=90   # seconds to wait for the `security` read before giving up

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${VS_PYTHON:-$REPO/cli/.venv/bin/python}"
VALUE="fake-smoke-value-$$-$(date +%s)"

say() { printf '%s\n' "$*"; }

if [ "$(uname)" != "Darwin" ]; then
  say "This script is for macOS only."; exit 1
fi
if ! command -v security >/dev/null 2>&1; then
  say "The macOS 'security' command was not found."; exit 1
fi
if [ ! -x "$PY" ] || ! "$PY" -c 'import keyring' >/dev/null 2>&1; then
  say "Need a Python with 'keyring' installed. Tried: $PY"
  say "Set VS_PYTHON to one that has it, for example:  VS_PYTHON=/path/to/python $0"
  exit 1
fi

item_exists() {  # attributes only: this asks whether the item exists, it never reads the secret
  security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1
}

if item_exists; then
  say "An item named '$SERVICE' (account '$ACCOUNT') already exists in your Keychain."
  say "This script will not touch an existing item. If it is a leftover from an earlier run, remove it with:"
  say "  security delete-generic-password -s $SERVICE -a $ACCOUNT"
  exit 1
fi

say "This will create, read and delete ONE fake item in your REAL Keychain:"
say "  service: $SERVICE    account: $ACCOUNT    value: made up, never printed"
say "If macOS shows a dialog, answer it however you like; the script reports what happened."
if [ -t 0 ]; then
  printf 'Press Enter to continue, Ctrl-C to cancel: '
  read -r _
fi

cleanup() {
  if item_exists; then
    "$PY" -c 'import keyring, sys; keyring.delete_password(sys.argv[1], sys.argv[2])' "$SERVICE" "$ACCOUNT" >/dev/null 2>&1 \
      || security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1
  fi
  if item_exists; then
    say "WARNING: the fake item is still in your Keychain. Remove it with:"
    say "  security delete-generic-password -s $SERVICE -a $ACCOUNT"
  fi
}
trap cleanup EXIT

now_ms() { perl -MTime::HiRes=time -e 'printf "%d\n", time() * 1000'; }

# timed <command...>: runs it, leaving its stdout in OUT, its exit code in RC, its duration in MS.
timed() {
  local start end
  start="$(now_ms)"
  OUT="$("$@" 2>/dev/null)"
  RC=$?
  end="$(now_ms)"
  MS=$((end - start))
}

slow_or_fast() {
  if [ "$1" -ge "$SLOW_MS" ]; then printf 'SLOW (a prompt was probably shown)'; else printf 'fast (no prompt detected)'; fi
}

FAILED=0
SLOW_STEPS=""

record() {  # record <step name> <ok yes|no> <detail>
  local name="$1" ok="$2" detail="$3"
  printf '  %-34s %6d ms  %-34s %s\n' "$name" "$MS" "$(slow_or_fast "$MS")" "$detail"
  if [ "$ok" != "yes" ]; then FAILED=1; fi
  if [ "$MS" -ge "$SLOW_MS" ]; then SLOW_STEPS="$SLOW_STEPS [$name]"; fi
}

say ""
say "Results:"

# 1. store
VS_SMOKE_VALUE="$VALUE" timed "$PY" -c 'import keyring, os, sys; keyring.set_password(sys.argv[1], sys.argv[2], os.environ["VS_SMOKE_VALUE"])' "$SERVICE" "$ACCOUNT"
if [ "$RC" -eq 0 ]; then record "1. store (python keyring)" yes "stored"; else record "1. store (python keyring)" no "FAILED (exit $RC)"; fi

# 2. read back with the same code path, and compare
timed "$PY" -c 'import keyring, sys; print(keyring.get_password(sys.argv[1], sys.argv[2]) or "")' "$SERVICE" "$ACCOUNT"
if [ "$RC" -eq 0 ] && [ "$OUT" = "$VALUE" ]; then
  record "2. read back (python keyring)" yes "matches what was stored"
else
  record "2. read back (python keyring)" no "FAILED (exit $RC, value did not match)"
fi

# 3. read it the way another program would, with a time limit so a dialog cannot hang this script
timed perl -e 'alarm shift; exec @ARGV' "$SECURITY_TIMEOUT" security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w
if [ "$RC" -eq 0 ] && [ "$OUT" = "$VALUE" ]; then
  record "3. read with 'security' (other program)" yes "READ THE VALUE (no block)"
elif [ "$RC" -eq 0 ]; then
  record "3. read with 'security' (other program)" yes "returned something else"
else
  record "3. read with 'security' (other program)" yes "REFUSED or cancelled (exit $RC)"
fi
OUT=""

# 4. delete, and check it is gone
timed "$PY" -c 'import keyring, sys; keyring.delete_password(sys.argv[1], sys.argv[2])' "$SERVICE" "$ACCOUNT"
if [ "$RC" -eq 0 ] && ! item_exists; then record "4. delete (python keyring)" yes "deleted, confirmed gone"; else record "4. delete (python keyring)" no "FAILED (exit $RC)"; fi

say ""
if [ -n "$SLOW_STEPS" ]; then
  say "PROMPT: macOS probably showed a prompt (a step waited several seconds) during:$SLOW_STEPS"
else
  say "PROMPT: macOS showed no prompt (every step finished in under ${SLOW_MS} ms)."
fi
say "        (This is inferred from timing. If you saw a dialog that the script did not report, tell Kiro.)"
if [ "$FAILED" -ne 0 ]; then say "At least one step FAILED: see the lines above."; exit 1; fi
exit 0
