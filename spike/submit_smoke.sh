#!/usr/bin/env bash
# submit_smoke.sh: a real `vaultshield run` against a THROWAWAY repo, then the
# exact approve + submit commands for you to run by hand.
#
# Kiro did NOT run this. `vaultshield run` needs a running Docker daemon, a
# reachable backend, and a prior `vaultshield login`; the submit step this
# script PRINTS pushes a branch and opens a REAL pull request. You drive those.
#
# What it does: clones $VS_SCRATCH_REPO into a temp checkout, runs the mock
# agent there, confirms step 8 wrote branch.bundle, then prints the commands to
# approve the Run in the dashboard and to submit it. It stops before submit —
# it never pushes and never opens a pull request.
#
# $VS_SCRATCH_REPO MUST be a repo you can throw away: submitting pushes a
# `vaultshield/run-<id>` branch to it and opens a pull request on it.
#
# Env:
#   VS_SCRATCH_REPO         (required) clone URL of the throwaway repo
#   VAULTSHIELD_BACKEND_URL (optional) backend the CLI talks to (the CLI reads
#                           this itself; defaults to the CLI's own default)
#   VS_PROJECT              (optional) project id, if the scratch repo's
#                           .vaultshield.yml does not set one
#   VS_PROMPT               (optional) the agent prompt
#   VS_VAULTSHIELD          (optional) path to the vaultshield launcher

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VS="${VS_VAULTSHIELD:-$REPO/cli/.venv/bin/vaultshield}"
PROMPT="${VS_PROMPT:-Make one tiny, harmless change: append a line to README.md (create it if it is missing).}"

say() { printf '%s\n' "$*"; }

[ -n "${VS_SCRATCH_REPO:-}" ] || {
    say "Set VS_SCRATCH_REPO to the clone URL of a THROWAWAY repo."
    say "The submit step pushes a branch and opens a REAL pull request on it."
    exit 2
}
[ -x "$VS" ] || { say "No vaultshield launcher at $VS (override with VS_VAULTSHIELD)."; exit 1; }

say "WARNING ===================================================================="
say "This runs the mock agent against:"
say "    $VS_SCRATCH_REPO"
say "The run itself is sandboxed and local. The SUBMIT command this script PRINTS"
say "(but does NOT run) will push a vaultshield/run-<id> branch and open a REAL"
say "pull request on that repo. Only continue with a repo you can throw away."
say "==========================================================================="
say ""

# One temp checkout of the scratch repo, shared by the run and the later submit.
# `submit` must run from a clone of the SAME repo (the repo_url-must-match rule)
# and must find the Run_Directory the run wrote, so both use this checkout. It
# is left in place on purpose — you need it for the submit step below.
WORK="$(mktemp -d "/tmp/vs-submit-smoke.XXXXXX")" || { say "Could not make a temp dir."; exit 1; }
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

run_args=(run --agent=mock --prompt="$PROMPT" --repo-url="$VS_SCRATCH_REPO")
[ -n "${VS_PROJECT:-}" ] && run_args+=(--project "$VS_PROJECT")

say ""
say "== run the mock agent (sandboxed; writes the run branch bundle) =="
RUN_OUT="$WORK/run.out"
( cd "$WORK/checkout" && "$VS" "${run_args[@]}" ) 2>&1 | tee "$RUN_OUT"
RUN_RC="${PIPESTATUS[0]}"
[ "$RUN_RC" -eq 0 ] || { say ""; say "RESULT: FAIL — \`vaultshield run\` exited $RUN_RC (see above)."; exit "$RUN_RC"; }

# `run`'s first line is "Run <run_id>: artifacts in <dir>".
RUN_ID="$(sed -n 's/^Run \([^:]*\): artifacts in .*/\1/p' "$RUN_OUT" | head -n 1)"
[ -n "$RUN_ID" ] || { say "Could not read the run id from the run output."; exit 1; }

BUNDLE="$WORK/checkout/.vaultshield/runs/$RUN_ID/branch.bundle"
if [ ! -f "$BUNDLE" ]; then
    say ""
    say "NOTE: run $RUN_ID wrote no branch.bundle (the agent made no commit beyond"
    say "the base), so there is nothing to submit."
    say "Temp checkout left at $WORK — delete it with: rm -rf $WORK"
    exit 0
fi

say ""
say "RESULT: OK — run $RUN_ID wrote a branch bundle at:"
say "    $BUNDLE"
say ""
say "NEXT STEPS (run these yourself) ==========================================="
say "1. Review and APPROVE the Run in the VaultShield dashboard:"
say "       approve run $RUN_ID at ${VAULTSHIELD_BACKEND_URL:-your VaultShield backend}"
say ""
say "2. Then SUBMIT it. This PUSHES a vaultshield/run-<id> branch and opens a"
say "   REAL pull request on $VS_SCRATCH_REPO:"
say ""
say "       cd $WORK/checkout"
say "       $VS submit $RUN_ID"
say ""
say "   Run submit from THIS checkout: it is a clone of the scratch repo (submit"
say "   refuses a checkout of a different repository) and it holds the"
say "   Run_Directory with the bundle submit imports before pushing."
say ""
say "When you are done, delete the temp checkout:  rm -rf $WORK"
say "==========================================================================="
