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
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import docker
from docker import DockerClient
from docker.models.containers import Container

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

# Keeps the container alive between `create()` and `teardown()` so later
# `exec()` calls have a running process to attach to. `tail -f /dev/null`
# is used instead of `sleep infinity` because `sleep infinity` is a GNU
# coreutils extension that busybox-based images (e.g. Alpine) don't
# support, while `tail -f` is available on both.
_KEEP_ALIVE_CMD = ["tail", "-f", "/dev/null"]

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
    """

    exit_code: int
    stdout: str
    stderr: str


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

    def create(
        self,
        repo_url: str,
        base_image: str,
        env_file: Path,
        *,
        publish_port: int | None = None,
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
            )
            self.container.start()

            self._clone_repo(repo_url)
            self._install_dependencies()
        except Exception as exc:
            self._cleanup_after_failure()
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(
                f"Sandbox creation failed for run {self.run_id}: {exc}"
            ) from exc

    def exec(self, cmd: list[str]) -> ExecResult:
        """Runs `cmd` inside the existing container, returning its exit
        code and separated stdout/stderr.

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
            exit_code, (stdout, stderr) = self.container.exec_run(
                cmd, workdir=_REPO_DIR, demux=True
            )
        except Exception as exc:
            raise SandboxError(
                f"Sandbox exec failed for run {self.run_id}: {exc}"
            ) from exc

        return ExecResult(
            exit_code=exit_code,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
        )

    def exec_detached(
        self, cmd: list[str], *, env: dict[str, str] | None = None
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

        Runs with `working_dir=_REPO_DIR`, exactly like `exec`, for the same
        reason. `env` is injected into the started process's environment
        only (not the container's), which is how the port/host variables a
        framework reads (`PORT`, `HOST`) reach it.

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
                cmd, workdir=_REPO_DIR, environment=env, detach=True
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
                environment=env or None,
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

    def _clone_repo(self, repo_url: str) -> None:
        """Clones `repo_url` into `_REPO_DIR` inside the container.

        Authentication for private repositories is out of scope for this
        task: `repo_url` is used as-is, so a private repo must already be
        clonable by whatever URL form is passed in (e.g. one that embeds
        a credential), or must be public.
        """
        self._exec_or_raise(
            ["git", "clone", repo_url, _REPO_DIR],
            workdir=_WORKDIR,
            step="git clone",
        )

    def _install_dependencies(self) -> None:
        """Detects a `package.json`/`requirements.txt` in the cloned repo
        and runs the corresponding install command (Requirement 4.2).

        Detection and install happen in one `exec_run` call (a single
        `sh -c` one-liner) rather than a separate detect-then-install
        round trip, since both need to observe the same directory
        listing and this keeps the MVP implementation to one step. A repo
        with neither manifest is not an error: the script simply does
        nothing and exits 0.
        """
        self._exec_or_raise(
            ["sh", "-c", _DEPENDENCY_INSTALL_SCRIPT],
            workdir=_REPO_DIR,
            step="dependency install",
        )

    def _exec_or_raise(self, cmd: list[str], *, workdir: str, step: str) -> None:
        """Runs `cmd` inside `self.container` and raises `SandboxError`
        naming `step` and the exit code/captured output if it exits
        non-zero (Requirement 4.4: report the failure reason). A raw
        Docker-level exception raised by `exec_run` itself is left to
        propagate uncaught here, so `create()`'s outer `except Exception`
        can wrap it in its own, container-creation-specific message.
        """
        exit_code, (stdout, stderr) = self.container.exec_run(
            cmd, workdir=workdir, demux=True
        )
        if exit_code != 0:
            detail = _decode(stderr) or _decode(stdout) or "no output captured"
            raise SandboxError(
                f"Sandbox creation failed for run {self.run_id} during "
                f"{step} (exit code {exit_code}): {detail.strip()}"
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
