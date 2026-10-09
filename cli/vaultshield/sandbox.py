"""Sandbox_Module: Docker container lifecycle for a single Run.

Implements the `Sandbox` class described in design.md's "Sandbox_Module
(`sandbox.py`)" section (Task 4.1): `Sandbox.create`, `Sandbox.exec`, and
`Sandbox.teardown`, built on the Docker SDK for Python (`docker-py`)
rather than shelling out to the `docker` CLI, per design.md's explicit
tech-stack choice.

Covers Requirements 4.1 (build/reuse an image, clone the repo, mount the
Sandbox_Credential_File), 4.2 (install dependencies before agent execution
starts), 4.3 (teardown removes the Sandbox and its resources), and 4.4 (a
failed `create()` reports its failure reason and never leaves a
partially-created Sandbox running). Requirement 4.5 (`off_limits_paths`
runtime enforcement) belongs to the Monitor (Task 7.7), not this module.

design.md leaves several mechanics deliberately unspecified ("build/reuse",
the exact mount mechanism, dependency-install detection). Each judgment
call made to fill in those gaps is documented at its point of use below;
the short version:

- "Build or reuse `base_image`" is implemented as reuse-if-cached-locally
  (`client.images.get`), falling back to a registry pull
  (`client.images.pull`) on `ImageNotFound`. `create()`'s signature (per
  design.md) takes only a `base_image` tag string, with no Dockerfile or
  build context, so there is nothing to hand `client.images.build(...)`
  a `path`/`fileobj` for; a real custom-Dockerfile build path is out of
  scope until a future task introduces one.
- `env_file` is mounted as a read-only bind mount, configured at
  container-creation time (docker-py has no supported way to attach a new
  bind mount to an already-running container), at a fixed, predictable
  in-container path (`_ENV_MOUNT_PATH`) alongside the cloned repo.
- Dependency install detection is a single `sh -c` one-liner run inside
  the cloned repo directory: `package.json` present -> `npm install`;
  else `requirements.txt` present -> `pip install -r requirements.txt`;
  else a no-op (no recognized manifest is not treated as a failure).

Two additions beyond design.md's reference signatures, both required by
Browser_Capture_Module (`browser_capture.py`, Task 11.1) and both shaped
the way `monitor.py`'s own extensions are (keyword-only, defaulted, so
every existing positional caller keeps working unchanged):

- `create(..., publish_port=...)`: publishes one in-container TCP port to
  an ephemeral host port, so the target application started for a Run is
  reachable from the host. design.md's `start_and_capture` polls
  `ready_path` "through the sandbox's published port" and then drives a
  host-side Playwright browser at the same origin, and Docker publishes a
  port only at container-creation time -- there is no way to add a
  published port to a running container -- so the capability has to live
  here, on `create`.
- `exec_detached`: starts a long-running process (the application itself)
  inside the container without waiting for it to exit, which plain
  `exec`/`exec_run` cannot do because it blocks until the command
  finishes. A detached exec needs no separate shutdown path: `teardown`'s
  `remove(force=True)` SIGKILLs the container along with every process
  exec'd into it.
"""

from __future__ import annotations

import io
import posixpath
import socket
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Sequence
from urllib.parse import urlsplit

import docker
from docker import DockerClient
from docker.models.containers import Container

from vaultshield.providers import ProviderError, parse_remote_url
from vaultshield.providers import github as github_provider
from vaultshield.providers import gitlab as gitlab_provider

if TYPE_CHECKING:
    # `submit` imports `git_capture`, which imports this module, so this import
    # cannot happen at runtime. `from __future__ import annotations` makes every
    # annotation below a string, so the type is still available to a checker.
    from vaultshield.submit import Credential

# Fixed in-container layout. Private to this module: callers never need to
# know these paths themselves, since `Sandbox.exec` already runs inside
# `_REPO_DIR` by default (see its docstring) and `create()` is the only
# method that needs `_ENV_MOUNT_PATH`.
_WORKDIR = "/workspace"
_REPO_DIR = f"{_WORKDIR}/repo"
# Public aliases. `sandbox_monitor` needs to name the agent's working directory
# inside the container (it is what an adapter's `sandbox_invocation` is told, and
# what the file-change watcher walks), and reaching for a private module
# constant from another module would be worse than exporting the two names that
# are genuinely part of the Sandbox's contract.
WORKDIR = _WORKDIR
REPO_DIR = _REPO_DIR
_ENV_MOUNT_PATH = f"{_WORKDIR}/.env.sandbox"


def container_path_under_repo(relative: str) -> str:
    """Resolves a repository-relative subdirectory to its container path.

    `""` returns `_REPO_DIR` unchanged, which is what every caller wanted before
    `project_dir` existed. Anything else is joined onto `_REPO_DIR`, normalised,
    and re-checked to be the repo root or genuinely beneath it.

    The containment check is deliberate duplication. `config._optional_project_dir`
    already rejects absolute paths, `..` segments and Windows separators at load
    time, and that is the check with the good error message; this one is the
    backstop that makes the guarantee a property of the path resolution itself
    rather than of one caller having validated first. The value decides a
    container working directory, and `/workspace` — one level up — holds the
    read-only `.env.sandbox` mount, so "we validated it earlier" is not where a
    security boundary should end.

    Raises:
        SandboxError: `relative` escapes `_REPO_DIR`, or is absolute.
    """
    if not relative or relative.strip() in ("", "."):
        return _REPO_DIR

    candidate = relative.strip()
    if candidate.startswith("/"):
        raise SandboxError(
            f"Refusing an absolute project_dir {candidate!r}: it must be "
            f"relative to {_REPO_DIR}."
        )

    resolved = posixpath.normpath(posixpath.join(_REPO_DIR, candidate))
    if resolved != _REPO_DIR and not resolved.startswith(f"{_REPO_DIR}/"):
        raise SandboxError(
            f"Refusing a project_dir that escapes the repository: {candidate!r} "
            f"resolves to {resolved!r}, which is outside {_REPO_DIR}."
        )
    return resolved

