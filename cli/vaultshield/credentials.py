"""Credentials_Module: value-shape classification, dummy-value generation,
env-file parsing, and the atomic `.env.sandbox` writer.

Implements the full Credentials_Module described in design.md:
`classify_shape`, `generate_dummy`, and `parse_env_file` (Task 3.1), plus
`build_sandbox_env` (Task 3.2), which combines the previous three into a
single atomic `.env.sandbox` writer.

Covers Requirements 3.2 (dummy values preserve structural shape) and 3.4
(env-file parsing) via `classify_shape`/`generate_dummy`/`parse_env_file`
in isolation. Requirements 3.1 (key-name preservation) and 3.3 (no real
value ever reaches disk) are properties of `build_sandbox_env` as a
whole — see its docstring below — rather than of any single helper
function in isolation.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import string
import tempfile
from pathlib import Path

# Shape signatures, exactly as declared in design.md.
URL_SHAPE = re.compile(r"^https?://")
KEY_SHAPE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")

# Connection-string shape. Broader than `URL_SHAPE`, which only knows http(s).
#
# Requirement 3.2 asks the dummy to preserve the value's structural shape, and
# for these schemes `_GENERIC_PLACEHOLDER` did not: a `DATABASE_URL` of
# `postgresql://user:pw@host:5432/db` became the literal `dummy-value`, which is
# not a URL at all. Anything in the Sandbox that parses that variable — which is
# most frameworks, at import time — then fails to start, so the Sandbox could not
# run the application whose screenshots the Run is supposed to capture.
#
# Captured groups are deliberately separate so each can be judged on its own.
# Scheme, port and path/query are reused: they describe the shape a parser needs
# and identify nothing. Userinfo is always discarded. The host is reused only if
# `_is_preservable_host` proves it local — see that function for why, and the
# password is matched only so it can be dropped. See
# `_generate_dummy_connection_string`.
CONNECTION_STRING_SHAPE = re.compile(
    r"""^
    (?P<scheme>
        postgresql|postgres|mongodb\+srv|mongodb|mysql|mariadb
      | redis\+tls|rediss|redis|amqps|amqp|https|http
    )
    ://
    (?:(?P<userinfo>[^/@\s]*)@)?      # optional user[:password]@ — discarded
    (?P<host>                         # host — preserved ONLY if provably local
        \[[0-9A-Fa-f:.]+\]            #   bracketed IPv6, e.g. [::1]
      | [^/?\#\s:]*                   #   hostname or IPv4 literal
    )
    (?::(?P<port>\d+))?               # optional port, safe to keep
    (?P<rest>[/?\#].*)?               # optional path/query/fragment
    $""",
    re.VERBOSE,
)

# Dummy userinfo substituted for whatever the real value carried. Fixed strings,
# never derived from the input, so no part of a real credential can survive.
_DUMMY_USER = "dummy_user"
_DUMMY_PASSWORD = "dummy_password"
# Host substituted for any host that is not provably local — see
# `_is_preservable_host`.
_DUMMY_HOST = "localhost"

# --- host preservation policy ------------------------------------------------
#
# A host is REPLACED unless it is provably local. This is an allowlist, and the
# default is to replace.
#
# The earlier implementation preserved every host on the reasoning that a host is
# "not a credential". That was wrong, and it leaked: a real MongoDB Atlas URI's
# cluster hostname survived verbatim into `.env.sandbox` and therefore into the
# Sandbox the agent runs in. Requirement 3.3 excludes every real value from that
# file, and infrastructure identity is exactly the kind of value the threat model
# cares about — a hostname names a reachable target, identifies the provider and
# often the account, and is enough to attack or fingerprint on its own.
#
# Only hosts that cannot identify anything outside the developer's own machine or
# private network are kept, because for those the *shape* is the only information
# they carry.
_PRESERVED_HOSTNAMES = frozenset({"localhost"})

# The IPv4 ranges preserved, written out so the policy is readable here rather
# than inferred from a library predicate:
#
#   127.0.0.0/8       loopback
#   10.0.0.0/8        RFC 1918 private
#   172.16.0.0/12     RFC 1918 private  (second octet 16-31)
#   192.168.0.0/16    RFC 1918 private
#
# Deliberately NOT `ipaddress.ip_address(...).is_private`. That property is
# broader than this policy: it also covers 0.0.0.0/8, 169.254.0.0/16 link-local,
# 100.64.0.0/10 carrier-grade NAT, 192.0.0.0/24, benchmarking and reserved
# ranges. Some of those are routable in a way that matters and none of them were
# reviewed for this decision, so relying on the property would silently widen the
# allowlist whenever the stdlib's definition changed. In particular `0.0.0.0` is
# a wildcard bind address rather than a destination and must NOT be preserved —
# the checks below reject it because its first octet matches none of the ranges.
_LOOPBACK_IPV4_FIRST_OCTET = 127
_PRIVATE_10_FIRST_OCTET = 10
_PRIVATE_172_FIRST_OCTET = 172
_PRIVATE_172_SECOND_OCTET_RANGE = range(16, 32)  # 172.16 - 172.31 inclusive
_PRIVATE_192_168_OCTETS = (192, 168)
# The one IPv6 address preserved. Compared against a parsed value rather than the
# literal text so every spelling of it (`::1`, `0:0:0:0:0:0:0:1`) is recognised.
_LOOPBACK_IPV6 = ipaddress.IPv6Address("::1")


def _is_preservable_host(host: str) -> bool:
    """True only for a host that cannot identify anything outside this machine.

    The allowlist behind `_generate_dummy_connection_string`. Returns True for
    `localhost`, IPv4/IPv6 loopback, and the three RFC 1918 private IPv4 ranges;
    False for everything else, which is what gets replaced with `_DUMMY_HOST`.

    A DNS name is NEVER preserved, however internal it looks.
    `db.company.internal` and `cluster0.abcde.mongodb.net` are treated
    identically: both name a host the sandboxed agent has no business learning,
    and "sounds internal" is not a security property. Only an IP literal can be
    checked, so anything that does not parse as one is replaced.

    Bracketed IPv6 (`[::1]`, the form RFC 3986 requires inside a URI) is accepted
    with the brackets stripped.

    `ipaddress` is used only to PARSE and normalise the literal. Every
    accept/reject decision is an explicit comparison against the constants above,
    so the policy can be audited by reading this function.
    """
    candidate = host.strip()
    if not candidate:
        return False
    if candidate.lower() in _PRESERVED_HOSTNAMES:
        return True

    literal = candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        # Not an IP literal, so it is a DNS name (or nonsense). Replaced.
        return False

    if isinstance(address, ipaddress.IPv6Address):
        # Only loopback. An IPv6 ULA (fc00::/7) is private but was not reviewed
        # for this policy, so it is replaced rather than quietly admitted.
        return address == _LOOPBACK_IPV6

    first, second = address.packed[0], address.packed[1]
    if first == _LOOPBACK_IPV4_FIRST_OCTET:
        return True
    if first == _PRIVATE_10_FIRST_OCTET:
        return True
    if first == _PRIVATE_172_FIRST_OCTET and second in _PRIVATE_172_SECOND_OCTET_RANGE:
        return True
    if (first, second) == _PRIVATE_192_168_OCTETS:
        return True
    # Everything else: public addresses, 0.0.0.0, link-local, CGNAT.
    return False

# KEY=value assignment, allowing whitespace around `=`. Restricted to
# standard shell/env identifier syntax (letters/digits/underscore, not
# starting with a digit) since that's the only thing a real environment
# file's keys can ever be.
_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

_KEY_ALPHABET = string.ascii_letters + string.digits
_DUMMY_URL = "https://example.invalid/dummy"
# Fallback dummy URL used only when `value` itself is (a prefix of)
# `_DUMMY_URL` — see `_generate_dummy_url`. Dropping the "s" is what makes
# it provably safe: any URL-shaped `value` contained in `_DUMMY_URL` must
# itself start with "https://" (the only place "http" occurs in
# `_DUMMY_URL`), and this string contains no "https" substring at all.
_DUMMY_URL_FALLBACK = "http://example.invalid/dummy"
_GENERIC_PLACEHOLDER = "dummy-value"
# Fallback used only when `value` itself is a substring of
# `_GENERIC_PLACEHOLDER` — see `_generate_dummy_generic`. Pure uppercase
# letters, deliberately disjoint from `_GENERIC_PLACEHOLDER`'s character
# set (lowercase letters + "-"), which is what makes it provably safe.
_GENERIC_PLACEHOLDER_FALLBACK = "REDACTED"


def classify_shape(value: str) -> str:
    """Returns 'url', 'key', or 'generic' based on the value's structural
    shape.

    Checked in priority order: 'url' (design.md's `URL_SHAPE`) before
    'key' (`KEY_SHAPE`), else 'generic'. The two patterns can never both
    match the same string in practice (a URL's `://` isn't part of
    `KEY_SHAPE`'s character class), but checking `url` first removes any
    ambiguity and mirrors the order the two regexes are declared in
    design.md.
    """
    if URL_SHAPE.match(value):
        # An http(s) value that carries `user:password@` is a credential, not a
        # plain endpoint, so it is classified as a connection string and has its
        # userinfo stripped. One without userinfo (`API_BASE=https://api.example
        # .com/v1`) is just configuration and keeps the fixed dummy URL, which is
        # both the established behaviour and the right answer: there is no secret
        # in it to replace.
        match = CONNECTION_STRING_SHAPE.match(value)
        if match is not None and match.group("userinfo"):
            return "connection_string"
        return "url"
    # Every non-http scheme. Checked before `key` for clarity only: a connection
    # string contains `:` and `/`, which `KEY_SHAPE`'s character class excludes,
    # so the two can never match the same value.
    if CONNECTION_STRING_SHAPE.match(value):
        return "connection_string"
    if KEY_SHAPE.match(value):
        return "key"
    return "generic"


def generate_dummy(value: str) -> str:
    """Given `classify_shape(value)`, returns a structurally-equivalent
    dummy value that is NEVER derived from `value`:
    - 'url'     -> a fixed, syntactically valid dummy URL.
    - 'key'     -> a freshly random same-length alphanumeric token.
    - 'generic' -> a fixed placeholder string.

    design.md's Property 5 (Task 3.3) requires the result to never
    contain `value` as a substring. Each branch below is a fixed/random
    constant already, so this only matters when `value` itself is short
    enough to appear inside that constant by coincidence — every branch
    detects that case and substitutes a value-independent alternative,
    still without ever deriving the replacement from `value`'s own
    characters.
    """
    # An empty value stays empty. It is the one input with no secret in it to
    # replace, and emptiness is itself the structural shape Requirement 3.2 asks
    # to preserve: an application that branches on `if not os.environ["X"]`
    # behaves in the Sandbox exactly as it does outside, whereas substituting
    # `dummy-value` silently turns "unset" into "set" and changes which code path
    # runs. Property 5's "never contains `value`" is trivially satisfiable here
    # and irrelevant — there is nothing to leak.
    if not value.strip():
        return value

    shape = classify_shape(value)
    if shape == "url":
        return _generate_dummy_url(value)
    if shape == "connection_string":
        return _generate_dummy_connection_string(value)
    if shape == "key":
        return _generate_dummy_key(value)
    return _generate_dummy_generic(value)


def _generate_dummy_connection_string(value: str) -> str:
    """Rebuilds a connection string with dummy credentials but a real shape.

        postgresql://user:REALPASS@localhost:5432/db
            -> postgresql://dummy_user:dummy_password@localhost:5432/db

    What is kept is what is structural and identifies nothing: the scheme (an
    application dispatches its driver on it), the port, and the path/query. What
    is replaced is the whole userinfo section — both the username and the
    password — with fixed strings that are not derived from the input in any way.

    The host is kept ONLY when `_is_preservable_host` proves it local, and
    replaced with `_DUMMY_HOST` otherwise. A hostname is infrastructure identity:
    it names a reachable target and usually the provider and account behind it,
    which is precisely what Requirement 3.3 keeps out of the Sandbox and what the
    threat model is about. Preserving it verbatim leaked a real MongoDB Atlas
    cluster hostname into `.env.sandbox`, so the default is now to replace.

    Shape survives either way, which is why replacing costs nothing structurally:
    `_DUMMY_HOST` is itself a valid host, so the value still parses and the
    application still starts. It just no longer resolves to anything real — the
    intended outcome, since the Sandbox reaching real infrastructure is the thing
    being prevented.

    Property 5 still holds: the result must not contain `value` as a substring.
    Userinfo is always rewritten, so a `value` carrying one cannot survive. A
    `value` with no userinfo (`redis://localhost:6379/0`) WOULD otherwise be
    reproduced verbatim, since every remaining part is preserved — so the dummy
    userinfo is inserted unconditionally, which makes the output strictly longer
    than and different from the input in every case.
    """
    match = CONNECTION_STRING_SHAPE.match(value)
    if match is None:  # pragma: no cover - classify_shape already matched
        return _generate_dummy_generic(value)

    scheme = match.group("scheme")
    host = match.group("host") or ""
    port = match.group("port")
    rest = match.group("rest") or ""

    # Replace by default. Only a provably local host is carried through, and an
    # empty host or one that would let the input reappear in the output is
    # replaced outright rather than patched.
    if not host or host in (_DUMMY_USER, _DUMMY_PASSWORD) or not _is_preservable_host(host):
        host = _DUMMY_HOST

    authority = f"{_DUMMY_USER}:{_DUMMY_PASSWORD}@{host}"
    if port:
        authority = f"{authority}:{port}"

    dummy = f"{scheme}://{authority}{rest}"

    # Belt and braces for Property 5. Unreachable for any realistic input —
    # inserting `dummy_user:dummy_password@` guarantees the input is not a
    # substring — but asserted by construction rather than by argument, because
    # this function's whole job is that no real value survives.
    if value in dummy:
        return _GENERIC_PLACEHOLDER_FALLBACK
    return dummy


def parse_env_file(path: Path) -> list[tuple[str, str]]:
    """Parses `KEY=value` lines from `path`, returning ordered
    `(key, value)` pairs in source-file order.

    - Blank lines (whitespace-only) are skipped.
    - Lines whose first non-whitespace character is `#` are treated as
      whole-line comments and skipped. There is no inline (trailing)
      comment support: a `#` appearing after a value is kept as part of
      that value, since a credential value legitimately containing `#`
      must not be silently truncated.
    - A value wrapped in matching single or double quotes has the outer
      quotes stripped; the inner content is kept verbatim (including any
      internal whitespace). No escape-sequence processing happens inside
      quotes — design.md only specifies "respecting quoted values", not
      shell-style escaping.
    - A non-blank, non-comment line that doesn't match `KEY=value` is
      skipped rather than raising, per Requirement 3.4's "without
      corrupting the generated Sandbox_Credential_File": an unparseable
      line simply contributes nothing, instead of failing the whole parse.
    """
    pairs: list[tuple[str, str]] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2)
        pairs.append((key, _unquote(raw_value)))
    return pairs


def build_sandbox_env(source_path: Path, dest_path: Path) -> Path:
    """Reads `source_path` line-by-line and writes `dest_path` with the
    same keys, in the same order, and `generate_dummy(value)` in place of
    every real value, preserving comment and blank lines verbatim in
    their original position. Returns `dest_path`.

    Satisfies Requirement 3.1 (the Sandbox_Credential_File contains the
    same key names as the source) and Requirement 3.3 (no real credential
    value ever reaches `dest_path`, or any other file written to disk
    during generation): the output is assembled entirely from
    `generate_dummy`'s dummy values, and `source_path`'s raw values are
    never written anywhere, including the temporary file used for the
    atomic write below.

    `parse_env_file` is intentionally NOT reused for the output-construction
    walk here: its `list[tuple[str, str]]` return value drops comment and
    blank lines by design (see its docstring), which is exactly right for a
    key/value parser but discards the structural information this
    function needs in order to satisfy "preserving key order and comment
    lines". So this function re-walks `source_path` itself, one line at a
    time, using the same line-classification rules as `parse_env_file`
    (blank-line and whole-line-comment detection, `_ASSIGNMENT_RE`,
    `_unquote`) so the two stay behaviorally consistent, without changing
    `parse_env_file`'s existing signature or behavior.

    Line handling:
    - Blank lines and whole-line comments (first non-whitespace character
      is `#`) are copied to the output verbatim, in their original
      position — this is what "preserving comment lines" means here, per
      design.md's `build_sandbox_env` docstring.
    - A line matching `KEY=value` becomes `KEY=<generate_dummy(value)>` in
      the output (quoting, if any, is not preserved — no requirement
      mandates quote-style fidelity, only that the key name and structural
      shape survive).
    - Any other non-blank, non-comment line (one that doesn't match
      `KEY=value`) is dropped rather than copied verbatim, mirroring
      `parse_env_file`'s own tolerance for unparseable lines. Copying an
      unrecognized line through unmodified could leak arbitrary source
      content (including a real value in an unexpected format) into
      `dest_path`, which Requirement 3.3 forbids; skipping it is the safe
      choice, matching Requirement 3.4's "without corrupting the generated
      Sandbox_Credential_File".

    The full output is built in memory first, as a list of lines, before
    any write happens — so an error raised while generating a dummy value
    (or anything else during the walk above) never results in a partial
    `dest_path`; `dest_path` is only ever touched by the single atomic
    replace performed by `_atomic_write`, once the complete content is
    ready.
    """
    out_lines: list[str] = []
    for raw_line in source_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            out_lines.append(raw_line)
            continue
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2)
        value = _unquote(raw_value)
        out_lines.append(f"{key}={generate_dummy(value)}")

    content = "\n".join(out_lines)
    if content:
        content += "\n"
    _atomic_write(dest_path, content)
    return dest_path


def _unquote(raw_value: str) -> str:
    """Strips one layer of matching outer quotes (`'...'` or `"..."`), if
    present. Non-matching or absent quotes are left untouched.
    """
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in ("'", '"'):
        return raw_value[1:-1]
    return raw_value


def _atomic_write(dest_path: Path, content: str) -> None:
    """Writes `content` to `dest_path` atomically: the full content is
    written to a temporary file in `dest_path`'s own directory, then
    `os.replace` swaps it into place in a single filesystem operation.

    This guarantees a concurrent reader of `dest_path` never observes a
    partially-written file, and that a crash mid-write leaves the
    pre-existing `dest_path` (if any) untouched rather than truncated.
    The temp file is created in the same directory as `dest_path` (never
    the system temp directory) so the final `os.replace` is a same-
    filesystem rename, not a cross-filesystem copy that could itself be
    interrupted partway through.

    On any exception raised while writing the temp file, the temp file is
    removed before re-raising — no stray temp file is left behind, and
    `dest_path` is never touched (the `os.replace` simply never happens).
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=dest_path.parent, prefix=f".{dest_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_path, dest_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _generate_dummy_url(value: str) -> str:
    """Fixed dummy URL, per design.md.

    `value` is guaranteed URL-shaped here (this helper is only reached via
    that branch of `generate_dummy`), i.e. it starts with "http://" or
    "https://". `_DUMMY_URL` contains no "http://" substring (only
    "https://"), so a `value` starting with plain "http://" can never be
    a substring of it — that case returns `_DUMMY_URL` directly. The only
    way `value` CAN be a substring of `_DUMMY_URL` is if `value` itself
    starts with "https://"; `_DUMMY_URL_FALLBACK` contains no "https"
    substring anywhere, so such a `value` can never be a substring of the
    fallback either. Either way the result provably never contains
    `value`.
    """
    if value not in _DUMMY_URL:
        return _DUMMY_URL
    return _DUMMY_URL_FALLBACK


def _generate_dummy_key(value: str) -> str:
    """Freshly random same-length alphanumeric token, drawn from
    `secrets` (not derived from `value`'s own characters in any way).

    Regenerates on landing exactly on `value` itself — astronomically
    unlikely given `KEY_SHAPE` requires length >= 16, but checked anyway
    for correctness. Since the candidate is always the same length as
    `value`, "contains value as a substring" and "equals value" are the
    same condition here, so this equality check is sufficient to satisfy
    Property 5's non-containment requirement.
    """
    length = len(value)
    candidate = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(length))
    while candidate == value:
        candidate = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(length))
    return candidate


def _generate_dummy_generic(value: str) -> str:
    """Fixed placeholder, per design.md.

    An empty `value` (an env line like `KEY=`) is the one case where no
    dummy can satisfy "never contains the original value as a
    substring" — every string trivially contains "" — so it's simply
    returned as the ordinary placeholder.

    Otherwise, if `value` is a substring of `_GENERIC_PLACEHOLDER`, `value`
    must be made up entirely of lowercase letters and/or "-" (the only
    characters `_GENERIC_PLACEHOLDER` contains). `_GENERIC_PLACEHOLDER_FALLBACK`
    is uppercase-only, sharing no characters with that set, so such a
    `value` can never be a substring of it.
    """
    if not value or value not in _GENERIC_PLACEHOLDER:
        return _GENERIC_PLACEHOLDER
    return _GENERIC_PLACEHOLDER_FALLBACK
