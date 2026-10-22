"""Click commands for VaultShield Orchestrate, attached to the main group by
`register_orchestrate_commands` (called at the bottom of `vaultshield/cli.py`).

No message here ever contains a secret value or a full API key."""

from __future__ import annotations

import click

from vaultshield.orchestrate import keys


@click.group("config")
def config_group() -> None:
    """VaultShield settings."""


class _KeyNeverInArguments(click.Command):
    """Refuses a key typed on the command line, without repeating it. Click's own
    "unexpected extra argument" error would print the argument back."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if len([a for a in args if not a.startswith("-")]) > 1:
            raise click.UsageError(
                "Do not put the key on the command line (it would stay in your shell history). "
                "Run the command with just the provider name and type the key at the prompt."
            )
        return super().parse_args(ctx, args)


@config_group.command("set-key", cls=_KeyNeverInArguments)
@click.argument("provider", type=click.Choice(list(keys.PROVIDERS)))
def set_key_command(provider: str) -> None:
    """Store the model API key in the Keychain. The key is typed at a hidden prompt."""
    label = keys.LABELS[provider]
    # Never an argument and never echoed: it would end up in shell history and `ps`.
    entered = click.prompt(f"{label} API key (input is hidden)", hide_input=True, default="", show_default=False)
    key = entered.strip()
    if not key:
        raise click.ClickException("No key was entered; nothing was stored.")
    try:
        keys.store_key(provider, key)
    except keys.ModelKeyError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Stored the {label} API key in the Keychain. Check it with: vaultshield config show-key {provider}")


@config_group.command("show-key")
@click.argument("provider", type=click.Choice(list(keys.PROVIDERS)))
def show_key_command(provider: str) -> None:
    """Say whether a key is set and show its last 4 characters. Never the whole key."""
    try:
        resolved = keys.resolve_key(provider)
    except keys.ModelKeyError as exc:
        raise click.ClickException(str(exc))
    if resolved is None:
        click.echo(f"{keys.LABELS[provider]} API key: not set. Run: vaultshield config set-key {provider}")
        click.get_current_context().exit(1)
    click.echo(resolved.describe())


def register_orchestrate_commands(group: click.Group) -> None:
    """Adds every Orchestrate command to `group`."""
    group.add_command(config_group)
