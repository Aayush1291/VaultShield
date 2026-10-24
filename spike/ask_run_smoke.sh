#!/usr/bin/env bash
# ask_run_smoke.sh: the one-flow smoke — `vaultshield run --ask` against a
# THROWAWAY repo and your LOCAL backend, then prints the run id.
#
# Kiro did NOT run this. `vaultshield run` needs a running Docker daemon, a
# reachable backend, and a prior `vaultshield login`; `--ask` additionally
# makes REAL model calls, so it needs a Gemini key (`vaultshield config
# set-key gemini`). You run it.
#
# What it does: clones $VS_SCRATCH_REPO into a temp checkout, drops a minimal
# .vaultshield.yml in, then runs the mock agent with an orchestrator-built
# prompt (`run --ask`). The orchestrator scans the checkout, asks you a few
# clarifying questions (answer them in the terminal), builds the enriched
# prompt, and the run proceeds. At the end it prints the run id.
#
# $VS_SCRATCH_REPO MUST be a repo you can throw away: the run clones it into a
# sandbox. The run itself is sandboxed and local; this script opens no pull
# request and pushes nothing.
#
# Env:
#   VS_SCRATCH_REPO         (required) clone URL of the throwaway repo
#   VAULTSHIELD_BACKEND_URL (optional) your local backend; the CLI reads this
#                           itself and defaults to http://localhost:8000
#   VS_VAULTSHIELD          (optional) path to the vaultshield launcher
#
# Usage: ask_run_smoke.sh "<the task for --ask>"   (a default is used if omitted)

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VS="${VS_VAULTSHIELD:-$REPO/cli/.venv/bin/vaultshield}"
TASK="${1:-Add a small health-check endpoint and a test for it.}"

say() { printf '%s\n' "$*"; }

[ -n "${VS_SCRATCH_REPO:-}" ] || {
    say "Set VS_SCRATCH_REPO to the clone URL of a THROWAWAY repo."
    exit 2
}
[ -x "$VS" ] || { say "No vaultshield launcher at $VS (override with VS_VAULTSHIELD)."; exit 1; }

say "NOTE ======================================================================="
say "This runs the mock agent against a sandboxed clone of:"
say "    $VS_SCRATCH_REPO"
say "backed by ${VAULTSHIELD_BACKEND_URL:-http://localhost:8000}. --ask makes REAL"
say "Gemini calls and will ask you a few clarifying questions in the terminal."
say "Nothing is pushed and no pull request is opened."
say "==========================================================================="
say ""

WORK="$(mktemp -d "/tmp/vs-ask-run-smoke.XXXXXX")" || { say "Could not make a temp dir."; exit 1; }
say "== clone the scratch repo into $WORK/checkout =="
git clone "$VS_SCRATCH_REPO" "$WORK/checkout" || { say "Clone failed."; exit 1; }

# A fresh clone of an arbitrary repo has no .vaultshield.yml, so `run` would
# stop at config load. Drop a minimal one in (a trivial static server as the
# app; no routes, so browser capture is skipped) unless the repo ships its own.
if [ ! -f "$WORK/checkout/.vaultshield.yml" ]; then
    cat > "$WORK/checkout/.vaultshield.yml" <<'EOF'
agent: mock
start_command: python3 -m http.server 8000
port: 8000
ready_path: /
EOF
fi

say ""
say "== run --ask (orchestrator builds the prompt, then the sandboxed run) =="
RUN_OUT="$WORK/run.out"
( cd "$WORK/checkout" && "$VS" run --ask "$TASK" --agent=mock --repo-url="$VS_SCRATCH_REPO" ) 2>&1 | tee "$RUN_OUT"
RUN_RC="${PIPESTATUS[0]}"
[ "$RUN_RC" -eq 0 ] || { say ""; say "RESULT: FAIL — \`vaultshield run --ask\` exited $RUN_RC (see above)."; exit "$RUN_RC"; }

# `run`'s first line is "Run <run_id>: artifacts in <dir>".
RUN_ID="$(sed -n 's/^Run \([^:]*\): artifacts in .*/\1/p' "$RUN_OUT" | head -n 1)"
[ -n "$RUN_ID" ] || { say "Could not read the run id from the run output."; exit 1; }

say ""
say "RESULT: OK"
say "run id: $RUN_ID"
say "enriched prompt: $WORK/checkout/.vaultshield/runs/$RUN_ID/prompt.md"
say "Temp checkout left at $WORK — delete it with: rm -rf $WORK"
