"""`vaultshield exec`: run a command with the real values in its environment.

The values come from the Keychain, go into the child's environment only, and
are removed from the child's output on the way out. Nothing is written to disk.

Which values are redacted (documented in README.md): a value is redacted from
the output when it is at least 4 characters long and is not only digits and not
one of true/false/yes/no/on/off/1/0 (case-insensitive). So `PORT=3000` and
`DEBUG=true` stay readable, and every other value shows as `***`.

What this does NOT catch: a value the child prints in another form (base64,
hex, URL-encoded, reversed, split by other text) or only a part of a value.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Mapping

import click

from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect.envfile import decode_record, value_of
from vaultshield.protect.vault import Vault, VaultError
from vaultshield.token_hygiene import SecretSet

_NOT_WORTH_REDACTING = frozenset({"true", "false", "yes", "no", "on", "off", "1", "0"})
_READ_SIZE = 64 * 1024


class ExecError(Exception):
    """`exec` cannot start. The message never contains a value."""


def scrubbable(value: str) -> bool:
    """Whether `value` is redacted from a command's output."""
    if len(value) < 4:
        return False
    if value.isdigit() or value.lower() in _NOT_WORTH_REDACTING:
        return False
    return True


class StreamScrubber:
    """Replaces secret values with `***` in a byte stream, including a value
    that arrives split across two or more chunks.

    Complete matches are replaced with `SecretSet.scrub`. To catch a value that
    straddles a chunk boundary, the tail of the data that could still turn into
    a match is held back until the next chunk (or `flush`).
    """

    def __init__(self, values: Iterable[str]) -> None:
        self._secrets = SecretSet()
        needles = {v.encode("utf-8") for v in values if v}
        for needle in needles:
            self._secrets.add_needle(needle)
        self._needles = sorted(needles, key=len, reverse=True)
        self._pending = b""

    def __repr__(self) -> str:  # never shows values
        return f"StreamScrubber(needles={len(self._needles)})"

    def feed(self, chunk: bytes) -> bytes:
        """Bytes that are safe to write now."""
        data = self._pending + chunk
        cut = self._safe_cut(data)
        ready, self._pending = data[:cut], data[cut:]
        return self._scrub(ready)

    def flush(self) -> bytes:
        """The held-back tail, once the stream has ended."""
        ready, self._pending = self._pending, b""
        return self._scrub(ready)

    def _scrub(self, data: bytes) -> bytes:
        if not data or not self._needles:
            return data
        # surrogateescape round-trips any bytes, so binary output passes through
        # unchanged apart from the redacted values.
        text = data.decode("utf-8", "surrogateescape")
        return self._secrets.scrub(text).encode("utf-8", "surrogateescape")

    def _safe_cut(self, data: bytes) -> int:
        """Where `data` can be split: everything before is final."""
        if not data or not self._needles:
            return len(data)
        # The longest tail of `data` that is the start of some value.
        hold = 0
        for needle in self._needles:
            for k in range(min(len(needle) - 1, len(data)), hold, -1):
                if needle[k - 1] == data[-1] and data.endswith(needle[:k]):
                    hold = k
                    break
        cut = len(data) - hold
        # A complete match must never be split by the cut.
        spans = []
        for needle in self._needles:
            start = data.find(needle)
            while start != -1:
                spans.append((start, start + len(needle)))
                start = data.find(needle, start + 1)
        moved = True
        while moved:
            moved = False
            for begin, end in spans:
                if begin < cut < end:
                    cut = begin
                    moved = True
        return cut


@dataclass
class Prepared:
    """Everything `run_command` needs. Holds values: never print or log it."""

    cwd: Path
    env: dict[str, str]
    secret_values: list[str] = field(default_factory=list, repr=False)
    missing: list[str] = field(default_factory=list)  # "<file>: <NAME>", names only

    def __repr__(self) -> str:
        return f"Prepared(cwd={self.cwd}, variables={len(self.secret_values)})"


