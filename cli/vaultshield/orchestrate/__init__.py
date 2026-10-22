"""VaultShield Orchestrate: before an agent starts, read the project, ask the
developer a few clarifying questions, and build an enriched prompt.

The orchestrator itself calls an AI model, so it must never send that model a
secret value or the contents of a secret file. See README.md in this directory
for exactly what is and is not sent.
"""