# Keeps the container alive between `create()` and `teardown()` so later
# `exec()` calls have a running process to attach to. `tail -f /dev/null`
# is used instead of `sleep infinity` because `sleep infinity` is a GNU
# coreutils extension that busybox-based images (e.g. Alpine) don't
# support, while `tail -f` is available on both.
_KEEP_ALIVE_CMD = ["tail", "-f", "/dev/null"]
# Public alias for the trusted capture helper (Batch 3 FR5, `trusted_capture`),
# which keeps its own container alive the same way.
KEEP_ALIVE_CMD = _KEEP_ALIVE_CMD

_DEPENDENCY_INSTALL_SCRIPT = (
    "if [ -f package.json ]; then npm install; "
    "elif [ -f requirements.txt ]; then pip install -r requirements.txt; "
    "fi"
)

# Prefix of the Docker name `create()` gives a Run's container. Public,
# unlike the in-container paths above, because a Run's container has to be
# findable by a process that does not hold the `Sandbox` that created it:
# `vaultshield teardown <run-id>` (`teardown.py`) runs in a later process
# than `vaultshield run` and has only the `run_id` to go on.
CONTAINER_NAME_PREFIX = "vaultshield-run-"

# --- private-repository cloning ----------------------------------------------
#
# A private repo cannot be cloned by URL alone, and the obvious fixes are both
# leaks: embedding the token in `repo_url` writes it into the container's git
# config (`.git/config` survives inside the repo the agent then reads), and
# putting it in the container's environment or in `.env.sandbox` hands it to the
# agent directly. Requirement 3.3 keeps every real credential out of the Sandbox,
# and a repository-host token is the most valuable one in the room — it is write
# access to the developer's source.
#
# So the token is supplied the way `submit.py` already supplies it to a push: via
# a one-shot git credential helper that reads it out of the environment of that
# single git process. Concretely, for the clone only:
#
#   * the token is passed as the `environment` of ONE `exec_run` call, which
#     Docker applies to that exec's process alone. It is not part of the
#     container's own environment (set at creation, where we pass none), so no
#     later exec — the agent included — inherits it;
#   * `argv` carries only `-c credential.helper=<shell snippet>`, which names the
#     two environment variables rather than their values, so the token is absent
#     from the exec's command line, from `docker inspect`, and from anything that
#     records the argv;
#   * the process lives exactly as long as `git clone`.
#
# Only http(s) URLs are ever given a credential. A token authenticates HTTP basic
# auth and nothing else, so for `ssh://`, scp-like (`git@host:org/repo`), `git://`
# and local paths there is nothing useful to supply and the URL is used as-is.
_CREDENTIALED_URL_SCHEMES = frozenset({"http", "https"})

# git's own switch for "never ask a human". Spelled out here rather than
# imported from `submit`, where it is private, but set for the same reason: an
# `exec_run` has no tty, so a prompt is invisible and blocks.
_GIT_TERMINAL_PROMPT_ENVVAR = "GIT_TERMINAL_PROMPT"

# Value substituted for the token anywhere text derived from the clone could
# reach a message, an Event_Log record, or a Run_Directory artifact.
_REDACTED = "***"


# Hosts a clone credential may be sent to, per provider. Exact host, or a true
# subdomain of it — the same test `providers.github.default_api_origin` uses to
# tell github.com from a GitHub Enterprise host.
#
# This is deliberately STRICTER than `submit.detect_provider`, which matches the
# product name anywhere in the hostname so that self-managed instances are
# recognised without configuration. That is right for choosing which API to call;
# it is not sufficient for deciding who to hand a token to, because
# `github.com.evil.com` contains "github". Sending a credential is the one
# decision where a near-miss must fail closed, so the provider `detect_provider`
# names is confirmed against this before the token is used.
#
# Consequence, on purpose: a self-managed GitHub Enterprise / GitLab host
# (`github.mycorp.com`) gets no clone credential and is cloned unauthenticated.
# If it is private the clone fails with the message `_clone_repo` builds, rather
# than the token going somewhere this module cannot positively identify.
_CREDENTIAL_HOSTS: dict[str, str] = {
    "github": github_provider.PUBLIC_HOST,
    "gitlab": gitlab_provider.PUBLIC_HOST,
}


def _clone_credential_provider(repo_url: str) -> str | None:
    """The Repository_Host_Provider whose credential can authenticate a clone
    of `repo_url`, or `None` if the URL must not or need not carry a token.

    Delegates the host reading to `submit.detect_provider`, which is already
    the one place that maps a remote URL onto `"github"`/`"gitlab"` (via
    `providers.parse_remote_url`, which drops userinfo and port before looking
    at the host). Re-deriving it here would be a second answer to the same
    question, and the two would drift. Its answer is then confirmed against
    `_CREDENTIAL_HOSTS` — see there for why the confirmation is needed.

    Returns `None` rather than raising whenever a credential does not apply. A
    URL this function does not claim is not an error: it is the existing
    public/unauthenticated path, which must keep working for `git://` daemons,
    local paths, SSH remotes, and self-managed hosts.

    The `submit` import is function-local, not module-level, because
    `submit` -> `git_capture` -> `sandbox` is a real import cycle; this module
    cannot import `submit` at module scope without breaking `import
    vaultshield.sandbox`.
    """
    if urlsplit(repo_url).scheme.lower() not in _CREDENTIALED_URL_SCHEMES:
        return None

    from vaultshield.submit import SubmitError, detect_provider

    try:
        provider = detect_provider(repo_url)
        host = parse_remote_url(repo_url).host.lower()
    except (SubmitError, ProviderError):
        return None

    credential_host = _CREDENTIAL_HOSTS.get(provider)
    if credential_host is None:
        return None
    if host == credential_host or host.endswith(f".{credential_host}"):
        return provider
    return None


