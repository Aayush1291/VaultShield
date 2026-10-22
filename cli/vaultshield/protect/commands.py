"""Click commands for VaultShield Protect, attached to the main group by
`register_protect_commands` (called at the bottom of `vaultshield/cli.py`)."""

from __future__ import annotations

from pathlib import Path

import click

from vaultshield.protect.discovery import KIND_ENV, KIND_SECRET, discover


@click.command("protect")
@click.option(
    "--dry-run",
    is_flag=True,
    help="List the secret files that would be handled. Changes nothing.",
)
def protect_command(dry_run: bool) -> None:
    """Move secret values out of this project's files and into the Keychain."""
    root = Path.cwd()
    if not dry_run:
        raise click.ClickException("protect: not implemented yet; use --dry-run")
    found = discover(root)
    env_files = [f for f in found if f.kind == KIND_ENV]
    secret_files = [f for f in found if f.kind == KIND_SECRET]
    if not found:
        click.echo("No secret files found.")
        return
    if env_files:
        click.echo("Would protect (values move to the Keychain):")
        for f in env_files:
            click.echo(f"  {f.path}")
    if secret_files:
        click.echo("Detected, not moved (reported only):")
        for f in secret_files:
            click.echo(f"  {f.path}")
    click.echo("Dry run: nothing was changed.")


def register_protect_commands(group: click.Group) -> None:
    """Adds every Protect command to `group`."""
    group.add_command(protect_command)