def prepare(directory: Path, environ: Mapping[str, str]) -> Prepared:
    """Reads the protected files in `directory` from the Keychain and builds
    the child's environment. A variable that is already in `environ` wins."""
    start = directory.resolve()
    root = manifest_mod.find_root(start)
    if root is None:
        raise ExecError("no protected files found; run `vaultshield protect` first")
    manifest = manifest_mod.load(root)
    if manifest is None:
        raise ExecError("the protect manifest disappeared; run `vaultshield protect` again")
    relative = PurePosixPath(start.relative_to(root).as_posix())
    files = sorted(rel for rel in manifest.protected if PurePosixPath(rel).parent == relative)
    if not files:
        raise ExecError(
            f"no protected files in {start}; the project root is {root} "
            "(use --dir to pick the directory whose files to load)"
        )

    vault = Vault(manifest.project_id)
    stored: dict[str, str] = {}
    missing: list[str] = []
    for rel in files:  # sorted, so a later file (.env.local) overrides an earlier one
        for name in manifest.protected[rel]:
            try:
                record = vault.fetch(rel, name)
            except VaultError as exc:
                raise ExecError(str(exc)) from None
            try:
                value = value_of(decode_record(record)[-1]) if record is not None else None
            except ValueError:
                value = None
            if value is None:
                missing.append(f"{rel}: {name}")
            else:
                stored[name] = value  # duplicate keys: the last line wins

    env = dict(environ)
    injected: list[str] = []
    for name, value in stored.items():
        if name not in env:  # existing environment wins
            env[name] = value
            injected.append(value)
    return Prepared(start, env, [v for v in injected if scrubbable(v)], missing)


def _stdin_is_tty() -> bool:
    try:
        return os.isatty(0)
    except OSError:
        return False


def _pump(source: BinaryIO, sink: BinaryIO, scrubber: StreamScrubber, failures: list[str]) -> None:
    """Copies `source` to `sink` through `scrubber`. If anything goes wrong it
    stops writing (never writes unscrubbed bytes) but keeps draining `source`
    so the child is not blocked on a full pipe."""
    broken = False

    def emit(data: bytes) -> None:
        nonlocal broken
        if not data or broken:
            return
        try:
            sink.write(data)
            sink.flush()
        except (BrokenPipeError, ValueError):
            broken = True

    while True:
        chunk = source.read1(_READ_SIZE)
        if not chunk:
            break
        if broken:
            continue
        try:
            emit(scrubber.feed(chunk))
        except Exception:  # fail closed: no more output rather than unscrubbed output
            broken = True
            failures.append("scrubbing failed")
    if not broken:
        try:
            emit(scrubber.flush())
        except Exception:
            failures.append("scrubbing failed")


def run_command(prepared: Prepared, argv: list[str]) -> int:
    """Runs `argv` with the prepared environment; returns its exit code
    (128+N if a signal N killed it, 127 if not found, 126 if not runnable)."""
    try:
        proc = subprocess.Popen(
            argv,
            env=prepared.env,
            cwd=prepared.cwd,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        click.echo(f"vaultshield exec: command not found: {argv[0]}", err=True)
        return 127
    except OSError as exc:
        click.echo(f"vaultshield exec: cannot run {argv[0]}: {type(exc).__name__}", err=True)
        return 126

    def forward(signum, _frame):
        # In a terminal, Ctrl-C already reaches the child (same process group).
        if signum == signal.SIGINT and _stdin_is_tty():
            return
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    previous: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, forward)
        except ValueError:  # not the main thread
            break

    sys.stdout.flush()
    sys.stderr.flush()
    out_stream = sys.stdout.buffer
    err_stream = sys.stderr.buffer
    failures: list[str] = []
    threads = [
        threading.Thread(
            target=_pump, args=(proc.stdout, out_stream, StreamScrubber(prepared.secret_values), failures)
        ),
        threading.Thread(
            target=_pump, args=(proc.stderr, err_stream, StreamScrubber(prepared.secret_values), failures)
        ),
    ]
    try:
        for thread in threads:
            thread.start()
        code = proc.wait()
        for thread in threads:
            thread.join()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
    if failures:
        click.echo("vaultshield exec: output scrubbing failed; the rest of the output was withheld", err=True)
    return 128 - code if code < 0 else code
