#!/bin/sh
# Build the VaultShield wheel into website/dist/ so install.sh can fetch it.
#
# Uses the cli/.venv's own pip (`pip wheel --no-deps`): the wheel carries no
# bundled dependencies, and `install.sh` resolves those from PyPI at install
# time. The built wheel is copied to website/dist/vaultshield-<version>.whl —
# the exact path install.sh downloads; install.sh re-saves it under the proper
# wheel filename before `pip install`, so pip is happy either way.
#
# Serve the result with:  cd website && python3 -m http.server 8080

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$REPO/cli"
DIST="$REPO/website/dist"
VENV_PYTHON="$CLI/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    printf '%s\n' "No cli/.venv found at $VENV_PYTHON."
    printf '%s\n' "Create it first:  python3 -m venv cli/.venv && cli/.venv/bin/pip install -e 'cli[dev]'"
    exit 1
fi

VERSION="$("$VENV_PYTHON" - "$CLI/pyproject.toml" <<'PY'
import pathlib, sys, tomllib
print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["project"]["version"])
PY
)"

mkdir -p "$DIST"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

"$VENV_PYTHON" -m pip wheel --no-deps -w "$WORK" "$CLI" >/dev/null

BUILT="$(ls "$WORK"/vaultshield-*.whl 2>/dev/null | head -n 1)"
if [ -z "$BUILT" ]; then
    printf '%s\n' "pip wheel produced no wheel."
    exit 1
fi

TARGET="$DIST/vaultshield-$VERSION.whl"
cp "$BUILT" "$TARGET"
printf '%s\n' "vaultshield-$VERSION.whl"
