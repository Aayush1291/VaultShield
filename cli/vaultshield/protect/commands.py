"""Click commands for VaultShield Protect, attached to the main group by
`register_protect_commands` (called at the bottom of `vaultshield/cli.py`).

Every message names files, variables and line numbers only. No value is ever
printed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from vaultshield.protect import execute
from vaultshield.protect.agents import antigravity
from vaultshield.protect.agents import install as agent_install
from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect import protector
from vaultshield.protect.discovery import KIND_ENV, KIND_SECRET, discover
from vaultshield.protect.manifest import Manifest, ManifestError


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


@click.command("exec", context_settings={"ignore_unknown_options": True})
@click.option(
    "--dir",
    "directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Load the protected files in this directory, and run the command there.",
)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def exec_command(directory: Path, command: tuple[str, ...]) -> None:
    """Run COMMAND with the real values in its environment.

    Put `--` before the command:  vaultshield exec -- npm start

    The command's output is scrubbed: the values show as ***. A variable that
    is already set in your environment is not overridden.
    """
    try:
        prepared = execute.prepare(directory, dict(os.environ))
    except (execute.ExecError, ManifestError) as exc:
        raise click.ClickException(str(exc))
    for missing in prepared.missing:
        click.echo(f"vaultshield exec: no value in the Keychain for {missing}; it is not set", err=True)
    code = execute.run_command(prepared, list(command))
    click.get_current_context().exit(code)


@click.command("status")
def status_command() -> None:
    """Show the protected files and their variable NAMES. Never shows values."""
    root = manifest_mod.find_root(Path.cwd())
    try:
        manifest = manifest_mod.load(root) if root is not None else None
    except ManifestError as exc:
        raise click.ClickException(f"{manifest_mod.MANIFEST_DIR}/{manifest_mod.MANIFEST_NAME}: {exc}")
    if manifest is None or not manifest.all_paths():
        click.echo("Nothing is protected here.")
        return

    click.echo(f"Project: {root}")
    if manifest.protected:
        click.echo("Protected (values are in the Keychain):")
        for path, names in sorted(manifest.protected.items()):
            click.echo(f"  {path}")
            for name in names:
                click.echo(f"    {name}")
    else:
        click.echo("Protected: none")
    if manifest.detected:
        click.echo("Detected, not moved (their real contents are still on disk):")
        for path in sorted(manifest.detected):
            click.echo(f"  {path}")


@click.group("agents")
def agents_group() -> None:
    """Agent-specific configuration."""


@agents_group.command("render")
@click.argument("agent", type=click.Choice(["antigravity"]))
def render_command(agent: str) -> None:
    """Print the deny rules and hook for AGENT. Prints only; installs nothing."""
    root = manifest_mod.find_root(Path.cwd())
    if root is None:
        raise click.ClickException("no protected files found; run `vaultshield protect` first")
    try:
        manifest = manifest_mod.load(root)
    except ManifestError as exc:
        raise click.ClickException(str(exc))
    if manifest is None or not manifest.all_paths():
        raise click.ClickException("no protected files found; run `vaultshield protect` first")
    click.echo(antigravity.render(root, manifest))


def _require_project() -> tuple[Path, Manifest]:
    """The project at or above the current folder, with its manifest."""
    root = manifest_mod.find_root(Path.cwd())
    try:
        manifest = manifest_mod.load(root) if root is not None else None
    except ManifestError as exc:
        raise click.ClickException(f"{manifest_mod.MANIFEST_DIR}/{manifest_mod.MANIFEST_NAME}: {exc}")
    if root is None or manifest is None or not manifest.all_paths():
        raise click.ClickException("no protected files found; run `vaultshield protect` first")
    return root, manifest


def _home_or_default(home: Path | None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _shown(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


_home_option = click.option(
    "--home",
    "home",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Use this folder as the home directory. Default: your real home.",
)


@agents_group.command("install")
@click.argument("agent", type=click.Choice(["antigravity"]))
@_home_option
def install_command(agent: str, home: Path | None) -> None:
    """Install deny rules and the hook for AGENT. Merge-only; `uninstall` undoes it."""
    root, manifest = _require_project()
    home = _home_or_default(home)
    try:
        actions = agent_install.install(home, root, manifest)
    except agent_install.InstallError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Antigravity backstops for {root} ({len(manifest.all_paths())} files, exact absolute paths)")
    click.echo("Quit Antigravity before installing: a running App can overwrite config.json.")
    for action in actions:
        where = _shown(action.target.path, home)
        if action.status == "skipped":
            click.echo(f"  {action.target.label}: skipped, {action.detail}")
            continue
        extra = f" ({action.added} deny rule{'s' if action.added != 1 else ''} added)" if action.target.kind != agent_install.KIND_HOOK else ""
        click.echo(f"  {action.target.label} {where}: {action.status}{extra}")
    if all(a.status == "skipped" for a in actions):
        raise click.ClickException("nothing was installed: Antigravity is not set up under this home")
    click.echo("Check it with: vaultshield agents doctor antigravity")


@click.group("hook")
def hook_group() -> None:
    """Hooks that an agent calls. Not for typing by hand."""


@hook_group.command("antigravity")
def hook_antigravity_command() -> None:
    """Antigravity PreToolUse hook: reads the tool call on stdin, prints a decision."""
    click.echo(antigravity.handle_hook(sys.stdin.read()))


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
    group.add_command(exec_command)
    group.add_command(status_command)
    group.add_command(agents_group)
    group.add_command(hook_group)
