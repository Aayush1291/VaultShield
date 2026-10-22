"""What the orchestrator knows about a project, and the checks that keep secrets out of it.

HARD SAFETY RULES (each is tested):

1. The content of a file that `vaultshield` discovery flags as secret, or that is in
   the protect manifest, is never read. Only fixed, named sources are read: the README,
   package.json, requirements.txt, pyproject.toml, and the files framework detection
   looks at. Symlinks are never followed.
2. No env value is ever included. Protected variables appear by NAME only.
3. Every piece of text then goes through `sanitize`, which replaces (a) every value
   stored in the vault for this project and (b) anything shaped like a secret, with ***.
   Truncation happens AFTER sanitizing, so a key cut in half by a size limit cannot
   leave a fragment behind.
4. `assert_clean` re-checks any text about to be sent and raises if anything slipped
   through. The flow calls it before every model request.

What is NOT covered is listed in README.md: a secret in an unusual shape inside README
text, a secret value shorter than the `exec` redaction threshold, and values in forms
the vault does not hold.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from vaultshield.config import detect_framework
from vaultshield.protect import manifest as manifest_mod
from vaultshield.protect.discovery import discover
from vaultshield.protect.envfile import decode_record, value_of
from vaultshield.protect.execute import scrubbable
from vaultshield.protect.vault import Vault, VaultError
from vaultshield.token_hygiene import SecretSet

REDACTED = "***"

TREE_MAX_DEPTH = 3
TREE_MAX_ENTRIES = 200
# `.vaultshield` is VaultShield's own folder (runs, manifest, saved prompts), not project structure.
TREE_SKIP = frozenset({"node_modules", ".git", ".venv", "__pycache__", "dist", "build", ".vaultshield"})
README_MAX_CHARS = 4000
README_READ_BYTES = 32 * 1024
README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme.markdown", "readme")
DEP_FILE_MAX_BYTES = 256 * 1024
DEPS_PER_LIST = 80
COMMIT_COUNT = 10
COMMIT_SUBJECT_MAX_CHARS = 200
GIT_TIMEOUT_S = 10

# Files `detect_framework` opens. If any is a symlink or flagged secret, detection is skipped.
_FRAMEWORK_FILES = ("manage.py", "package.json", "Procfile", "docker-compose.yml")


class ContextError(Exception):
    """The project could not be read safely. Nothing has been sent. No secret in the message."""


class LeakError(Exception):
    """`assert_clean` found something that must not be sent. The message counts; it never shows it."""


# --- secrets -------------------------------------------------------------------


class Secrets:
    """The values that must never be sent, in memory only. Its text form shows counts."""

    def __init__(self, values: Iterable[str] = (), always: Iterable[str] = ()) -> None:
        """`values` are project values; those too short or too plain to redact safely
        (the same rule `vaultshield exec` uses) are left out. `always` are redacted
        whatever they look like, for example the model API key."""
        self._set = SecretSet()
        self._count = 0
        seen: set[str] = set()
        for value in values:
            if value not in seen and scrubbable(value):
                seen.add(value)
                self._add(value)
        for value in always:
            if value and value not in seen:
                seen.add(value)
                self._add(value)

    def _add(self, value: str) -> None:
        self._set.add_needle(value.encode("utf-8"))
        self._count += 1

    def __len__(self) -> int:
        return self._count

    def __repr__(self) -> str:
        return f"Secrets(count={self._count})"

    __str__ = __repr__

    def scrub(self, text: str) -> str:
        return self._set.scrub(text)

    def contains(self, text: str) -> bool:
        return self._set.contains_secret(text.encode("utf-8", "replace"))


def load_secrets(root: Path, always: Iterable[str] = ()) -> Secrets:
    """Every value stored in the vault for the project at `root`, plus `always`.

    Fails closed: if the project has protected variables but the Keychain cannot be
    read, raises ContextError, because then the sanitizer would not know the values."""
    try:
        manifest = manifest_mod.load(root)
    except manifest_mod.ManifestError as exc:
        raise ContextError(f".vaultshield/protect.json: {exc}") from None
    values: list[str] = []
    if manifest is not None and manifest.protected:
        vault = Vault(manifest.project_id)
        for rel, names in manifest.protected.items():
            for name in names:
                try:
                    record = vault.fetch(rel, name)
                except VaultError:
                    raise ContextError(
                        "Cannot read the Keychain, so the project's secret values cannot be checked for. "
                        "Nothing was sent."
                    ) from None
                if record is None:
                    continue
                try:
                    values.extend(value_of(raw) for raw in decode_record(record))
                except ValueError:
                    continue
    return Secrets(values, always)


# --- secret shapes ---------------------------------------------------------------

_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
# Whole match is replaced.
_WHOLE_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # sk-..., sk-proj-..., sk-ant-...
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # ghp_ gho_ ghu_ ghs_ ghr_
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),  # Google OAuth access token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/\-]{20,}", re.IGNORECASE),
)
# Only the secret part is replaced.
_URL_PASSWORD = re.compile(r"(?<=://)[^/\s:@'\"]+:[^/\s@'\"]+(?=@)")
_ASSIGNED_SECRET = re.compile(
    r"(?i)(?P<head>\b[A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key)"
    r"[A-Za-z0-9_]*\s*[=:]\s*['\"]?)(?!\$)(?P<value>[^\s'\";&|)]{8,})"
)
# 32+ characters of letters, digits, - _ + = with upper case, lower case and a digit. `/` and `.`
# are left out of the class on purpose, so file paths and domain names are not mistaken for tokens.
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_+=\-])[A-Za-z0-9_+=\-]{32,}(?![A-Za-z0-9_+=\-])")


def _token_shaped(text: str) -> bool:
    return any(c.isdigit() for c in text) and any(c.isupper() for c in text) and any(c.islower() for c in text)


def _shape_count(text: str) -> int:
    """How many separate secret-shaped strings `text` holds. Several patterns can match
    the same text (a `ghp_` token is also a long token), so overlapping matches count once."""
    spans = [m.span() for m in _PRIVATE_KEY_BLOCK.finditer(text)]
    for pattern in _WHOLE_SHAPES:
        spans += [m.span() for m in pattern.finditer(text)]
    spans += [m.span() for m in _URL_PASSWORD.finditer(text)]
    spans += [m.span("value") for m in _ASSIGNED_SECRET.finditer(text)]
    spans += [m.span() for m in _LONG_TOKEN.finditer(text) if _token_shaped(m.group())]
    count, reach = 0, -1
    for start, end in sorted(spans):
        if start >= reach:
            count += 1
        reach = max(reach, end)
    return count


def _scrub_shapes(text: str) -> str:
    text = _PRIVATE_KEY_BLOCK.sub(REDACTED, text)
    for pattern in _WHOLE_SHAPES:
        text = pattern.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(REDACTED, text)
    text = _ASSIGNED_SECRET.sub(lambda m: m.group("head") + REDACTED, text)
    return _LONG_TOKEN.sub(lambda m: REDACTED if _token_shaped(m.group()) else m.group(), text)


def sanitize(text: str, secrets: Secrets) -> str:
    """`text` with every vault value and everything shaped like a secret replaced by ***."""
    return _scrub_shapes(secrets.scrub(text))


def _as_text(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace")
    return json.dumps(payload, ensure_ascii=False, default=str)


def assert_clean(payload: object, secrets: Secrets) -> None:
    """Raises LeakError if `payload` (text, bytes, or JSON-able data) still holds a vault
    value or anything shaped like a secret. Call it before every model request."""
    text = _as_text(payload)
    if secrets.contains(text):
        raise LeakError("A stored secret value is in the text about to be sent. Nothing was sent.")
    found = _shape_count(text)
    if found:
        raise LeakError(
            f"{found} secret-shaped string{'s' if found != 1 else ''} in the text about to be sent. Nothing was sent."
        )


# --- reading the project ----------------------------------------------------------


@dataclass
class ProjectContext:
    """The project as the model will see it. Every string in it has been sanitized."""

    name: str
    framework: dict = field(default_factory=dict)
    tree: list[str] = field(default_factory=list)
    tree_truncated: bool = False
    readme: str = ""
    dependencies: dict[str, dict] = field(default_factory=dict)
    commits: list[str] = field(default_factory=list)
    protected: dict[str, list[str]] = field(default_factory=dict)  # file -> variable NAMES

    def render(self) -> str:
        """The project summary as markdown."""
        lines = [f"Project folder: {self.name}"]
        start = self.framework.get("start_command")
        if start:
            port = self.framework.get("port")
            lines.append(f"Detected start command: `{start}`" + (f" (port {port})" if port else ""))
        if self.dependencies:
            lines += ["", "Dependencies (names only):"]
            for source, info in self.dependencies.items():
                label = f"{source}" + (f" ({info['name']})" if info.get("name") else "")
                lines.append(f"- {label}")
                for group, items in info.get("groups", {}).items():
                    if items:
                        lines.append(f"  - {group}: {', '.join(items)}")
        if self.tree:
            lines += ["", "Files (top levels):"]
            lines += [f"- {entry}" for entry in self.tree]
            if self.tree_truncated:
                lines.append(f"- ... (only the first {TREE_MAX_ENTRIES} entries are listed)")
        if self.readme:
            lines += ["", "README (start of file):", "```text", self.readme, "```"]
        if self.commits:
            lines += ["", "Recent commit subjects:"]
            lines += [f"- {subject}" for subject in self.commits]
        if self.protected:
            lines += ["", "Protected variables (names only; the values are not available):"]
            for path, names in sorted(self.protected.items()):
                lines.append(f"- {path}: {', '.join(names)}")
        return "\n".join(lines)


def _blocked_paths(root: Path, manifest: manifest_mod.Manifest | None) -> set[str]:
    """Every project-relative path whose content must never be read."""
    blocked = {item.path for item in discover(root)}
    if manifest is not None:
        blocked |= set(manifest.all_paths())
    return blocked


def _read_prefix(root: Path, rel: str, blocked: set[str], limit: int) -> str | None:
    """Up to `limit` bytes of a regular, non-symlink, non-secret file, or None.
    Compared without regard to case, because the macOS file system ignores it."""
    if rel.lower() in {b.lower() for b in blocked}:
        return None
    path = root / rel
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _find_readme(root: Path) -> str | None:
    try:
        names = {entry.name.lower(): entry.name for entry in os.scandir(root) if entry.is_file(follow_symlinks=False)}
    except OSError:
        return None
    for wanted in README_NAMES:
        if wanted in names:
            return names[wanted]
    return None


def _tree(root: Path) -> tuple[list[str], bool]:
    """Entries up to depth 3 and 200 in total, shallowest first, then sorted for display."""
    found: list[str] = []
    truncated = False
    level = [("", root)]
    for depth in range(1, TREE_MAX_DEPTH + 1):
        next_level: list[tuple[str, Path]] = []
        for prefix, folder in level:
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if entry.name in TREE_SKIP or entry.is_symlink():
                    continue
                rel = f"{prefix}{entry.name}"
                if len(found) >= TREE_MAX_ENTRIES:
                    truncated = True
                    break
                if entry.is_dir(follow_symlinks=False):
                    found.append(rel + "/")
                    next_level.append((rel + "/", Path(entry.path)))
                elif entry.is_file(follow_symlinks=False):
                    found.append(rel)
            if truncated:
                break
        if truncated:
            break
        level = next_level
    return sorted(found), truncated


_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<rest>.*)$")
_PLAIN_SPEC = re.compile(r"^[=<>!~]=?=?\s*[0-9][\w.*+!-]*(\s*,\s*[=<>!~]=?=?\s*[0-9][\w.*+!-]*)*$")
_NPM_NAME = re.compile(r"^(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9._-]*$")
_NPM_PLAIN_VERSION = re.compile(r"^[\^~<>=]*\s*v?[0-9][0-9A-Za-z.*+\-\s<>=|^~]*$")


def _python_dependency(line: str) -> str | None:
    """'name' or 'name >=1.2' from a requirement line. Options, URLs, VCS references and
    anything else that is not a plain name with a plain version are dropped."""
    line = line.split(" #")[0].strip()
    if not line or line.startswith("-") or "://" in line.split(";")[0].split("@")[0]:
        return None
    match = _REQUIREMENT.match(line)
    if not match:
        return None
    name = match.group("name")
    rest = match.group("rest").split(";")[0].strip()
    return f"{name} {rest}" if _PLAIN_SPEC.match(rest) else name


def _npm_dependency(name: object, version: object) -> str | None:
    if not isinstance(name, str) or not _NPM_NAME.match(name):
        return None
    if isinstance(version, str) and _NPM_PLAIN_VERSION.match(version.strip()):
        return f"{name} {version.strip()}"
    return name


def _dependencies(root: Path, blocked: set[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}

    text = _read_prefix(root, "package.json", blocked, DEP_FILE_MAX_BYTES)
    if text is not None:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            info: dict = {"name": data.get("name") if isinstance(data.get("name"), str) else None, "groups": {}}
            for group in ("dependencies", "devDependencies", "peerDependencies"):
                section = data.get(group)
                if isinstance(section, dict):
                    entries = [_npm_dependency(n, v) for n, v in section.items()]
                    info["groups"][group] = [e for e in entries if e][:DEPS_PER_LIST]
            found["package.json"] = info

    text = _read_prefix(root, "requirements.txt", blocked, DEP_FILE_MAX_BYTES)
    if text is not None:
        entries = [_python_dependency(line) for line in text.splitlines()]
        found["requirements.txt"] = {"name": None, "groups": {"dependencies": [e for e in entries if e][:DEPS_PER_LIST]}}

    text = _read_prefix(root, "pyproject.toml", blocked, DEP_FILE_MAX_BYTES)
    if text is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = None
        if isinstance(data, dict):
            project = data.get("project") if isinstance(data.get("project"), dict) else {}
            info = {"name": project.get("name") if isinstance(project.get("name"), str) else None, "groups": {}}
            deps = project.get("dependencies")
            if isinstance(deps, list):
                info["groups"]["dependencies"] = [e for e in (_python_dependency(str(d)) for d in deps) if e][:DEPS_PER_LIST]
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group, items in optional.items():
                    if isinstance(items, list) and isinstance(group, str):
                        info["groups"][f"optional:{group}"] = [e for e in (_python_dependency(str(d)) for d in items) if e][:DEPS_PER_LIST]
            poetry = (data.get("tool") or {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
            poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
            if isinstance(poetry_deps, dict):
                entries = [_npm_dependency(n, v) for n, v in poetry_deps.items() if n != "python"]
                info["groups"]["poetry"] = [e for e in entries if e][:DEPS_PER_LIST]
            found["pyproject.toml"] = info
    return found


def _commit_subjects(root: Path) -> list[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", "-n", str(COMMIT_COUNT), "--pretty=format:%s"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()][:COMMIT_COUNT]


def _framework(root: Path, blocked: set[str]) -> dict:
    for name in _FRAMEWORK_FILES:
        if name in blocked or (root / name).is_symlink():
            return {}
    detected = detect_framework(root)
    return {k: detected[k] for k in ("start_command", "port") if detected.get(k) not in ("", None)}


def scan_project(root: Path, secrets: Secrets | None = None) -> ProjectContext:
    """Read the project at `root` into a sanitized ProjectContext. See the module notes for
    what is and is not read. `secrets` defaults to the project's vault values."""
    root = root.resolve()
    if secrets is None:
        secrets = load_secrets(root)
    try:
        manifest = manifest_mod.load(root)
    except manifest_mod.ManifestError as exc:
        raise ContextError(f".vaultshield/protect.json: {exc}") from None
    blocked = _blocked_paths(root, manifest)

    def clean(text: str) -> str:
        return sanitize(text, secrets)

    tree, truncated = _tree(root)
    readme = ""
    readme_name = _find_readme(root)
    if readme_name is not None:
        raw = _read_prefix(root, readme_name, blocked, README_READ_BYTES)
        if raw is not None:
            # sanitize first, cut second: a key split by the cut would leave a fragment
            readme = clean(raw.replace("\r\n", "\n"))[:README_MAX_CHARS]

    dependencies = _dependencies(root, blocked)
    for info in dependencies.values():
        if info.get("name"):
            info["name"] = clean(info["name"])
        info["groups"] = {clean(g): [clean(i) for i in items] for g, items in info["groups"].items()}

    return ProjectContext(
        name=clean(root.name),
        framework={k: clean(str(v)) for k, v in _framework(root, blocked).items()},
        tree=[clean(entry) for entry in tree],
        tree_truncated=truncated,
        readme=readme,
        dependencies=dependencies,
        commits=[clean(s)[:COMMIT_SUBJECT_MAX_CHARS] for s in _commit_subjects(root)],
        protected={path: [clean(n) for n in names] for path, names in (manifest.protected.items() if manifest else [])},
    )
