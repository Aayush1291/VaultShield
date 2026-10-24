#!/usr/bin/env bash
# PreToolUse(run_command) gate: deny any command mentioning .env / printenv / env.
python3 - "/Users/aayush/Codes/VaultShield/spike/results/hook_invocations.log" <<'PY'
import json, sys, re
log = sys.argv[1]
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    print(json.dumps({"decision": "deny", "reason": "vaultshield: unparsable tool call"}))
    sys.exit(0)
cmd = (((data.get("toolCall") or {}).get("args") or {}).get("CommandLine")) or ""
open(log, "a").write("HOOK run_command: " + cmd.replace(chr(10), " ")[:200] + "\n")
if re.search(r'(^|[^A-Za-z0-9_])(printenv|env)([^A-Za-z0-9_]|$)', cmd) or '.env' in cmd:
    print(json.dumps({"decision": "deny", "reason": "vaultshield spike: command references env/.env"}))
else:
    print(json.dumps({"decision": "allow"}))
PY