def _redact(text: str, secrets: Sequence[str | None]) -> str:
    """`text` with every value in `secrets` replaced by `_REDACTED`.

    Applied to captured git output before it is interpolated into a
    `SandboxError`. git does not normally echo a credential taken from a helper,
    but "normally" is not a guarantee worth resting on when the failure path is
    exactly where a transport error might quote what it was given, and the
    resulting message is written to the Event_Log and the Run_Directory.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def container_name(run_id: str) -> str:
    """Returns the Docker container name for `run_id`:
    `vaultshield-run-<run_id>`.

    Its own function for the same reason `git_capture.run_branch_name` is:
    two components need this exact string for the same Run (`create()` to
    name the container, `teardown.py` to find it again), and only one of
    them should own the spelling — a divergence would make teardown
    silently find nothing.

    `run_id` is not validated here (see below): `create()` accepts whatever id the
    pipeline generated, and the callers that take an id from a command line
    validate it themselves (`run_directory.validate_run_id`) before they
    get this far.
    """
    return f"{CONTAINER_NAME_PREFIX}{run_id}"


# Mutable, unlike the frozen `ExecResult` below: `exit_code` is filled in once
# the output stream has been drained, because that is the first moment the
# daemon can report it.
@dataclass
class InteractiveExec:
    """A live exec whose stdin the caller can write to.

    Returned by `Sandbox.exec_interactive`, and used for exactly one thing: the
    Antigravity sign-in, where the developer has to paste an authorization code
    into a process running inside the container.

    Deliberately separate from `ExecStream` rather than a flag on it. `ExecStream`
    carries the MONITORED agent, and every byte it produces is timestamped into
    the Event_Log; this one carries an authentication exchange whose input must
    never be recorded anywhere. Keeping them apart means the recording path has
    no stdin to accidentally capture, instead of relying on a caller passing the
    right flag.

    ### The echo problem, and why `redact` exists

    `agy` prints nothing at all unless it has a TTY — verified: with `tty=False`
    it produces no consent URL and no paste prompt, because it checks `isatty()`.
    So a TTY is mandatory here. But a container TTY has `ECHO` on, so every byte
    written to stdin comes straight back out in the output stream — measured, not
    assumed: a probe value sent once appeared twice.

    That means displaying this exec's output verbatim would print the developer's
    authorization code on their screen. `read_output(redact=...)` removes it,
    the same way `_redact` strips a git token out of a `SandboxError`. The
    redaction is exact rather than heuristic: the caller knows the literal string
    it sent.
    """

    api: object
    exec_id: str
    socket: object
    tty: bool
    _buffer: bytes = b""
    _secret: str | None = None
    # Set when the read deadline expired instead of the process finishing. The
    # caller reports it; this class does not raise, because the Sandbox still has
    # to be torn down either way.
    timed_out: bool = False

    def send_line(self, text: str) -> None:
        """Writes one line to the process's stdin, arming redaction for it.

        Recording `text` as the value to scrub is done HERE, by the act of
        sending, rather than being a separate argument the caller has to
        remember. The echo and the write are two halves of one event, so the
        mitigation belongs to the write: it is not possible to send a secret
        through this class and forget to redact it afterwards.

        `text` is never logged, never reaches `argv`, and never reaches the
        container environment — it exists only as bytes on this socket.
        """
        self._secret = text
        raw = getattr(self.socket, "_sock", self.socket)
        raw.sendall(text.encode("utf-8") + b"\n")  # type: ignore[union-attr]

    def read_output(self) -> Iterator[str]:
        """Yields complete output lines as they arrive, scrubbed.

        Iterate this EXACTLY ONCE and send from inside the loop. Each call opens
        a fresh `frames_iter` over the same socket, so breaking out and
        re-entering loses the exchange in progress — measured against real `agy`:
        a code written after re-entry never reached it and the sign-in waited out
        its full 60 seconds. The single-pass shape is why `send_line` arms its own
        redaction instead of taking it as an argument here.

        Line-buffered rather than chunk-at-a-time because of the echo: a pasted
        code can be split across two chunks, and scrubbing chunks in isolation
        would miss the halves. Whole lines are assembled first, so the value is
        always complete by the time it is replaced.
        """
        from docker.utils.socket import frames_iter

        try:
            for frame in frames_iter(self.socket, self.tty):
                chunk = frame[1] if isinstance(frame, tuple) else frame
                if not chunk:
                    continue
                self._buffer += chunk
                while b"\n" in self._buffer:
                    line, _, self._buffer = self._buffer.partition(b"\n")
                    yield self._scrubbed(line)
                # A prompt with no trailing newline (`agy`'s "paste the code
                # here:") would otherwise never be shown, so flush a pending tail
                # that already looks like a prompt.
                if self._buffer.rstrip().endswith(b":"):
                    pending, self._buffer = self._buffer, b""
                    yield self._scrubbed(pending)
        except socket.timeout:
            # The process stopped producing output and never exited. Recorded
            # rather than raised so the caller can report a timeout with its own
            # wording and still tear the Sandbox down.
            self.timed_out = True
        except OSError:
            # EOF / the daemon closed the connection. Iteration simply ends: the
            # process exiting IS the normal way this stream finishes, and
            # `wait()` is what says whether it succeeded.
            pass

    def flush_remaining(self) -> str:
        """Whatever is left in the buffer, scrubbed. Called after the stream ends
        so a final partial line is not silently dropped."""
        pending, self._buffer = self._buffer, b""
        return self._scrubbed(pending)

    def _scrubbed(self, raw: bytes) -> str:
        return _scrub(raw.decode("utf-8", errors="replace").rstrip("\r"), self._secret)

    def wait(self) -> int | None:
        """The process's exit code, or `None` if the daemon cannot report it."""
        try:
            return self.api.exec_inspect(self.exec_id).get("ExitCode")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        """Drops the socket. Never raises: this runs on the failure path too."""
        try:
            raw = getattr(self.socket, "_sock", self.socket)
            raw.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _scrub(text: str, secret: str | None) -> str:
    """`text` with `secret` replaced by `_REDACTED`, if there is one to remove."""
    if secret:
        return text.replace(secret, _REDACTED)
    return text


