"""Click commands for VaultShield Protect, attached to the main group by
`register_protect_commands` (called at the bottom of `vaultshield/cli.py`).

Every message names files, variables and line numbers only. No value is ever
printed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from vaultshield import uploader
from vaultshield.config import ConfigError, load_config
from vaultshield.protect import PLACEHOLDER
from vaultshield.protect import execute
from vaultshield.protect import sync as sync_mod
from vaultshield.protect.agents import antigravity
from vaultshield.protect.agents import agentdoctor, agentinstall, claude_code, codex, kiro
from vaultshield.protect.agents import doctor as agent_doctor
from vaultshield.protect.agents import install as agent_install
from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect import protector
from vaultshield.protect.discovery import KIND_ENV, KIND_SECRET, discover
from vaultshield.protect.manifest import Manifest, ManifestError

# The default dashboard backend, matching `vaultshield run`'s `--backend-url`
# (cli.py `_DEFAULT_BACKEND_URL` / `_BACKEND_URL_ENVVAR`). Spelled here rather
# than imported, because cli.py imports this module and the reverse import would
# be circular; the two strings are the one contract `protect sync` shares with
# `run`, and a test pins them equal.
_SYNC_DEFAULT_BACKEND_URL = "http://localhost:8000"
_SYNC_BACKEND_ENVVAR = "VAULTSHIELD_BACKEND_URL"


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


@click.group("protect", invoke_without_command=True)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List the secret files that would be handled. Changes nothing.",
)
@click.pass_context
def protect_command(ctx: click.Context, dry_run: bool) -> None:
    """Move secret values out of this project's files and into the Keychain.

    With no subcommand this protects the current project, exactly as before.
    `protect sync` pushes recorded protection and orchestrator activity to the
    dashboard.
    """
    # A subcommand (e.g. `protect sync`) runs on its own; the group body is only
    # the bare `protect` / `protect --dry-run` behaviour.
    if ctx.invoked_subcommand is not None:
        return
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


def _resolve_sync_project(project: str | None, root: Path) -> str:
    """The backend project id for this sync, resolved like `vaultshield run`:
    `--project` wins, then `.vaultshield.yml`'s `project:`, else a clear error.

    `--project`, when given, is used without reading `.vaultshield.yml` at all,
    so a sync works even where there is no config. Otherwise the config is read
    (and a malformed one surfaces its own error, as `run` would)."""
    explicit = (project or "").strip()
    if explicit:
        return explicit
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    resolved = config.project.strip()
    if not resolved:
        raise click.ClickException(
            "No project for this sync: pass --project <project-id> or set "
            "`project: <project-id>` in .vaultshield.yml (ids are listed by "
            "the dashboard / GET /api/projects/)."
        )
    return resolved


@protect_command.command("sync")
@click.option(
    "--project",
    "project",
    default=None,
    help="Backend project id to register the Run under. Overrides `project:` in .vaultshield.yml.",
)
@click.option(
    "--backend-url",
    default=_SYNC_DEFAULT_BACKEND_URL,
    show_default=True,
    envvar=_SYNC_BACKEND_ENVVAR,
    help=f"VaultShield backend the protection log is uploaded to.  [env var: {_SYNC_BACKEND_ENVVAR}]",
)
def sync_command(project: str | None, backend_url: str) -> None:
    """Push unsynced protection and orchestrator activity to the dashboard.

    Reads this project's `.vaultshield/violations.jsonl` and
    `.vaultshield/orchestrator.jsonl`, uploads whatever is new as one Run (agent
    `protect`) marked ready for review, and remembers how far it got so the next
    run sends only what came after. Needs a `vaultshield login` token. If nothing
    is new, it says so and creates no Run.
    """
    root = sync_mod.project_dir(Path.cwd())
    resolved = _resolve_sync_project(project, root)
    try:
        token = uploader.load_token()
    except uploader.UploadError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        outcome = sync_mod.perform(root, resolved, backend_url, token)
    except uploader.UploadError as exc:
        raise click.ClickException(
            f"Sync did not complete: {exc} (nothing was marked synced, so the next "
            "`vaultshield protect sync` resends it)"
        ) from exc
    except sync_mod.SyncError as exc:
        raise click.ClickException(str(exc)) from exc

    if outcome.nothing:
        click.echo("nothing to sync")
        return
    plural = "s" if outcome.event_count != 1 else ""
    click.echo(
        f"Synced {outcome.event_count} event{plural} to run {outcome.run_id} "
        f"({backend_url}); it is awaiting review in the dashboard."
    )


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


# The agents added on Day 3, keyed by name. Antigravity keeps its own install/doctor
# modules (install.py, doctor.py); these three share agentinstall.py and agentdoctor.py.
NEW_AGENTS = {claude_code.AGENT: claude_code, kiro.AGENT: kiro, codex.AGENT: codex}
_AGENT_CHOICE = click.Choice(["antigravity", *NEW_AGENTS])


@click.group("agents")
def agents_group() -> None:
    """Agent-specific configuration."""


@agents_group.command("render")
@click.argument("agent", type=_AGENT_CHOICE)
def render_command(agent: str) -> None:
    """Print the deny rules and hook for AGENT. Prints only; installs nothing."""
    root, manifest = _require_project()
    module = antigravity if agent == "antigravity" else NEW_AGENTS[agent]
    click.echo(module.render(root, manifest))


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


def _install_new(module, home: Path) -> None:
    """Install the backstops for one of the Day 3 agents (claude-code, kiro, codex)."""
    root, manifest = _require_project()
    try:
        actions = agentinstall.install(module.install_edits(home, root, manifest))
    except agentinstall.InstallError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{module.AGENT_LABEL} backstops for {root} ({len(manifest.all_paths())} files, exact absolute paths)")
    for note in module.INSTALL_NOTES:
        click.echo(note)
    for action in actions:
        if action.status == "skipped":
            click.echo(f"  {action.label}: skipped, {action.detail}")
            continue
        extra = f" ({action.detail})" if action.detail else ""
        click.echo(f"  {action.label} {_shown(action.path, home)}: {action.status}{extra}")
    if all(a.status == "skipped" for a in actions):
        raise click.ClickException(f"nothing was installed: {module.AGENT_LABEL} is not set up under this home")
    click.echo(f"Check it with: vaultshield agents doctor {module.AGENT}")


def _uninstall_new(module, home: Path) -> None:
    try:
        actions = agentinstall.uninstall(module.uninstall_edits(home))
    except agentinstall.InstallError as exc:
        raise click.ClickException(str(exc))
    for action in actions:
        detail = f" ({action.detail})" if action.detail else ""
        click.echo(f"  {action.label} {_shown(action.path, home)}: {action.status}{detail}")
    if all(a.status == "nothing" for a in actions):
        click.echo("Nothing to uninstall.")


def _emit_hook(output) -> None:
    if output.stdout:
        click.echo(output.stdout)
    if output.stderr:
        click.echo(output.stderr, err=True)
    click.get_current_context().exit(output.exit_code)


@agents_group.command("install")
@click.argument("agent", type=_AGENT_CHOICE)
@_home_option
def install_command(agent: str, home: Path | None) -> None:
    """Install deny rules and the hook for AGENT. Merge-only; `uninstall` undoes it."""
    home = _home_or_default(home)
    if agent != "antigravity":
        _install_new(NEW_AGENTS[agent], home)
        return
    root, manifest = _require_project()
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


@agents_group.command("uninstall")
@click.argument("agent", type=_AGENT_CHOICE)
@_home_option
def uninstall_command(agent: str, home: Path | None) -> None:
    """Put every file `install` touched back exactly as it was."""
    home = _home_or_default(home)
    if agent != "antigravity":
        _uninstall_new(NEW_AGENTS[agent], home)
        return
    try:
        actions = agent_install.uninstall(home)
    except agent_install.InstallError as exc:
        raise click.ClickException(str(exc))

    click.echo("Quit Antigravity before uninstalling: a running App can overwrite config.json.")
    for action in actions:
        where = _shown(action.target.path, home)
        detail = f" ({action.detail})" if action.detail else ""
        click.echo(f"  {action.target.label} {where}: {action.status}{detail}")
    if all(a.status == "nothing" for a in actions):
        click.echo("Nothing to uninstall.")


def _doctor_antigravity(root: Path, manifest: Manifest, home: Path) -> bool:
    report = agent_doctor.diagnose(home, root, manifest)
    click.echo(f"Antigravity protection for {root}")
    for surface in report.surfaces:
        click.echo("")
        click.echo(f"{surface.name}  {_shown(surface.path, home)}")
        for label, check in (
            ("placeholders", surface.placeholders),
            ("deny rules", surface.deny),
            ("hook", surface.hook),
        ):
            click.echo(f"  {label:<13} {check.state:<11} {check.detail}")
    if report.not_covered:
        click.echo("")
        click.echo(
            "Not covered by placeholders (detected, not moved; their real contents are still on disk): "
            + ", ".join(report.not_covered)
        )
    if report.credential_grants and report.credential_grants[1]:
        total, flagged = report.credential_grants
        click.echo("")
        click.echo(
            f"WARN: {len(flagged)} of {total} grants in {_shown(home / agent_install.APP_CONFIG, home)} look like "
            "they contain credentials, stored in plain text."
        )
        click.echo(
            "      Grant positions (0-based, in userSettings.globalPermissionGrants.allow): "
            + ", ".join(map(str, flagged))
            + ". The values are not shown."
        )
    click.echo("")
    click.echo("Everything is active." if report.ok else "Not everything is active.")
    return report.ok


def _doctor_new(module, root: Path, manifest: Manifest, home: Path) -> bool:
    report = module.diagnose(home, root, manifest)
    click.echo(f"{module.AGENT_LABEL} protection for {root}")
    for line in agentdoctor.render(report):
        click.echo(line)
    click.echo("")
    click.echo("Everything is active." if report.ok else "Not everything is active.")
    return report.ok


def _doctor_one(agent: str, root: Path, manifest: Manifest, home: Path) -> bool:
    if agent == "antigravity":
        return _doctor_antigravity(root, manifest, home)
    return _doctor_new(NEW_AGENTS[agent], root, manifest, home)


@agents_group.command("doctor")
@click.argument("agent", type=click.Choice(["antigravity", *NEW_AGENTS, "all"]))
def doctor_command(agent: str) -> None:
    """Show, per surface, whether placeholders, deny rules and the hook are active. AGENT may be `all`."""
    root, manifest = _require_project()
    home = Path.home()
    if agent != "all":
        if not _doctor_one(agent, root, manifest, home):
            click.get_current_context().exit(1)
        return
    all_ok = True
    for index, name in enumerate(["antigravity", *NEW_AGENTS]):
        if index:
            click.echo("")
            click.echo("-" * 70)
        all_ok = _doctor_one(name, root, manifest, home) and all_ok
    click.echo("")
    click.echo("All agents: everything is active." if all_ok else "All agents: at least one layer is not active.")
    if not all_ok:
        click.get_current_context().exit(1)


@click.group("hook")
def hook_group() -> None:
    """Hooks that an agent calls. Not for typing by hand."""


@hook_group.command("antigravity")
def hook_antigravity_command() -> None:
    """Antigravity PreToolUse hook: reads the tool call on stdin, prints a decision."""
    click.echo(antigravity.handle_hook(sys.stdin.read()))


@hook_group.command("claude-code")
def hook_claude_code_command() -> None:
    """Claude Code PreToolUse hook: reads the tool call on stdin, decides, exits 2 to block."""
    _emit_hook(claude_code.handle_hook(sys.stdin.read()))


@hook_group.command("kiro")
def hook_kiro_command() -> None:
    """Kiro PreToolUse hook: reads the tool call on stdin, decides, exits 2 to block."""
    _emit_hook(kiro.handle_hook(sys.stdin.read()))


@hook_group.command("codex")
def hook_codex_command() -> None:
    """Codex PreToolUse hook: reads the tool call on stdin, decides, exits 2 to block."""
    _emit_hook(codex.handle_hook(sys.stdin.read()))


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


# `vaultshield secure`: the one friendly command. Which agents it can add
# backstops for, each by the config directory whose presence means the agent is
# set up, with a one-line description of what installing changes.
_SECURE_AGENTS = [
    ("antigravity", ".gemini", "exact-path deny rules and a command hook in Antigravity's config"),
    ("claude-code", ".claude", "Read deny rules and a PreToolUse hook in ~/.claude/settings.json"),
    ("kiro", ".kiro", "fs_read deny rules and a PreToolUse hook under ~/.kiro"),
    ("codex", ".codex", "a permission profile (left unselected) and a hook under ~/.codex"),
]


def _secure_install_agent(agent: str, home: Path, root: Path, manifest: Manifest) -> None:
    """Install one agent's backstops, reusing the existing installers."""
    if agent == "antigravity":
        agent_install.install(home, root, manifest)
    else:
        agentinstall.install(NEW_AGENTS[agent].install_edits(home, root, manifest))


