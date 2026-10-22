"""Click commands for VaultShield Protect, attached to the main group by
`register_protect_commands` (called at the bottom of `vaultshield/cli.py`).

Every message names files, variables and line numbers only. No value is ever
printed."""

from __future__ import annotations

from pathlib import Path

import click

from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect import protector
from vaultshield.protect.discovery import KIND_ENV, KIND_SECRET, discover
from vaultshield.protect.manifest import ManifestError


def _line_numbers(lines: list[int]) -> str:
    return "line " + ", ".join(map(str, lines)) if len(lines) == 1 else "lines " + ", ".join(map(str, lines))


def _refusal_text(reason: str, lines: list[int]) -> str:
    return f"{reason} ({_line_numbers(lines)})" if lines else reason


def _dry_run(root: Path) -> None:
    found = discover(root)
    if not found:
        click.echo("No secret files found.")
        return
    will, nothing, cannot = [], [], []
    for f in (f for f in found if f.kind == KIND_ENV):
        analysis = protector.analyze(root / f.path)
        if analysis.refusal:
            cannot.append((f.path, analysis))
        elif analysis.real:
            will.append((f.path, len(analysis.real)))
        else:
            nothing.append(f.path)
    secret_files = [f.path for f in found if f.kind == KIND_SECRET]

    if will:
        click.echo("Would protect (values move to the Keychain):")
        for path, count in will:
            click.echo(f"  {path}  ({count} variable{'s' if count != 1 else ''})")
    if cannot:
        click.echo("Cannot protect (nothing would change):")
        for path, analysis in cannot:
            click.echo(f"  {path}  {_refusal_text(analysis.refusal, analysis.refusal_lines)}")
    if nothing:
        click.echo("Already protected, or no values to protect:")
        for path in nothing:
            click.echo(f"  {path}")
    if secret_files:
        click.echo("Detected, not moved (reported only):")
        for path in secret_files:
            click.echo(f"  {path}")
    click.echo("Dry run: nothing was changed.")


@click.command("protect")
@click.option(
    "--dry-run",
    is_flag=True,
    help="List the secret files that would be handled. Changes nothing.",
)
def protect_command(dry_run: bool) -> None:
    """Move secret values out of this project's files and into the Keychain."""
    root = Path.cwd()
    if dry_run:
        _dry_run(root)
        return

    try:
        report = protector.protect_project(root)
    except ManifestError as exc:
        raise click.ClickException(f"{manifest_mod.MANIFEST_DIR}/{manifest_mod.MANIFEST_NAME}: {exc}")

    if not report.files and not report.detected:
        click.echo("No secret files found.")
        return

    moved = 0
    for result in report.files:
        if result.status == protector.STATUS_PROTECTED:
            moved += len(result.names)
            click.echo(f"Protected {result.path}: {', '.join(result.names)}")
        elif result.status == protector.STATUS_UNCHANGED:
            click.echo(f"Unchanged {result.path} (already protected, or no values to protect)")
        else:
            click.echo(
                f"Refused {result.path}: {_refusal_text(result.reason, result.lines)}. "
                "Nothing in this file was changed."
            )
    not_moved = [p for p in report.detected if p not in {r.path for r in report.refused}]
    if not_moved:
        click.echo("Detected, not moved (reported only): " + ", ".join(not_moved))
    click.echo(f"{moved} variable{'s' if moved != 1 else ''} moved to the Keychain.")
    if report.refused:
        click.get_current_context().exit(1)


@click.command("unprotect")
def unprotect_command() -> None:
    """Put the real values back into this project's files and forget them in the Keychain."""
    root = Path.cwd()
    try:
        results = protector.unprotect_project(root)
    except ManifestError as exc:
        raise click.ClickException(f"{manifest_mod.MANIFEST_DIR}/{manifest_mod.MANIFEST_NAME}: {exc}")

    if not results:
        click.echo("Nothing is protected here.")
        return
    refused = False
    for result in results:
        if result.status == protector.STATUS_RESTORED:
            click.echo(f"Restored {result.path}: {', '.join(result.names)}")
        else:
            refused = True
            click.echo(f"Refused {result.path}: {result.reason}.")
    if refused:
        click.get_current_context().exit(1)


def register_protect_commands(group: click.Group) -> None:
    """Adds every Protect command to `group`."""
    group.add_command(protect_command)
    group.add_command(unprotect_command)
