# VaultShield CLI

The `vaultshield` command-line tool: sandboxes AI coding agent runs in
Docker, captures every action, and uploads the result to the VaultShield
dashboard for human review before anything reaches a real branch or pull
request.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```