@click.command("secure")
@click.option(
    "--dir",
    "directory",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="The project directory to secure.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Protect without the confirmation prompt. Never installs any agent backstop.",
)
def secure_command(directory: Path, yes: bool) -> None:
    """Protect this project's secrets in one friendly step.

    Shows what it found (names only), asks before changing anything, moves secret
    values into the macOS Keychain, optionally adds per-agent backstops, and ends
    with how to undo it.
    """
    root = directory.resolve()

    # (a) discovery, in plain English (names and counts only, never values).
    found = discover(root)
    if not found:
        click.echo(f"No secret files found in {root}. Nothing to protect.")
        return
    env_files = [f for f in found if f.kind == KIND_ENV]
    secret_files = [f for f in found if f.kind == KIND_SECRET]

    click.echo(f"VaultShield looked in {root} and found:")
    for f in env_files:
        analysis = protector.analyze(root / f.path)
        if analysis.refusal:
            click.echo(f"  {f.path} — cannot be protected safely ({analysis.refusal})")
            continue
        names = sorted(set(analysis.real) | set(analysis.placeholders))
        if not names:
            click.echo(f"  {f.path} — no values to protect")
        else:
            count = len(names)
            click.echo(f"  {f.path} — {count} variable{'s' if count != 1 else ''}: {', '.join(names)}")
    for f in secret_files:
        click.echo(f"  {f.path} — a key or credential file (detected only; its contents stay on disk)")
    click.echo("Only the names are shown above; VaultShield never prints a value.")

    # (b) one confirmation, then the existing protect.
    if not yes and not click.confirm("Move these secret values into the macOS Keychain now?", default=False):
        click.echo("Nothing was changed.")
        return
    try:
        report = protector.protect_project(root)
    except ManifestError as exc:
        raise click.ClickException(f"{manifest_mod.MANIFEST_DIR}/{manifest_mod.MANIFEST_NAME}: {exc}")

    moved = 0
    protected_files = 0
    for result in report.files:
        if result.status == protector.STATUS_PROTECTED:
            moved += len(result.names)
            protected_files += 1
        elif result.status == protector.STATUS_REFUSED:
            click.echo(f"  Skipped {result.path}: {result.reason}.")
    click.echo(
        f"Moved {moved} value{'s' if moved != 1 else ''} from {protected_files} "
        f"file{'s' if protected_files != 1 else ''} into the Keychain; the files now hold {PLACEHOLDER}."
    )

    try:
        manifest = manifest_mod.load(root)
    except ManifestError:
        manifest = None

    # (c) per-agent backstops: detected by config dir, default no, one prompt each.
    #     Skipped entirely with --yes, which never installs a backstop.
    installed: list[str] = []
    home = Path.home()
    if not yes and manifest is not None and manifest.all_paths():
        detected = [(agent, why) for agent, dirname, why in _SECURE_AGENTS if (home / dirname).is_dir()]
        if detected:
            click.echo("")
            click.echo("These AI agents are set up on this Mac. The placeholders already protect every")
            click.echo("agent; the backstops below are optional extra rules (default: no).")
            for agent, why in detected:
                if click.confirm(f"  Add backstops for {agent}? (adds {why})", default=False):
                    try:
                        _secure_install_agent(agent, home, root, manifest)
                        installed.append(agent)
                    except agent_install.InstallError as exc:
                        click.echo(f"    Could not add {agent} backstops: {exc}")

    # (d) the existing doctor for whatever was installed.
    if installed and manifest is not None:
        click.echo("")
        for agent in installed:
            _doctor_one(agent, root, manifest, home)

    # (e) a plain summary.
    click.echo("")
    click.echo("Summary:")
    click.echo(
        f"  Secrets moved to the Keychain: {moved} value{'s' if moved != 1 else ''} "
        f"in {protected_files} file{'s' if protected_files != 1 else ''}."
    )
    if report.detected:
        click.echo("  Detected but left on disk (not .env format): " + ", ".join(sorted(report.detected)))
    click.echo("  Agent backstops added: " + (", ".join(installed) if installed else "none") + ".")
    click.echo("To undo: vaultshield unprotect")


def register_protect_commands(group: click.Group) -> None:
    """Adds every Protect command to `group`."""
    group.add_command(protect_command)
    group.add_command(secure_command)
    group.add_command(unprotect_command)
    group.add_command(exec_command)
    group.add_command(status_command)
    group.add_command(agents_group)
    group.add_command(hook_group)