@dataclass
class ExecStream:
    """A live exec inside the container: iterate for output, then read the code.

    Returned by `Sandbox.exec_stream`. Deliberately not a bare generator: the
    caller needs the process's exit code as well as its output, and a generator
    has nowhere to put it. `exit_code` is `None` until the stream is exhausted,
    because the daemon only reports it once the process has actually finished.
    """

    api: object
    exec_id: str
    chunks: object
    exit_code: int | None = None

    def __iter__(self) -> Iterator[bytes]:
        """Yields raw output chunks, then records the exit code.

        Chunks are bytes and are NOT line-aligned — the daemon splits on
        whatever boundary the process happened to write, so a caller assembling
        lines must buffer across chunks.
        """
        for chunk in self.chunks:  # type: ignore[attr-defined]
            if chunk:
                yield chunk
        try:
            self.exit_code = self.api.exec_inspect(self.exec_id).get("ExitCode")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            # The container can be gone by the time the stream drains (a
            # teardown racing a finishing agent). An unknown exit code is
            # reported as `None` rather than masking the output already read.
            self.exit_code = None


@dataclass(frozen=True)
class ExecResult:
    """The result of `Sandbox.exec`.

    design.md references `"ExecResult"` as `Sandbox.exec`'s return type
    without spelling out its fields. This is a VaultShield-specific type
    (distinct from docker-py's own `docker.models.containers.ExecResult`
    namedtuple, which only carries `exit_code`/`output`): it decodes and
    demultiplexes docker-py's raw exec output into the minimal shape a
    caller actually needs — `exit_code` to check success, `stdout`/
    `stderr` as separate decoded text — rather than a single combined
    byte stream.

    `stdout_bytes` is stdout exactly as the process wrote it, before
    `_decode`'s lossy `errors="replace"`. `git_capture` needs it to tell a
    file name that is not valid UTF-8 from one that really contains U+FFFD
    (Requirement 17.1: the security scan must not be blinded silently). It
    defaults to `b""` so hand-built results still construct, and
    `repr=False` keeps large outputs out of error reprs.
    """

    exit_code: int
    stdout: str
    stderr: str
    stdout_bytes: bytes = field(default=b"", repr=False)


class SandboxError(Exception):
    """Raised on any Sandbox creation failure. Guarantees no partially
    created container is left running (caller does not need to clean up).

    Also raised by `Sandbox.exec`/`Sandbox.teardown` when an unexpected
    Docker-level failure occurs (e.g. `docker.errors.APIError`), so a
    caller only ever needs to catch this one exception type from any
    `Sandbox` method — matching design.md's note that Docker SDK
    exceptions (`docker.errors.APIError`, `docker.errors.NotFound`) are
    wrapped rather than left to leak raw subprocess/HTTP errors.
    """


