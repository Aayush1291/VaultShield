#!/bin/sh
# VaultShield installer (macOS). One command, one isolated Python, one launcher.
#
#   curl -fsSL http://localhost:8080/install.sh | sh
#
# It finds a Python >= 3.11, makes a private virtual environment just for
# VaultShield, installs VaultShield into it, and drops a small launcher on your
# PATH so you can type `vaultshield`. It installs no system software and never
# edits your shell startup files.
#
#   install.sh              install or upgrade
#   install.sh --uninstall  remove the venv and launcher (leaves your secrets alone)
#
# Overridable with environment variables:
#   VAULTSHIELD_HOME  the venv directory      (default ~/.vaultshield/venv)
#   VAULTSHIELD_BIN   the launcher path        (default ~/.local/bin/vaultshield)
#   VAULTSHIELD_BASE  where to fetch the wheel (default http://localhost:8080)

set -eu

# The version this installer fetches. Kept in step with cli/pyproject.toml and
# cli/vaultshield/__init__.py (a test asserts they match).
VERSION="0.1.0"

VAULTSHIELD_HOME="${VAULTSHIELD_HOME:-$HOME/.vaultshield/venv}"
VAULTSHIELD_BIN="${VAULTSHIELD_BIN:-$HOME/.local/bin/vaultshield}"
VAULTSHIELD_BASE="${VAULTSHIELD_BASE:-http://localhost:8080}"

VENV_PYTHON="$VAULTSHIELD_HOME/bin/python"

say() { printf '%s\n' "$*"; }

# --- uninstall (works on any OS; only removes files this installer created) ---
if [ "${1:-}" = "--uninstall" ]; then
    say "Removing VaultShield's venv and launcher only."
    say "Your Keychain items and project files are left untouched: run \`vaultshield unprotect\` in each protected project first."
    rm -rf "$VAULTSHIELD_HOME"
    rm -f "$VAULTSHIELD_BIN"
    say "Done. Removed $VAULTSHIELD_HOME and $VAULTSHIELD_BIN."
    exit 0
fi

if [ "${1:-}" != "" ]; then
    say "Unknown option: $1"
    say "Usage: install.sh [--uninstall]"
    exit 2
fi

# --- macOS only ---------------------------------------------------------------
OS="$(uname -s)"
if [ "$OS" != "Darwin" ]; then
    say "VaultShield's installer supports macOS only right now (you are on $OS)."
    say "Nothing was installed."
    exit 1
fi

# --- find one Python >= 3.11 --------------------------------------------------
find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if PYTHON="$(find_python)"; then
    say "Using Python: $PYTHON"
else
    say "VaultShield needs Python 3.11 or newer, and none was found."
    say "Install one with Homebrew, then run this installer again:"
    say "    brew install python@3.12"
    say "Nothing was installed."
    exit 1
fi

# --- the venv (reused on a re-run, so re-running upgrades) ---------------------
if [ -x "$VENV_PYTHON" ]; then
    say "Reusing the existing VaultShield environment at $VAULTSHIELD_HOME."
else
    say "Creating a private environment at $VAULTSHIELD_HOME ..."
    "$PYTHON" -m venv "$VAULTSHIELD_HOME"
fi

# --- download the wheel -------------------------------------------------------
WHEEL_URL="$VAULTSHIELD_BASE/dist/vaultshield-$VERSION.whl"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# Saved under the proper wheel name so pip accepts it, whatever the URL path is.
LOCAL_WHEEL="$WORK/vaultshield-$VERSION-py3-none-any.whl"

say "Downloading VaultShield from $WHEEL_URL ..."
if ! curl -fsSL -o "$LOCAL_WHEEL" "$WHEEL_URL"; then
    say "Could not download $WHEEL_URL."
    say "Is the server running?  cd website && python3 -m http.server 8080"
    exit 1
fi

# --- install it into the venv -------------------------------------------------
say "Installing VaultShield (this can take a minute the first time) ..."
"$VENV_PYTHON" -m pip install --default-timeout=300 --retries 10 --upgrade "$LOCAL_WHEEL"

# --- the launcher: exactly one Python -----------------------------------------
BIN_DIR="$(dirname "$VAULTSHIELD_BIN")"
mkdir -p "$BIN_DIR"
cat > "$VAULTSHIELD_BIN" <<EOF
#!/bin/sh
exec "$VENV_PYTHON" -m vaultshield.cli "\$@"
EOF
chmod +x "$VAULTSHIELD_BIN"
say "Installed the launcher at $VAULTSHIELD_BIN."

# --- PATH (we never edit your shell files) ------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *)
        say ""
        say "$BIN_DIR is not on your PATH. Add this line to your shell startup file (we did not):"
        say "    export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

# --- finish -------------------------------------------------------------------
say ""
"$VAULTSHIELD_BIN" --version
# `agents doctor all` only has something to report inside a protected project;
# outside one it prints a short "run protect first" note. Either way it is
# informational, so it never fails the install.
"$VAULTSHIELD_BIN" agents doctor all || true

say ""
say "What to do next:"
say "  1. Open Terminal and cd into a project that has a .env or other secret files."
say "  2. Run:  vaultshield secure"
say "     It shows what it found, asks before changing anything, and moves your"
say "     secret values into the macOS Keychain, leaving placeholders in the files."
say "  3. Point your AI coding agent at the project as usual; it sees the names,"
say "     never the values."
say ""
say "To undo in a project later:  vaultshield unprotect"
