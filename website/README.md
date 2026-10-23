# VaultShield installer & landing page

A one-command install for a non-technical macOS user, plus the page that hands
out the command. Everything here is for **local** use (served from your own Mac
on `http://localhost:8080`); hosting it on the internet is out of scope.

```
website/
  index.html     the landing page (self-contained: no CDN, fonts, or analytics)
  install.sh     the macOS installer the page tells people to curl | sh
  build.sh       builds the wheel into dist/
  dist/          the built wheel (git-ignored)
```

## Build the wheel

Needs `cli/.venv` (the project's dev environment):

```
./website/build.sh
```

It runs `pip wheel --no-deps` with the venv's own pip and copies the result to
`website/dist/vaultshield-<version>.whl` — the exact path `install.sh` downloads.
It prints the file name. (The first build fetches the build backend, `hatchling`,
the usual way `pip wheel` does.)

## Serve it

```
cd website && python3 -m http.server 8080
```

Then open <http://localhost:8080> and read the page, or just run the install
command below.

## Try the install

```
curl -fsSL http://localhost:8080/install.sh | sh
```

It:

- finds a Python 3.11+ (`python3.13` → `python3.12` → `python3.11` → `python3`);
  if none, it prints `brew install python@3.12` and installs nothing;
- creates one private venv at `~/.vaultshield/venv`;
- downloads `/dist/vaultshield-<version>.whl` and `pip install`s it there;
- writes a launcher at `~/.local/bin/vaultshield` that runs that one venv's
  Python, so there is exactly one interpreter;
- never edits your shell startup files — if `~/.local/bin` is not on your `PATH`
  it prints the one line to add;
- is idempotent: re-running upgrades in place.

Finish by protecting a project:

```
cd your-project
vaultshield secure
```

Remove the venv and launcher (your Keychain items and project files are left
alone — run `vaultshield unprotect` in each protected project first):

```
curl -fsSL http://localhost:8080/install.sh | sh -s -- --uninstall
```

### Overrides

| Variable | Default | Meaning |
|---|---|---|
| `VAULTSHIELD_HOME` | `~/.vaultshield/venv` | the private venv directory |
| `VAULTSHIELD_BIN` | `~/.local/bin/vaultshield` | the launcher path |
| `VAULTSHIELD_BASE` | `http://localhost:8080` | where the wheel is fetched from |

The installer's `VERSION` is kept in step with `cli/pyproject.toml` and
`cli/vaultshield/__init__.py` (a test fails if they drift). The wheel is served
under the short name `vaultshield-<version>.whl`; `install.sh` saves the download
under the proper wheel filename before `pip install`, so pip accepts it.

## Try it on a clean machine

On a Mac that does not have VaultShield:

1. Copy this repo (or just `website/` plus a built `dist/` wheel) to the machine.
2. `cd website && python3 -m http.server 8080`.
3. In another Terminal: `curl -fsSL http://localhost:8080/install.sh | sh`.
4. Check it: `~/.local/bin/vaultshield --version`, then in a throwaway project
   with a `.env`, `vaultshield secure`.
5. Clean up: `curl -fsSL http://localhost:8080/install.sh | sh -s -- --uninstall`.

For an automated end-to-end check that installs into a **temp prefix** and
cleans up (it does a real `pip install`, so it needs the network), run once:

```
spike/install_smoke.sh
```

It points `VAULTSHIELD_HOME`/`VAULTSHIELD_BIN` at a `/tmp` prefix, so your real
`~/.vaultshield/venv` and `~/.local/bin` are never touched, and it does not run
`secure`/`protect`, so it never writes to your Keychain.

## Tests

The installer, the `secure` command, and the page are covered by the CLI test
suite (no Docker, no network — the installer tests use fake `python`/`curl`
shims):

```
cd cli && .venv/bin/python -m pytest tests/test_install_sh.py \
  tests/test_protect_secure.py tests/test_website_html.py
```