class Sandbox:
    """A single Docker container instance for the duration of one Run.

    `client` is stored privately (`self._client`); design.md's reference
    snippet only calls out `run_id` and `container` as this object's
    attributes, but every method here needs the Docker client to do
    anything, so it has to live somewhere.
    """

    # The container-side paths callers legitimately need to name. `REPO_DIR` in
    # particular is the agent's working directory inside the Sandbox: it is what
    # an adapter's `sandbox_invocation` is handed, what `exec`/`exec_stream`
    # default to, and what the in-container file-change watcher walks.
    WORKDIR = _WORKDIR
    REPO_DIR = _REPO_DIR

    def __init__(self, run_id: str, client: DockerClient):
        self.run_id = run_id
        self.container: Container | None = None
        self._client = client
        # Environment merged UNDER the caller's env in `exec_stream`,
        # `exec_interactive` and `exec_detached` (Batch 3 FR2, D11). Empty by
        # default, so every exec is today's. `egress.EgressGuard.isolate` sets
        # the proxy variables (`HTTPS_PROXY`, `NO_PROXY`, …), which are not
        # secrets. Exec env never persists into the container.
        self.exec_env_defaults: dict[str, str] = {}

    def _merged_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
        """`exec_env_defaults` overlaid with `env`, or `None` when both are
        empty (so an exec without either is byte-identical to before)."""
        merged = {**self.exec_env_defaults, **(env or {})}
        return merged or None

    def create(
        self,
        repo_url: str,
        base_image: str,
        env_file: Path,
        *,
        publish_port: int | None = None,
        resolve_credential: Callable[[str], Credential] | None = None,
        project_dir: str = "",
        network: str | None = None,
        cap_drop: list[str] | None = None,
        sysctls: dict[str, str] | None = None,
        extra_hosts: dict[str, str] | None = None,
    ) -> None:
        """Builds/reuses `base_image`, clones `repo_url` into the container,
        mounts `env_file` at the expected path, installs dependencies.

        On any failure at any step, removes any container that was started
        before re-raising as `SandboxError` (Requirement 4.4).

        Step order is fixed by what docker-py's API actually allows: the
        env-file bind mount can only be configured at container-creation
        time (there's no supported way to attach a new mount to an
        already-running container), so "mount" necessarily happens as
        part of container creation, before the clone/install steps that
        run via `exec_run` against the now-existing container:

            1. Ensure `base_image` is available locally (reuse or pull).
            2. Validate `env_file` exists (see below for why this is
               checked here rather than left to Docker).
            3. Create the container with the env-file bind mount and
               `working_dir` configured, then start it.
            4. Clone `repo_url` into `_REPO_DIR` via `exec_run`.
            5. Install dependencies via `exec_run` (Requirement 4.2).

        `env_file` is checked with `Path.is_file()` before any container
        is created: if the host path doesn't exist, Docker's default bind
        mount behavior is to silently create an empty *directory* there
        instead of erroring, which would produce a confusing in-container
        failure far away from the real cause. Failing fast here, before
        any Docker call, gives a clear failure reason (Requirement 4.4)
        and never creates a container for this case (nothing to clean up).

        The container is named `vaultshield-run-<run_id>` so it's
        identifiable via `docker ps`/future SandboxRecord correlation —
        this is why `run_id` is accepted at all, since design.md's own
        `create()` body never otherwise references it.

        `publish_port` (keyword-only, added for Task 11.1 — see the module
        docstring) publishes that in-container TCP port to an *ephemeral*
        host port, which Docker picks; `browser_capture.resolve_base_url`
        reads the assigned host port back off the container afterwards.
        Ephemeral rather than the same number on the host so two concurrent
        Runs of the same project cannot collide on one host port, and so a
        port already in use on the developer's machine (very often the case
        for a project's own dev-server port) does not fail the Run. Left
        `None` — every caller before Task 11.1 — nothing is published and
        the container's networking is exactly as it was.

        `project_dir` (keyword-only) is the validated `config.project_dir`: the
        repository-relative application root for a repo whose app is nested. It
        affects step 5 ONLY — dependencies are installed there instead of at the
        clone root. Step 4 is deliberately untouched: the clone always lands at
        `_REPO_DIR` whatever `project_dir` says, so `.git` stays where
        `git_capture` expects it and the whole repository is still what gets
        committed and diffed.

        `resolve_credential` (keyword-only) overrides how the repository-host
        credential for step 4 is resolved; it defaults to
        `submit.resolve_git_credential`. Private-repository support is entirely
        inside `_clone_repo` — see that method and the module-level
        "private-repository cloning" note for how the token is kept away from
        both `argv` and the agent.

        `network`, `cap_drop`, `sysctls` and `extra_hosts` (keyword-only, Batch 3
        FR2, D11) are the egress guard's knobs: the per-Run open network the
        clone and install run on, `["NET_RAW"]`, IPv6 off, and the allowlisted
        names mapped to the egress proxy. Each is passed to `containers.create`
        only when not `None`, so with all four left `None` (egress `off`, the
        default) the call is byte-identical to before.

        Any exception raised by our own clone/install steps is already a
        `SandboxError` with a specific reason and is re-raised as-is
        (after cleanup) rather than being wrapped a second time. Any
        other exception — most notably `docker.errors.APIError`/
        `NotFound`/`ImageNotFound` from the image/container/start calls
        themselves — is caught, cleaned up after, and wrapped into a new
        `SandboxError` identifying the failure.
        """
        try:
            if not env_file.is_file():
                raise SandboxError(
                    f"Sandbox creation failed for run {self.run_id}: "
                    f"credential file not found at {env_file}"
                )

            self._ensure_image(base_image)

            egress_kwargs = {
                key: value
                for key, value in (
                    ("network", network),
                    ("cap_drop", cap_drop),
                    ("sysctls", sysctls),
                    ("extra_hosts", extra_hosts),
                )
                if value is not None
            }
            self.container = self._client.containers.create(
                base_image,
                command=_KEEP_ALIVE_CMD,
                name=container_name(self.run_id),
                working_dir=_WORKDIR,
                volumes={
                    str(env_file.resolve()): {
                        "bind": _ENV_MOUNT_PATH,
                        "mode": "ro",
                    }
                },
                # `None` as a `ports` value is docker-py's own spelling of
                # "publish this container port on a host port Docker
                # chooses"; omitting `ports` entirely publishes nothing.
                ports=(
                    {f"{publish_port}/tcp": None} if publish_port is not None else None
                ),
                **egress_kwargs,
            )
            self.container.start()

            self._clone_repo(repo_url, resolve_credential=resolve_credential)
            self._install_dependencies(project_dir)
        except Exception as exc:
            self._cleanup_after_failure()
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(
                f"Sandbox creation failed for run {self.run_id}: {exc}"
            ) from exc

    def exec(self, cmd: list[str], *, env: dict[str, str] | None = None) -> ExecResult:
        """Runs `cmd` inside the existing container, returning its exit
        code and separated stdout/stderr.

        `env` (keyword-only, Batch 3 FR5, D11) is passed to `exec_run` as the
        process environment when given; it is how git capture's fixed
        `_GIT_ENV` reaches git. Omitted, the call is exactly as before.
        `exec_env_defaults` is deliberately NOT merged here: `exec` serves
        VaultShield's own commands, not the agent's.

        Runs with `working_dir=_REPO_DIR` by default: `Sandbox` exists to
        operate on the cloned repository, so callers (git capture, browser
        capture, an agent adapter's own working-directory expectations)
        should not need to know or repeat that path themselves — this is
        a judgment call, since design.md's `exec` signature has no
        `workdir` parameter of its own to make this configurable.

        Raises `SandboxError` if called before `create()` has succeeded
        (`self.container` is `None`), and wraps any unexpected Docker-level
        failure (e.g. `docker.errors.APIError` if the container died) into
        `SandboxError` as well. A *nonzero exit code from `cmd` itself* is
        not treated as a Sandbox failure — that's ordinary information the
        caller inspects via `ExecResult.exit_code`, not an exception.
        """
        if self.container is None:
            raise SandboxError(
                f"Cannot exec in Sandbox for run {self.run_id}: "
                "no container has been created (call create() first, or "
                "it failed/was already torn down)"
            )

        try:
            if env is None:
                exit_code, (stdout, stderr) = self.container.exec_run(
                    cmd, workdir=_REPO_DIR, demux=True
                )
            else:
                exit_code, (stdout, stderr) = self.container.exec_run(
                    cmd, workdir=_REPO_DIR, demux=True, environment=env
                )
        except Exception as exc:
            raise SandboxError(
                f"Sandbox exec failed for run {self.run_id}: {exc}"
            ) from exc

        return ExecResult(
            exit_code=exit_code,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
            stdout_bytes=stdout or b"",
        )

    def exec_detached(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
    ) -> None:
        """Starts `cmd` inside the existing container and returns
        immediately, without waiting for it to exit.

        Added for Task 11.1 (see the module docstring): the target
        application's `start_command` is a server that runs for the rest of
        the Run, so `exec` — which blocks until the command finishes and
        collects its output — cannot start it. Nothing is returned, because
        a detached exec has no exit code or output to report yet: whether
        the application actually came up is observed by polling its
        `ready_path`, not by inspecting this call.

        Defaults to `working_dir=_REPO_DIR`, exactly like `exec`, for the same
        reason. `workdir` overrides it, which is what a nested application root
        needs: a repository whose `package.json` sits in a subdirectory has to
        run its `start_command` there, not at the clone root. `browser_capture`
        passes the resolved `project_dir`; every other caller passes nothing and
        keeps the original behaviour. `exec_stream` already had this parameter —
        this makes the two agree.

        `env` is injected into the started process's environment only (not the
        container's), which is how the port/host variables a framework reads
        (`PORT`, `HOST`) reach it.

        Raises `SandboxError` if called before `create()` has succeeded, or
        if the Docker API call itself fails — same contract as `exec`.

        There is deliberately no matching "stop this process" method. The
        started process's lifetime is the container's: `teardown`'s
        `remove(v=True, force=True)` SIGKILLs the container and every
        process exec'd into it, so a detached application never outlives the
        Sandbox and never blocks teardown.
        """
        if self.container is None:
            raise SandboxError(
                f"Cannot exec in Sandbox for run {self.run_id}: "
                "no container has been created (call create() first, or "
                "it failed/was already torn down)"
            )

        try:
            self.container.exec_run(
                cmd,
                workdir=workdir or _REPO_DIR,
                # `exec_env_defaults` under `env` (Batch 3 D11); `env` alone
                # when there are no defaults, exactly as before.
                environment=self._merged_env(env) if self.exec_env_defaults else env,
                detach=True,
            )
        except Exception as exc:
            raise SandboxError(
                f"Sandbox detached exec failed for run {self.run_id}: {exc}"
            ) from exc

    def exec_stream(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        tty: bool = True,
    ) -> "ExecStream":
        """Starts `cmd` inside the container and streams its output live.

        The execution primitive the Monitor uses to run an agent inside the
        Sandbox. `exec` cannot serve that purpose because it blocks until the
        command exits and only then returns the whole output, so nothing could
        be captured or timestamped while the agent was working; `exec_detached`
        cannot either, because it discards output entirely.

        Args:
            cmd: argv to execute. Not shell-interpreted.
            env: extra environment variables for this process only, merged over
                the container's own. Deliberately a caller-supplied whitelist —
                see `adapters.base.SandboxInvocation` on why the host
                environment must never be copied in here.
            workdir: working directory inside the container; defaults to
                `_REPO_DIR`, matching `exec`/`exec_detached`.
            tty: allocate a pseudo-terminal for the process. Default `True`
                because agent CLIs check `isatty()` and change or disable their
                output when it is false — the Antigravity adapter's design note
                calls this out explicitly. With a TTY the daemon returns one
                interleaved stream rather than separate stdout/stderr, which is
                the correct shape for a terminal transcript anyway.

        Returns:
            An `ExecStream`: iterate it for output chunks, then read
            `.exit_code` once iteration has finished.

        Raises:
            SandboxError: if no container exists, or if the Docker API refuses
                to create or start the exec.

        Uses the low-level `api.exec_create`/`exec_start` pair rather than
        `container.exec_run(stream=True)` because the high-level helper gives no
        way to retrieve the exit code of a streamed exec, and "did the agent
        exit non-zero" is information the Monitor has to have.
        """
        if self.container is None:
            raise SandboxError(
                f"Cannot exec in Sandbox for run {self.run_id}: "
                "no container has been created (call create() first, or "
                "it failed/was already torn down)"
            )

        try:
            api = self._client.api
            handle = api.exec_create(
                self.container.id,
                cmd,
                workdir=workdir or _REPO_DIR,
                # `exec_env_defaults` under `env` (Batch 3 D11).
                environment=self._merged_env(env),
                tty=tty,
                stdout=True,
                stderr=True,
            )
            chunks = api.exec_start(handle["Id"], stream=True, tty=tty, demux=False)
        except Exception as exc:
            raise SandboxError(
                f"Sandbox streaming exec failed for run {self.run_id}: {exc}"
            ) from exc

        return ExecStream(api=api, exec_id=handle["Id"], chunks=chunks)

    def exec_interactive(
        self,
        cmd: list[str],
        *,
        workdir: str | None = None,
        timeout_s: float | None = None,
    ) -> "InteractiveExec":
        """Starts `cmd` with its stdin open, for an exchange a human takes part in.

        Added for `--interactive-auth`: `agy` authenticates by printing a Google
        consent URL and then waiting for an authorization code on stdin, so that
        one exec needs a writable stdin. Nothing else in the pipeline does.

        Separate from `exec_stream` on purpose, and the separation is the security
        property rather than a style choice:

        * `exec_stream` runs the MONITORED agent, and `sandbox_monitor` turns
          everything it yields into Event_Log records. It has no stdin, so there
          is no input for the recording path to capture even by mistake.
        * this method has stdin, and no caller of it writes to the Event_Log.

        A TTY is always allocated, because `agy` prints neither the consent URL
        nor the paste prompt without one. The consequence is that stdin is echoed
        back into the output stream, so callers displaying that output MUST pass
        `redact=` to `InteractiveExec.read_output` — see that class.

        `env` is deliberately NOT a parameter. The authorization code and the
        session it produces must not travel through the environment (which
        `docker inspect` can surface for the exec), and no other value needs to.

        `timeout_s` bounds each read, so a container process that stops producing
        output and never exits cannot hang the CLI forever. Expiry sets
        `InteractiveExec.timed_out` rather than raising, so the caller reports it
        and the Sandbox is still torn down.

        Raises:
            SandboxError: no container exists, or the Docker API refused the exec.
        """
        if self.container is None:
            raise SandboxError(
                f"Cannot exec in Sandbox for run {self.run_id}: "
                "no container has been created (call create() first, or "
                "it failed/was already torn down)"
            )

        try:
            api = self._client.api
            handle = api.exec_create(
                self.container.id,
                cmd,
                workdir=workdir or _REPO_DIR,
                # No caller env: see the docstring. Only `exec_env_defaults`
                # (the egress proxy variables, Batch 3 D11, never a secret), and
                # nothing at all when there are none.
                environment=self._merged_env(None),
                tty=True,
                stdin=True,
                stdout=True,
                stderr=True,
            )
            stream = api.exec_start(handle["Id"], socket=True, tty=True, demux=False)
            if timeout_s is not None:
                raw = getattr(stream, "_sock", stream)
                raw.settimeout(timeout_s)
        except Exception as exc:
            raise SandboxError(
                f"Sandbox interactive exec failed for run {self.run_id}: {exc}"
            ) from exc

        return InteractiveExec(
            api=api, exec_id=handle["Id"], socket=stream, tty=True
        )

    def write_file(self, container_path: str, content: str) -> None:
        """Writes `content` to `container_path` inside the container.

        Used to place the Monitor's in-container file-change watcher. A tar
        stream through `put_archive` is the only way to create a file in a
        container without going through a shell, which matters because the
        content is a Python program: passing it as a `sh -c` argument would
        make its quoting and newlines the shell's problem.

        Raises `SandboxError` if no container exists or the upload fails.
        """
        if self.container is None:
            raise SandboxError(
                f"Cannot write into Sandbox for run {self.run_id}: "
                "no container has been created"
            )

        payload = content.encode("utf-8")
        directory, _, name = container_path.rpartition("/")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(payload))
        archive.seek(0)

        try:
            self.container.put_archive(directory or "/", archive.getvalue())
        except Exception as exc:
            raise SandboxError(
                f"Sandbox file write failed for run {self.run_id} "
                f"({container_path}): {exc}"
            ) from exc

    def teardown(self) -> None:
        """Force-stops and removes the container and any anonymous volumes.

        Idempotent: safe to call on an already-torn-down Sandbox. Calling
        it when `self.container` is `None`, or when the container no
        longer exists in Docker (`docker.errors.NotFound`), is a safe
        no-op and never raises. `remove(v=True, force=True)` alone covers
        both "force-stop" and "remove" in a single call — `force=True`
        already SIGKILLs a still-running container as part of removal, so
        a separate, slower `stop()` (which waits out a grace period) call
        first would be redundant.

        Any *other* unexpected Docker-level error (e.g. the daemon itself
        being unreachable) is allowed to propagate as-is rather than
        being silently swallowed: idempotency is specifically about
        tolerating "there's nothing left to tear down" states, not about
        hiding a genuinely broken Docker environment from the caller.

        After a successful teardown (including the already-gone case),
        `self.container` is reset to `None` so a second call trivially
        hits the no-op branch above.
        """
        if self.container is None:
            return

        try:
            self.container.remove(v=True, force=True)
        except docker.errors.NotFound:
            pass
        finally:
            self.container = None

    def _ensure_image(self, base_image: str) -> None:
        """Reuses `base_image` if already present locally; otherwise pulls
        it from the registry. See the module docstring for why an actual
        `images.build(...)` call isn't applicable here.
        """
        try:
            self._client.images.get(base_image)
        except docker.errors.ImageNotFound:
            self._client.images.pull(base_image)

    def _clone_repo(
        self,
        repo_url: str,
        *,
        resolve_credential: Callable[[str], Credential] | None = None,
    ) -> None:
        """Clones `repo_url` into `_REPO_DIR` inside the container,
        authenticating to the repository host when a local credential is
        available and the URL needs one.

        Two paths, chosen by whether a usable token was resolved:

        1. **Authenticated.** For an http(s) URL on a recognised host with a
           `SOURCE_FILE` credential, git is invoked with the same one-shot
           credential helper `submit.git_credential_args` builds for a push, and
           the token is passed as this single exec's environment. See the
           "private-repository cloning" note at the top of this module for why
           that is the form chosen over a token in `repo_url`.
        2. **Unauthenticated.** Everything else: the original `git clone
           <repo_url> <dir>`, byte for byte, so public repos, `git://` remotes,
           local paths and SSH URLs behave exactly as before.

        `GIT_TERMINAL_PROMPT=0` is set on both paths. `exec_run` gives git no
        tty, so a git that decides to ask for a username has nothing to ask and
        the clone fails on a timeout or an opaque exit code — which is what a
        private clone with no credential did before this. With the prompt
        disabled it fails immediately with git's own authentication message.

        A `SOURCE_CLI` credential is deliberately NOT usable here. It carries no
        token by design (`gh` holds the secret and VaultShield does not), and the
        transport credentials it stands for — the developer's SSH key, or the
        helper `gh auth setup-git` installs — exist on the host and not in the
        container. Rather than silently produce an unauthenticated clone that
        fails with git's generic message, it is recorded as the reason and
        surfaced if the clone does fail.

        `resolve_credential` is a keyword-only testability seam, defaulting to
        `submit.resolve_git_credential`; see that module's "Testability seams".

        Raises:
            SandboxError: the clone failed. When a credential could not be
                resolved for a host that looked like it needed one, the message
                says so and how to fix it, instead of only quoting git. Never
                contains the token.
        """
        provider = _clone_credential_provider(repo_url)
        credential, unavailable_reason = self._clone_credential(
            provider, resolve_credential
        )

        if credential is not None:
            from vaultshield.submit import git_credential_args

            assert provider is not None  # implied by credential is not None
            config_args, env = git_credential_args(credential, provider)
            self._exec_or_raise(
                # `config_args` names the two environment variables the helper
                # reads; the token itself is only ever in `env`.
                ["git", *config_args, "clone", repo_url, _REPO_DIR],
                workdir=_WORKDIR,
                step="git clone",
                environment=env,
                secrets=(credential.token,),
            )
            return

        try:
            self._exec_or_raise(
                ["git", "clone", repo_url, _REPO_DIR],
                workdir=_WORKDIR,
                step="git clone",
                environment={_GIT_TERMINAL_PROMPT_ENVVAR: "0"},
            )
        except SandboxError as exc:
            if unavailable_reason is None:
                raise
            raise SandboxError(
                f"Sandbox creation failed for run {self.run_id} during git "
                f"clone: {repo_url} could not be cloned, and no usable "
                f"{provider} credential was available to authenticate it. If "
                f"the repository is private, {unavailable_reason} "
                f"Original git failure: {exc}"
            ) from exc

    def _clone_credential(
        self,
        provider: str | None,
        resolve_credential: Callable[[str], Credential] | None,
    ) -> tuple[Credential | None, str | None]:
        """Resolves the clone credential for `provider`.

        Returns `(credential, unavailable_reason)` with exactly one side set:
        a credential carrying a token, or a sentence explaining what to do
        about its absence. `(None, None)` means no credential was needed —
        `provider` was `None`, so the URL takes no host token and its clone is
        not a credential question at all.

        A failure to resolve is never raised from here. The repository may
        simply be public, in which case an unauthenticated clone is correct and
        a credential error would be a false alarm; the reason is carried
        forward instead, and only surfaces if the clone actually fails.
        """
        if provider is None:
            return None, None

        from vaultshield.submit import CredentialError, resolve_git_credential

        resolver = resolve_credential or resolve_git_credential
        try:
            resolved = resolver(provider)
        except CredentialError as exc:
            # `str(exc)` is `resolve_git_credential`'s own remediation text
            # (run `gh auth login`, or add a token to credentials.yml and
            # chmod it). It names paths and commands, never a token.
            return None, str(exc)

        if getattr(resolved, "token", None):
            return resolved, None

        cli = getattr(resolved, "executable", None) or f"the {provider} CLI"
        return None, (
            f"note that the authenticated {cli} cannot be used from inside the "
            f"Sandbox: it holds the token on the host, and the container has "
            f"neither it nor your SSH key. Add a `{provider}:` entry with a "
            f"`token:` value to ~/.vaultshield/credentials.yml and `chmod 600` "
            f"it, so the clone can be authenticated without exposing your CLI "
            f"session to the container."
        )

    def _install_dependencies(self, project_dir: str = "") -> None:
        """Detects a `package.json`/`requirements.txt` and runs the
        corresponding install command (Requirement 4.2).

        Detection and install happen in one `exec_run` call (a single
        `sh -c` one-liner) rather than a separate detect-then-install
        round trip, since both need to observe the same directory
        listing and this keeps the MVP implementation to one step. A repo
        with neither manifest is not an error: the script simply does
        nothing and exits 0.

        `project_dir` moves both the detection and the install into the
        application root for a repository whose app is nested. That matters in
        both directions: at the clone root a nested repo has no manifest at all,
        so dependencies were silently never installed and the application then
        failed to start for a reason several steps removed from the cause.

        Dependencies are always installed INSIDE the container. No host
        `node_modules` is mounted or consulted — there is no host mount of any
        kind — so what gets installed is whatever the cloned manifests specify.
        """
        self._exec_or_raise(
            ["sh", "-c", _DEPENDENCY_INSTALL_SCRIPT],
            workdir=container_path_under_repo(project_dir),
            step="dependency install",
        )

    def _exec_or_raise(
        self,
        cmd: list[str],
        *,
        workdir: str,
        step: str,
        environment: dict[str, str] | None = None,
        secrets: Sequence[str | None] = (),
    ) -> None:
        """Runs `cmd` inside `self.container` and raises `SandboxError`
        naming `step` and the exit code/captured output if it exits
        non-zero (Requirement 4.4: report the failure reason). A raw
        Docker-level exception raised by `exec_run` itself is left to
        propagate uncaught here, so `create()`'s outer `except Exception`
        can wrap it in its own, container-creation-specific message.

        `environment` is applied to this exec's process only. Docker scopes an
        exec's environment to that exec, so a value passed here is not added to
        the container and is not inherited by any later `exec`/`exec_stream` —
        which is what lets `_clone_repo` hand git a token the agent cannot read.

        `secrets` are removed from the captured output before it reaches the
        exception, so a credential cannot travel out through the error path.
        """
        exit_code, (stdout, stderr) = self.container.exec_run(
            cmd, workdir=workdir, demux=True, environment=environment
        )
        if exit_code != 0:
            detail = _decode(stderr) or _decode(stdout) or "no output captured"
            raise SandboxError(
                f"Sandbox creation failed for run {self.run_id} during "
                f"{step} (exit code {exit_code}): "
                f"{_redact(detail, secrets).strip()}"
            )

    def _cleanup_after_failure(self) -> None:
        """Best-effort removal of a partially-created container after a
        `create()` failure (Requirement 4.4). Any error raised while
        cleaning up is deliberately swallowed: the original failure is
        always what gets reported to the caller, never a secondary
        cleanup error masking it.
        """
        if self.container is None:
            return
        try:
            self.container.remove(v=True, force=True)
        except Exception:
            pass
        finally:
            self.container = None


def _decode(chunk: bytes | None) -> str:
    """Decodes one half of a demultiplexed `exec_run` result. `demux=True`
    returns `None` for a stream that produced no output at all, which is
    not the same as an empty-but-present stream; both are normalized to
    `""` here since `ExecResult`'s callers only care about the text.
    """
    if not chunk:
        return ""
    return chunk.decode("utf-8", errors="replace")
