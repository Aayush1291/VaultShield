"""Config_Module: framework detection and `.vaultshield.yml` handling.

Implements both halves of the Config_Module described in design.md:
- `detect_framework` / `write_config` — the `init`-time half (Task 1.2).
- `ProjectConfig` / `ConfigError` / `load_config` — the
  `run`/`submit`/`teardown`-time half (Task 1.4).

`detect_framework` is invoked ONLY from the `init` command (see
`vaultshield.cli`) — never from `run`/`load_config`, per Requirement 1.4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Fields written into every `.vaultshield.yml`, detected or not.
_DETECTED_FIELDS = ("start_command", "port", "ready_path")

# Fields `load_config` requires to be present AND non-empty. Matches
# _DETECTED_FIELDS above (the same three fields `detect_framework` fills
# in, or leaves as "" when nothing was detected) — kept as a separate name
# because it plays a different role here: `load_config` treats a "" value
# for any of these the same as an absent key (Requirement 2.2), which is
# exactly what lets an un-filled-in `init` output get rejected by `run`
# with a clear error instead of silently proceeding with blank values.
REQUIRED_FIELDS = ("start_command", "port", "ready_path")


@dataclass(frozen=True)
class ProjectConfig:
    """A validated `.vaultshield.yml`, as returned by `load_config`.

    Mirrors design.md's dataclass exactly. `port` is typed `int`, but
    `load_config` does not coerce or type-check it beyond the
    presence/non-empty check applied to every `REQUIRED_FIELDS` entry:
    Requirement 2 only specifies "missing field" and "malformed YAML"
    rejection (Requirements 2.2, 2.3), with no requirement or Correctness
    Property covering a present-but-wrong-type value, so this field simply
    carries through whatever `yaml.safe_load` produced for it.
    """

    start_command: str
    port: int
    ready_path: str
    agent: str
    routes: list[str]
    off_limits_paths: list[str]


class ConfigError(Exception):
    """Raised by `load_config` when a `.vaultshield.yml` can't be turned
    into a valid `ProjectConfig`: a missing/unreadable file, malformed
    YAML, a YAML document that doesn't parse to a mapping, or one or more
    `REQUIRED_FIELDS` missing/empty. Every message names the config file
    path plus the specific problem, per Requirements 2.2 and 2.3.
    """


def detect_framework(repo_root: Path) -> dict:
    """Inspect package.json / manage.py / Procfile / docker-compose.yml.

    Returns a dict with `start_command`/`port`/`ready_path` (possibly empty
    strings if no signature matched). Invoked ONLY from `init`, never from
    `run`.

    Detection precedence (design.md does not specify an order when multiple
    signals are present, so it is documented here): the first matching
    signature wins and detection stops there.

        1. manage.py           - a single, highly specific filename with no
                                  ambiguity (Django).
        2. package.json        - also a specific, common signature (Node.js).
        3. Procfile             - only consulted if neither of the above
                                  matched. A Procfile is often *paired* with
                                  a Node/Django app rather than being the
                                  primary signature, and its `web:` line is
                                  more of a fallback/override hint.
        4. docker-compose.yml   - the broadest, least specific signal (a
                                  compose file describes how services are
                                  *composed*, not necessarily how to start
                                  one standalone inside our own Sandbox), so
                                  it is checked last.
    """
    for detector in (
        _detect_django,
        _detect_node,
        _detect_procfile,
        _detect_docker_compose,
    ):
        result = detector(repo_root)
        if result is not None:
            return result
    return {"start_command": "", "port": "", "ready_path": ""}


def write_config(repo_root: Path, detected: dict) -> Path:
    """Writes `.vaultshield.yml` with detected fields plus placeholder
    `agent`/`routes`/`off_limits_paths`. Returns the written path.

    `detected` is expected to be the dict shape returned by
    `detect_framework` (keys `start_command`, `port`, `ready_path`); missing
    keys default to `""` so this also tolerates a partially-populated dict.

    The placeholder fields (`agent` as an empty string, `routes` and
    `off_limits_paths` as empty lists) are always written, regardless of
    whether detection succeeded — the developer fills these in by hand
    later, and `off_limits_paths` is only ever populated manually (there is
    no auto-detection story for it).
    """
    config: dict[str, Any] = {
        "start_command": detected.get("start_command", ""),
        "port": detected.get("port", ""),
        "ready_path": detected.get("ready_path", ""),
        "agent": "",
        "routes": [],
        "off_limits_paths": [],
    }
    path = repo_root / ".vaultshield.yml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def load_config(repo_root: Path) -> ProjectConfig:
    """Parses `.vaultshield.yml`, validates required fields are present and
    non-empty, and returns a `ProjectConfig`. Raises `ConfigError` with the
    specific missing field names, or a parse-error message identifying the
    file, on failure.

    Called from `run`/`submit`/`teardown` — NEVER re-runs `detect_framework`
    (Requirement 1.4). A missing config file is treated the same as a
    parse error: both are "can't get a usable document out of this path"
    failures, and `ConfigError` is the single failure type callers need to
    handle either way.

    design.md's reference implementation assumes `yaml.safe_load` always
    returns something with a `.get()` method, which isn't true for an
    empty file (`None`) or a YAML document whose root is a list/scalar
    instead of a mapping. Both are still "malformed" from the caller's
    perspective (Requirement 2.3 talks about rejecting a bad config file,
    not just a YAML syntax error), so they're handled here as parse-error
    `ConfigError`s rather than left to crash with an `AttributeError`.
    """
    path = repo_root / ".vaultshield.yml"
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Malformed YAML in {path}: expected a mapping at the document root")

    missing = [field for field in REQUIRED_FIELDS if not raw.get(field)]
    if missing:
        raise ConfigError(f"Missing required field(s) in {path}: {', '.join(missing)}")

    return ProjectConfig(
        start_command=_require_text(raw, "start_command", path),
        port=_require_port(raw, "port", path),
        ready_path=_require_text(raw, "ready_path", path),
        agent=_optional_text(raw, "agent", path),
        routes=_require_string_list(raw, "routes", path),
        off_limits_paths=_require_string_list(raw, "off_limits_paths", path),
    )


# --- typed field validation -------------------------------------------------
#
# The presence/non-empty check above is necessary but not sufficient: YAML types
# a value for you, so a field can be present and non-empty and still be the
# wrong type entirely. Without these checks a wrong type was accepted here and
# only failed much later, or — worse — did not fail at all:
#
#   port: not-a-number        reached Docker and surfaced as an opaque
#                             `400 Client Error` about a malformed port binding,
#                             after a container had already been created.
#   start_command: 12345      an int reached the Sandbox as a command.
#   ready_path: [/health]     a list was used where a URL path was expected.
#   routes: /home             a bare string is *iterable*, so this was silently
#                             treated as the routes ['/','h','o','m','e'].
#   off_limits_paths: p.txt   same character-by-character iteration, which meant
#                             `p.txt` was NOT protected and thirteen
#                             single-character paths were. A security control
#                             that silently protects nothing is worse than one
#                             that refuses to start.
#
# Every check therefore reports the field, what it got, and what was expected,
# and every one runs inside `load_config` — which `run` calls as step 1, before
# the Run_Directory exists, before credentials are generated, and before any
# container is created.


def _type_error(path: Path, field: str, value: object, expected: str) -> ConfigError:
    """A uniform message: which field, what arrived, what was wanted."""
    return ConfigError(
        f"Invalid `{field}` in {path}: expected {expected}, got "
        f"{type(value).__name__} ({value!r})"
    )


def _require_text(raw: dict, field: str, path: Path) -> str:
    """A required non-empty string.

    `bool` is rejected explicitly: YAML's `yes`/`no`/`true`/`false` parse to
    Python bools, and a bool is not a string, but a bare `isinstance(value, str)`
    check would already catch that — what needs saying is that the message names
    the field rather than letting a bool reach a command line.
    """
    value = raw.get(field)
    if not isinstance(value, str):
        raise _type_error(path, field, value, "a string")
    if not value.strip():
        raise ConfigError(f"Invalid `{field}` in {path}: expected a non-empty string")
    return value


def _optional_text(raw: dict, field: str, path: Path) -> str:
    """An optional string, defaulting to `""` when absent.

    `agent` is written as an empty placeholder by `init`, so absent and empty
    are both normal; a wrong *type* still is not.
    """
    if field not in raw or raw[field] is None:
        return ""
    value = raw[field]
    if not isinstance(value, str):
        raise _type_error(path, field, value, "a string")
    return value


def _require_port(raw: dict, field: str, path: Path) -> int:
    """A required TCP port: an integer in 1-65535.

    Accepts a string of digits (`port: "3000"`, which YAML keeps as a string
    when quoted) because that is a reasonable thing to write and the intent is
    unambiguous. It does not accept arbitrary strings, floats, or bools —
    `port: not-a-number` used to pass this function's predecessor and fail
    inside Docker, and `True` would otherwise sneak through as the int 1.
    """
    value = raw.get(field)
    if isinstance(value, bool):
        raise _type_error(path, field, value, "an integer TCP port")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        raise _type_error(path, field, value, "an integer TCP port")
    if not 1 <= port <= 65535:
        raise ConfigError(
            f"Invalid `{field}` in {path}: expected a TCP port in 1-65535, got {port}"
        )
    return port


def _require_string_list(raw: dict, field: str, path: Path) -> list[str]:
    """An optional list of non-empty strings, defaulting to `[]` when absent.

    A bare string is rejected rather than wrapped in a single-element list. That
    is the one case where being permissive would be actively dangerous: a string
    is iterable, so `off_limits_paths: protected.txt` would otherwise be consumed
    one character at a time and protect nothing while appearing to work. Refusing
    it means the developer is told to write a list, and the security control
    either does what it says or does not start.
    """
    if field not in raw or raw[field] is None:
        return []
    value = raw[field]
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _type_error(
            path, field, value, "a list of strings (a bare string is not accepted)"
        )
    entries: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            raise _type_error(
                path, f"{field}[{index}]", entry, "a string"
            )
        if not entry.strip():
            raise ConfigError(
                f"Invalid `{field}[{index}]` in {path}: expected a non-empty string"
            )
        entries.append(entry)
    return entries


def _detect_django(repo_root: Path) -> dict | None:
    """manage.py signature (Django)."""
    if not (repo_root / "manage.py").is_file():
        return None
    return {
        "start_command": "python manage.py runserver 0.0.0.0:8000",
        "port": 8000,
        "ready_path": "/",
    }


def _detect_node(repo_root: Path) -> dict | None:
    """package.json signature (Node.js).

    package.json's mere presence is the Requirement-1.1 signal; we then try
    to pick a sensible `start_command` from its `scripts` map:
        - an explicit "start" script -> "npm start" (npm resolves it, so we
          use the invocation command rather than the script's own content)
        - else an explicit "dev" script -> "npm run dev"
        - else "npm start" as a conventional fallback guess, so a detected
          Node project never ends up with a blank start_command; the
          developer can correct it by hand if it doesn't apply.

    A malformed package.json does not prevent detection (the file's
    presence is still a valid Node signal) — we just skip script
    inspection and fall back to the same conventional defaults.
    """
    path = repo_root / "package.json"
    if not path.is_file():
        return None

    start_command = "npm start"
    try:
        data = json.loads(path.read_text())
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            if "start" in scripts:
                start_command = "npm start"
            elif "dev" in scripts:
                start_command = "npm run dev"
    except (json.JSONDecodeError, OSError):
        pass

    return {"start_command": start_command, "port": 3000, "ready_path": "/"}


def _detect_procfile(repo_root: Path) -> dict | None:
    """Procfile signature: parses the `web:` process line if present.

    A Procfile conventionally doesn't declare a port itself (hosting
    platforms like Heroku inject `$PORT` at runtime), so we fall back to
    the same common default port (3000) and root `ready_path` used for the
    Node heuristic above; the developer can correct these manually.

    If no `web:` line is present, there is nothing actionable to extract,
    so this is not treated as a match.
    """
    path = repo_root / "Procfile"
    if not path.is_file():
        return None

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("web:"):
            command = stripped.split(":", 1)[1].strip()
            if command:
                return {"start_command": command, "port": 3000, "ready_path": "/"}
    return None


def _detect_docker_compose(repo_root: Path) -> dict | None:
    """docker-compose.yml signature.

    design.md flags docker-compose.yml as a signal to inspect per
    Requirement 1.1 but doesn't specify how to derive start_command/port
    from it. Heuristic used here, documented since design.md leaves this
    underspecified: look for the first service (in file order) that
    defines BOTH a `ports` mapping and a `command`, and use that service's
    first published host port plus its `command` verbatim as
    `start_command`.

    We deliberately require both pieces of information before claiming a
    match: a compose file describes how a service is *composed* alongside
    others, not necessarily how to start it standalone inside our own
    Sandbox (a separate container built by Sandbox_Module, not by
    docker-compose itself). If we can't extract a usable command, we
    prefer falling through to the "nothing detected" case rather than
    guessing a command that likely won't run in that context.
    """
    path = repo_root / "docker-compose.yml"
    if not path.is_file():
        return None

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None
    services = data.get("services")
    if not isinstance(services, dict):
        return None

    for service in services.values():
        if not isinstance(service, dict):
            continue
        command = service.get("command")
        ports = service.get("ports")
        if not command or not isinstance(ports, list) or not ports:
            continue
        host_port = _parse_compose_host_port(ports[0])
        if host_port is None:
            continue
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return {
            "start_command": str(command),
            "port": host_port,
            "ready_path": "/",
        }
    return None


def _parse_compose_host_port(port_entry: Any) -> int | None:
    """Extracts the host-side port from one docker-compose `ports` entry.

    Supports the common short syntax (`"8080:80"`, `"8080"`, or a bare
    int) and the long syntax (`{"published": 8080, "target": 80}`).
    Returns `None` if a host port can't be confidently parsed.
    """
    if isinstance(port_entry, bool):
        # bool is a subclass of int in Python; explicitly exclude it.
        return None
    if isinstance(port_entry, int):
        return port_entry
    if isinstance(port_entry, str):
        head = port_entry.split(":")[0]
        try:
            return int(head)
        except ValueError:
            return None
    if isinstance(port_entry, dict):
        published = port_entry.get("published")
        try:
            return int(published)
        except (TypeError, ValueError):
            return None
    return None
