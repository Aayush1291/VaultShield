"""Static rule registry and the diff scanner that applies it.

Task 22.1 — design.md's "Security_Scan_Service (`security_scan/`)"
section, which fixes both the shape of a rule and the entry point:

    @dataclass(frozen=True)
    class Rule:
        rule_id: str
        pattern: re.Pattern
        description: str

    RULES: list[Rule] = [
        Rule("secret-literal", SECRET_PATTERN, "Possible hardcoded secret"),
        Rule("destructive-sql", DESTRUCTIVE_SQL_PATTERN, "Destructive SQL statement"),
        Rule("rm-rf", RM_RF_PATTERN, "Recursive force delete command"),
        Rule("auth-disabled", AUTH_DISABLED_PATTERN, "Disabled authentication check"),
        Rule("outbound-call", OUTBOUND_CALL_PATTERN, "New outbound network call"),
    ]

    def scan_diff(run_diff: RunDiff) -> list[SecurityFinding]:
        \"\"\"Applies every Rule's regex against each changed file's added
        lines only (never executes any diff content). Returns one
        SecurityFinding per match, with file_path/line_number/rule_id/
        description populated.\"\"\"

Covers Requirement 17.1 (rule-based checks "for secrets, destructive SQL
statements, `rm -rf` commands, disabled authentication checks, and new
outbound network calls"), Requirement 17.2 (each match becomes a
`Security_Finding` "describing the matched rule and location"), and
Requirement 17.3 (checks run "exclusively through rule-based analysis,
without executing any code from the Run_Diff").

The Celery task that persists what this returns is Task 22.3
(`security_scan/tasks.py`), and the diff-ingest view that enqueues it is
Task 23.2. This module is deliberately Django-model-aware but
request-unaware: it knows how to derive findings from a change set, not
when a change set arrives.

### Requirement 17.3 is a property of what this file does *not* contain

The whole file is `re.search` over strings. There is no `exec`, `eval`,
`compile`, `import`/`importlib`, `subprocess`, `os.system`, `pickle`,
`yaml.load`, `ast.literal_eval`, or template render anywhere in it, and no
diff-derived string is ever used to build a path that is opened, a module
name that is imported, or a command that is run. That is the entire
implementation of "without executing any code from the Run_Diff" — the
requirement is satisfied by the absence of those constructs rather than by
a guard that could be bypassed, which is why it is worth stating as an
invariant a reviewer can check by reading the imports.

Diff content is also never interpolated into another regex. Every pattern
below is a module-level literal compiled once at import; a diff line is
only ever the *subject* of a match, never part of a pattern. A scanner
that compiled patterns out of scanned content would hand an uploaded diff
control over the matcher, which is the regex-shaped version of the same
risk.

### Where added-line text comes from, and the gap that remains

`scan_diff` matches only added lines, per design.md and Requirement 17.1's
purpose: a reviewer is evaluating what the agent *introduced*. Text that a
change removed cannot be a new risk (it is being taken out), and
surrounding context lines were already in the repository before the Run, so
flagging either would report pre-existing code as this Run's finding.

That text is read from each `RunDiff.files` entry's `patch` key: the
unified diff for that one file, as `git diff` prints it. A unified diff is
the carrier this scanner needs, for two reasons beyond convenience —
"added lines only" is *intrinsic* to the format (a `+` prefix marks
exactly the lines the change introduces), and its `@@` hunk headers carry
the post-change line numbers, which is what lets a finding name a real
location in the file a reviewer will open rather than an offset into a
detached list of lines.

**The gap, stated plainly:** as of this task, nothing produces that key.
`cli/vaultshield/git_capture.py` writes `diff.json` from `git diff
--numstat`, which is counts only, and `runs.serializers`'
`FileDiffStatSerializer` validates a file entry down to exactly `path`,
`additions`, and `deletions` — so a `patch` sent today would be dropped at
ingest. design.md specifies both `RunDiff.files` as
`[{"path", "additions", "deletions"}, ...]` *and* a scanner that reads
added lines, and those two statements cannot both be complete; the diff
payload has to grow a text field for Requirement 17.1 to do anything on
real uploads.

Closing that is a change to the CLI's diff capture and to the ingest
serializer, which are Tasks 12.2 and 16.7's files, not this one — so this
module is written to be correct on both sides of the fix rather than to
reach across into them. A count-only entry (every entry produced today)
carries no scannable text and yields no findings, which is the truthful
result for "we were given no added-line text", not a silent pass. A
`patch`-carrying entry is scanned in full the moment ingest starts keeping
one. `PATCH_KEY` below is the single place the key is named, so whichever
task closes the gap has one spelling to match.

### Per-file, and why the parse is per-file rather than whole-diff

`_iter_added_lines` is handed one file's patch and yields
`(line_number, text)` pairs. It deliberately does not parse a multi-file
`git diff` dump and recover paths from `+++ b/...` headers: the file path
is already an authoritative field on the entry (`path`), and re-deriving it
from diff text would mean trusting uploaded content to say which file a
finding belongs to. Anything before the first `@@` in a patch — the
`diff --git` line, `index` line, `---`/`+++` headers — is skipped for that
reason, which also keeps the `+++ b/secrets.env` header from being read as
an added line beginning with `+`.

### One finding per rule per line, not per regex match

`scan_diff` uses `pattern.search`, not `finditer`: a rule that matches a
line twice produces one finding. `SecurityFinding` stores `run`,
`rule_id`, `file_path`, `line_number`, and `description` and nothing else —
no column offset, no matched text — so two findings from one line would be
byte-identical rows, indistinguishable in the dashboard's Security tab
(Requirement 20.5) and impossible for a reviewer to act on separately.
Different rules matching the same line *do* each produce a finding, since
those differ in `rule_id` and describe genuinely different risks.

Findings come back in a stable, reviewable order: files in payload order
(which is git's own path order), lines in file order within a file, and
`RULES` order within a line. `bulk_create` preserves the order of the list
it is given, so that is also the order the dashboard reads them back in.

### Malformed entries are skipped, not raised on

`RunDiff.files` is a `JSONField`, so the column enforces nothing; ingest
validation is what normally guarantees the shape, and a row written by a
data migration, a fixture, or a shell is under no such guarantee. An entry
that is not a mapping, has no usable `path`, or whose `patch` is not a
string is skipped. This runs inside a Celery worker with no user waiting on
it and no request to return a 400 to, so raising would turn one bad row
into a task that fails on every retry and blocks a Run's review; and
Requirement 17 asks the scanner to report what it *finds*, which it can
still do for every other file in the change set. The malformed entry
contributes no findings, which is visibly "nothing scanned" rather than
"scanned and clean".

### Precision, and the false-positive/false-negative trade

Property 26 (Task 22.2) is two-sided: planted risk patterns must be found,
and content containing none of them must produce no finding. The patterns
below are therefore written to match *call sites and literals*, not
mentions — `requests.get(` rather than the word "requests", a quoted
credential literal rather than any assignment to a variable named `token`,
`rm` with both a recursive and a force flag rather than any `rm`. Two
specific temptations are declined for that reason:

- **No bare-URL rule.** An `outbound-call` rule matching `https?://...`
  anywhere would flag every documentation link a change adds to a comment
  or docstring, which is the single most common thing an agent adds.
- **No "value looks high-entropy" secret rule.** Entropy heuristics fire
  on hashes, UUIDs, minified identifiers, and base64 test fixtures, none
  of which are secrets.

Each rule's exact matching contract is documented on its pattern below, so
Task 22.2's generators can be built from the stated contract rather than
reverse-engineered from the regex.

The one place a false positive is accepted on purpose is the
`WHERE`-less `DELETE`/`UPDATE` half of `destructive-sql`, which is
line-scoped and so cannot see a `WHERE` clause on the following line; that
trade is argued at the pattern itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from security_scan.models import SecurityFinding

if TYPE_CHECKING:  # pragma: no cover - import for type checking only
    from runs.models import RunDiff


# Keys `scan_diff` reads off a `RunDiff.files` entry. Named rather than
# inlined because `path` has to agree with `FileDiffStatSerializer` and
# `cli/vaultshield/git_capture.py`'s `FileDiffStat`, and `patch` is the key
# whichever task closes the added-line-text gap has to start producing (see
# the module docstring).
FILE_PATH_KEY = "path"
PATCH_KEY = "patch"

# A unified-diff hunk header: `@@ -<old start>[,<old count>] +<new start>[,<new
# count>] @@[ optional section heading]`. Only the new-file start is captured —
# it is the line number the first line of the hunk occupies *after* the change,
# which is the number a reviewer needs to find a finding in the file.
#
# Anchored with `match` at the start of a line, and `@@` exactly (not `@@@`):
# the three-at form is a combined diff of a merge with more than one parent,
# whose per-line prefixes mean something different (one column per parent), so
# reading it with the two-way rules below would mis-attribute lines. A patch
# containing only combined hunks therefore yields no added lines rather than
# wrong ones.
_HUNK_HEADER_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")

# Names that mark a value as a credential rather than ordinary data. Used
# only inside `SECRET_PATTERN`; kept separate because the list is the part
# most likely to grow, and it reads as a list rather than as regex noise.
_SECRET_NAME = r"""(?:
      api[_-]?key
    | secret[_-]?key
    | client[_-]?secret
    | access[_-]?key
    | secret[_-]?access[_-]?key
    | private[_-]?key
    | encryption[_-]?key
    | auth[_-]?token
    | access[_-]?token
    | refresh[_-]?token
    | bearer[_-]?token
    | secret
    | token
    | password
    | passwd
    | passphrase
)"""

# `secret-literal` — Possible hardcoded secret.
#
# Matches exactly two things:
#
# 1. A credential-named field assigned a *quoted* literal of at least 8
#    characters: `API_KEY = "sk-live-abc123..."`, `"password": "hunter2!!"`,
#    `client_secret: 'abcdefgh'`, `authToken => "..."`. The quotes are what
#    make this "hardcoded" and are what keep it precise — the overwhelmingly
#    common benign shape is a credential name assigned an *expression*
#    (`API_KEY = os.environ["API_KEY"]`, `token = getenv("TOKEN")`,
#    `token: ${{ secrets.GITHUB_TOKEN }}`), whose value does not start with a
#    quote and so cannot match. The 8-character floor drops the other benign
#    shape, a name assigned a short placeholder or an empty string
#    (`password = ""`, `token: "n/a"`).
#
#    `\w*` after the name lets a suffixed field match (`api_key_value`,
#    `SECRET_KEY_FALLBACK`) and no leading `\b` is required, so a prefixed one
#    does too (`DJANGO_SECRET_KEY`, `STRIPE_API_KEY`).
#
# 2. A recognisable vendor credential format, wherever it appears, since these
#    are self-identifying and need no surrounding assignment: AWS access key
#    ids, GitHub tokens, OpenAI-style `sk-` keys, Slack `xox*` tokens, and a
#    PEM private-key header. These are the cases where a real secret is
#    committed as a bare value — inside a URL, a header dict, a CLI argument —
#    with no credential-named field anywhere on the line.
#
# What it deliberately does not do: no entropy heuristic (see the module
# docstring), and no attempt to distinguish a real credential from a
# realistic-looking fixture. `description` says "Possible" for that reason —
# this rule surfaces candidates for a human, which is what Requirement 17's
# user story asks for.
SECRET_PATTERN = re.compile(
    r"""
    (?:
        # (1) credential-named field assigned a quoted literal, >= 8 chars
        """ + _SECRET_NAME + r"""
        \w*                          # optional suffix: api_key_value, ...
        ["']? \s* (?: := | => | [:=] ) \s*     # `=`, `:`, `:=`, `=>`; the
                                               # optional quote covers a
                                               # quoted key: "PASSWORD": "..."
        (?P<quote>['"`])
        (?: (?! (?P=quote) ) [^\r\n] ){8,}
        (?P=quote)
      |
        # (2) self-identifying vendor credential formats. Case-sensitive
        # (`(?-i:...)` locally undoes the pattern-wide IGNORECASE), because
        # every one of these formats fixes its own case — an AWS key id is
        # uppercase, a `ghp_`/`sk-`/`xox` prefix and a PEM header are not —
        # and matching them case-blind is what would let an ordinary
        # lowercase identifier of the right length read as a credential.
        (?-i:
            AKIA[0-9A-Z]{16}
          | \b gh[pousr]_[A-Za-z0-9]{20,}
          | \b sk-(?:live|test|proj)?-?[A-Za-z0-9]{16,}
          | \b xox[baprs]-[A-Za-z0-9-]{10,}
          | -----BEGIN\ (?:[A-Z]+\ )?PRIVATE\ KEY-----
        )
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# `destructive-sql` — Destructive SQL statement.
#
# Matches:
#
# - `DROP TABLE|DATABASE|SCHEMA|VIEW|INDEX|COLUMN|CONSTRAINT` — irreversibly
#   removes a schema object. `COLUMN`/`CONSTRAINT` are listed so
#   `ALTER TABLE t DROP COLUMN c` matches on the `DROP` without this rule
#   needing to model `ALTER` statements.
# - `TRUNCATE [TABLE] <name>` — empties a table with no row-level condition
#   possible, so there is no benign narrower form to distinguish it from.
# - `DELETE FROM <table>` and `UPDATE <table> SET` with no `WHERE` on the same
#   line — an unbounded delete or mass column overwrite.
#
# The last two are the deliberate false-positive trade named in the module
# docstring. Matching is per added line, so a statement whose `WHERE` sits on
# the next line reads here as unbounded and is reported. That direction is the
# right one for a review aid: a reviewer sees the finding next to the diff and
# dismisses it in a second, whereas the opposite trade (require the whole
# statement on one line) would silently miss the formatted multi-line
# `DELETE FROM users;` that is the actual risk. The alternative — buffering
# lines into statements — would mean reassembling SQL out of arbitrary added
# lines across hunks, in files that may not be SQL at all.
#
# Case-insensitive because SQL keywords are written both ways, and embedded in
# host-language string literals just as often as in `.sql` files, which is why
# nothing here anchors to a statement start.
DESTRUCTIVE_SQL_PATTERN = re.compile(
    r"""
    \b(?:
        DROP \s+ (?:TABLE|DATABASE|SCHEMA|VIEW|INDEX|COLUMN|CONSTRAINT) \b
      | TRUNCATE \s+ (?: TABLE \s+ )? ["`\[]? \w
      | DELETE \s+ FROM \s+ (?! .* \b WHERE \b ) ["`\[]? \w
      | UPDATE \s+ ["`\[]? \w [\w"`\].\[]* \s+ SET \b (?! .* \b WHERE \b )
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# `rm-rf` — Recursive force delete command.
#
# Requirement 17.1 names "`rm -rf` commands", and the flags can be spelled
# several ways for the same command, so this matches an `rm` invocation whose
# following option words supply *both* recursion and force, in any order and
# any grouping: `rm -rf`, `rm -fr`, `rm -Rf`, `rm -r -f`, `rm -rvf`,
# `rm --recursive --force`, `rm -r --force`.
#
# Both halves are zero-width lookaheads from the same position after `rm`, so
# neither ordering is privileged. Each skips a run of option words and then
# requires the flag it is looking for. Recursion is `--recursive` or a short
# bundle containing `r`/`R`; force is `--force` or a short bundle containing
# `f`/`F`. The short-bundle forms require a *single* leading dash
# (`-[A-Za-z]*`), which is what stops `--force` from satisfying the recursion
# half by way of the `r` in "force".
#
# The separator between words is `[\s,'"\[]+` rather than `\s+` so the argv-list
# spellings match too — `subprocess.run(["rm", "-rf", target])` and
# `execFile('rm', ['-rf', dir])` are how a Python or JS change actually performs
# this delete, and quoting the words apart (with the flags in their own array,
# in the JS case) is the only difference from the shell form. A closing `]` is
# deliberately *not* in the separator, so `[["rm", "f"], ["-rf"]]` — two
# unrelated argument lists — cannot have its flag read as belonging to the `rm`.
#
# Two properties of the skip worth keeping: an option word is `-[\w-]*`, one
# leading dash then a single greedy run, so it is unambiguous per word and
# cannot backtrack badly on a long option list; and the skip stops at the first
# word that is not an option, so the `-r` of a *later* command on the same line
# cannot satisfy the lookahead for an earlier `rm -f`. `rm -f one && rm -r two`
# produces no match, correctly, since neither invocation is both recursive and
# forced.
#
# Scoped to `rm` rather than extended to `shutil.rmtree`, `rimraf`, or
# `fs.rm(..., {recursive: true, force: true})`: Requirement 17.1 names this
# rule after the shell command, and each of those would need its own contract
# and its own true/false-positive reasoning rather than riding along here.
RM_RF_PATTERN = re.compile(
    r"""
    \b rm \b
    (?= (?: [\s,'"\[]+ -[\w-]* )* [\s,'"\[]+ (?: --recursive \b | -[A-Za-z]* [rR] ) )
    (?= (?: [\s,'"\[]+ -[\w-]* )* [\s,'"\[]+ (?: --force \b     | -[A-Za-z]* [fF] ) )
    """,
    re.VERBOSE,
)

# `auth-disabled` — Disabled authentication check.
#
# Four shapes, covering how an authentication check actually gets turned off
# in a diff:
#
# 1. **An access-control list emptied or opened up.** DRF's
#    `permission_classes`/`authentication_classes` set to `[]` or to
#    `[AllowAny]`, in either the decorator/attribute form
#    (`permission_classes = [AllowAny]`) or the settings-dict form
#    (`"DEFAULT_PERMISSION_CLASSES": []`) — which is why there is no leading
#    `\b` and why a quote may sit before the separator.
# 2. **CSRF protection waived**: `@csrf_exempt`, `csrf_exempt = True`.
# 3. **A gate flag switched off**: `require_auth = False`,
#    `"requireAuth": false`, `loginRequired: null`, `check_auth => 0`. The name
#    list is deliberately restricted to names that describe a *gate*
#    (`..._required`, `require_auth...`, `check_auth...`, `verify_token`,
#    `auth_enabled`); the value list is the four ways "off" is spelled across
#    Python, JS, and JSON/YAML.
#
#    Bare state flags — `authenticated = False`, `is_authenticated = False`,
#    `authorized = False` — are excluded, even though they read like a match.
#    They describe what is currently true of a session rather than whether a
#    check runs, and they are ordinary code: a logout sets one, a test fixture
#    sets one, a model field defaults to one. Flagging them is how this rule
#    would become the noise a reviewer learns to skip.
# 4. **A guard commented out rather than deleted** — an added line that is a
#    `#` or `//` comment whose content is a *decorator*, a *guard call*, or an
#    *access-control assignment*: `# @login_required`, `// requireAuth(req)`,
#    `# permission_classes = [IsAuthenticated]`. This shape only exists in
#    added lines, which is exactly what this scanner reads: commenting a
#    decorator out shows up as a deletion of the real line plus an addition of
#    the commented one.
#
#    The `@`/`(`/`=` requirement is what keeps prose out. A comment is the one
#    place an auth *word* appears with no auth *code* around it, so without it
#    this alternative would flag "# authenticate the request first".
#
# `IsAuthenticated` appearing *positively* is not matched — adding it is
# adding a check. Only its removal-shaped spellings (an emptied list, a
# commented-out reference) match, which is the distinction between this rule
# and a rule that would fire on every authenticated view an agent writes.
AUTH_DISABLED_PATTERN = re.compile(
    r"""
    (?:
        # (1) an access-control list emptied or opened to everyone
        (?: permission | authentication ) _classes
        ["']? \s* [:=] \s* \[ \s* (?: \] | ["']? AllowAny )
      |
        # (2) CSRF protection waived
        @ csrf_exempt \b
      | \b csrf_exempt \s* [:=] \s* true \b
      |
        # (3) an auth gate flag turned off
        \b (?:
              require[_-]?auth \w*
            | auth \w* [_-] required
            | login[_-]?required
            | permission[_-]?required
            | check[_-]?auth \w*
            | verify[_-]? (?: token | auth \w* )
            | auth[_-]? (?: enabled | check | guard )
          )
        ["']? \s* (?: := | => | [:=] ) \s* (?: false | none | null | 0 ) \b
      |
        # (4) an auth guard commented out instead of removed
        (?: \# | // ) \s*
        (?:
            @ \s* (?:
                  login_required
                | permission_classes
                | permission_required
                | authentication_classes
                | require[_-]?auth \w*
                | authenticated?
                | authGuard
                | csrf_protect
              ) \b
          | (?:
                  require[_-]?auth \w*
                | login_required
                | permission_required
                | ensureAuthenticated
                | authenticate
                | authGuard
                | csrf_protect
              ) \s* \(
          | (?: permission | authentication ) _classes \s* =
        )
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# `outbound-call` — New outbound network call.
#
# Matches call sites that open a connection to somewhere else: the Python
# HTTP clients (`requests`, `httpx`, `urllib`, `aiohttp`, `http.client`), the
# JS ones (`fetch`, `axios`, `XMLHttpRequest`, `http(s).request`,
# `net.connect`), raw socket connection setup, and the two shell fetchers
# (`curl`, `wget`).
#
# Every alternative requires the *call*, not the name: `requests.get(`
# matches, an added `import requests` or the word "requests" in a comment does
# not. That is what keeps this rule from firing on the dependency and import
# lines that accompany any real change.
#
# `curl`/`wget` must be followed by an option or a URL, so prose mentioning
# either does not match. `socket.socket(` is absent on purpose — constructing
# a socket is equally how a *listener* is built, so `create_connection` and
# `.connect(` on a socket object are used instead, both of which are outbound
# by definition. That `.connect(` alternative is restricted to socket-named
# receivers (`sock`, `socket`, `s`) rather than accepting any receiver: a bare
# `.connect(` is also how every database client, message-queue client, and
# signal/slot binding in the ecosystem spells its own unrelated operation.
#
# No bare-URL alternative, for the reason given in the module docstring: it
# would flag every documentation link an added comment contains.
OUTBOUND_CALL_PATTERN = re.compile(
    r"""
    (?:
        # Python HTTP clients
        \b (?: requests | httpx ) \s* \. \s*
          (?: get | post | put | patch | delete | head | options | request
            | Session | Client | AsyncClient ) \s* \(
      | \b urllib \b [\w.]* \. \s* (?: urlopen | urlretrieve ) \s* \(
      | \b aiohttp \s* \. \s* (?: ClientSession | request ) \s* \(
      | \b http \. client \. \s* HTTPS? Connection \s* \(
      | \b (?: urlopen | urlretrieve ) \s* \(
      |
        # JS/browser HTTP clients
        \b fetch \s* \(
      | \b axios \s* (?: \. \s* \w+ )? \s* \(
      | \b XMLHttpRequest \s* \(
      | \b https? \s* \. \s* (?: request | get ) \s* \(
      |
        # raw sockets
        \b (?: socket \s* \. \s* )? create_connection \s* \(
      | \b net \s* \. \s* (?: connect | createConnection ) \s* \(
      | \b (?: sock | socket | s ) \s* \. \s* connect \s* \(
      |
        # shell fetchers, in a script or handed to a subprocess
        \b (?: curl | wget ) \s+ (?: - | ["']? https? :// )
    )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Rule:
    """One static check: an id, the pattern it matches, and what it means.

    Exactly design.md's declaration. Frozen because the registry is a
    constant read by a Celery worker — a mutable rule would let one scan
    change what a later scan on the same worker process checks for — and
    because that makes a `Rule` hashable, so a test can compare rule sets
    as sets.

    `rule_id` is what lands in `SecurityFinding.rule_id` and is therefore
    the stable identifier the dashboard's Security tab (Requirement 20.5)
    groups and labels findings by; `description` is the human-facing half
    that Requirement 17.2's "describing the matched rule" needs, copied
    onto each finding so a stored row stays readable after its rule is
    reworded or retired.
    """

    rule_id: str
    pattern: re.Pattern[str]
    description: str


# The registry, in the order Requirement 17.1 lists the five check
# categories. Order is meaningful downstream: `scan_diff` applies rules in
# this order, so it is the order multiple findings on one line come back
# in, and `bulk_create` preserves it.
RULES: list[Rule] = [
    Rule("secret-literal", SECRET_PATTERN, "Possible hardcoded secret"),
    Rule("destructive-sql", DESTRUCTIVE_SQL_PATTERN, "Destructive SQL statement"),
    Rule("rm-rf", RM_RF_PATTERN, "Recursive force delete command"),
    Rule("auth-disabled", AUTH_DISABLED_PATTERN, "Disabled authentication check"),
    Rule("outbound-call", OUTBOUND_CALL_PATTERN, "New outbound network call"),
]


def scan_diff(run_diff: RunDiff) -> list[SecurityFinding]:
    """Applies every `Rule` to each changed file's added lines and returns
    the resulting findings, unsaved (Requirements 17.1, 17.2, 17.3).

    Each returned `SecurityFinding` has `run`, `rule_id`, `file_path`,
    `line_number`, and `description` populated and no primary key —
    `run_security_scan` (Task 22.3) persists them with a single
    `bulk_create`, so nothing here touches the database.

    Only added lines are read, and only as text: no diff content is
    executed, imported, or interpolated into a pattern (see the module
    docstring). Added-line text comes from each file entry's `patch` key;
    an entry without one contributes nothing, which is the case for every
    entry the current ingest path produces — see the module docstring's
    note on that gap.

    One finding per rule per matching line, ordered by file (payload
    order), then line, then `RULES` order.

    Args:
        run_diff: the `RunDiff` to scan. `run_diff.run` becomes each
            finding's `run`, and `run_diff.files` is the change set walked.

    Returns:
        The findings, in the stable order described above. Empty when the
        change set contains no added-line text or nothing matched — those
        two outcomes are indistinguishable here by design, since "nothing
        to scan" and "scanned and clean" are both correctly "no findings"
        for this function to report.
    """
    run = run_diff.run
    findings: list[SecurityFinding] = []

    for file_entry in _iter_file_entries(run_diff.files):
        file_path = file_entry[FILE_PATH_KEY]

        for line_number, text in _iter_added_lines(file_entry[PATCH_KEY]):
            findings.extend(
                SecurityFinding(
                    run=run,
                    rule_id=rule.rule_id,
                    file_path=file_path,
                    line_number=line_number,
                    description=rule.description,
                )
                # `search`, not `finditer`: one finding per rule per line
                # (see the module docstring).
                for rule in RULES
                if rule.pattern.search(text) is not None
            )

    return findings


def _iter_file_entries(files: Any) -> Iterator[dict[str, Any]]:
    """Yields the entries of a `RunDiff.files` payload that carry both a
    usable file path and scannable patch text.

    Skips anything else rather than raising — the column is a `JSONField`
    with no shape guarantee, and this runs in a worker with nothing to
    return an error to (see the module docstring). Skipped: a `files` value
    that is not a list, an entry that is not a mapping, an entry whose
    `path` is missing/blank/not a string, and an entry whose `patch` is
    missing or not a string.

    An entry whose `patch` is an empty string is yielded and simply
    produces no added lines, since "a file with an empty patch" is a
    well-formed statement about the change set rather than a malformed
    entry.
    """
    if not isinstance(files, list):
        return

    for entry in files:
        if not isinstance(entry, dict):
            continue

        file_path = entry.get(FILE_PATH_KEY)
        if not isinstance(file_path, str) or not file_path.strip():
            continue

        if not isinstance(entry.get(PATCH_KEY), str):
            continue

        yield entry


def _iter_added_lines(patch: str) -> Iterator[tuple[int, str]]:
    """Yields `(line_number, text)` for each added line of one file's
    unified-diff `patch`, with `line_number` the line's position in the
    file *after* the change.

    The walk is the unified-diff format read literally, one line at a time,
    tracking where the current hunk places us in the post-change file:

    - `@@ -a,b +c,d @@` resets the position to `c`, the new-file line the
      hunk's first line occupies.
    - `+line` is an added line: it is yielded at the current position, and
      advances it.
    - `-line` is a removed line: skipped, and does *not* advance, because a
      deleted line occupies no line in the post-change file. Getting this
      wrong is what makes reported line numbers drift after the first
      deletion in a hunk.
    - `\\ No newline at end of file` is metadata about the preceding line,
      not a line: skipped without advancing.
    - anything else is a context line (a leading space, or the bare empty
      line git sometimes emits for an empty context line): not yielded,
      but advances the position.

    Everything before the first hunk header is skipped — `diff --git`, the
    `index` line, and the `---`/`+++` path headers — which is also what
    stops `+++ b/config.env` from being read as an added line starting with
    `+`. Since `path` is already an authoritative field on the entry, no
    path is ever recovered from patch text (see the module docstring).

    Nothing here interprets the yielded text: it is a `str` handed to
    `re.search` and nothing else (Requirement 17.3).
    """
    # `None` until the first hunk header, which is what marks "still in the
    # preamble" and makes the preamble skip fall out of the same check that
    # tracks position.
    line_number: int | None = None

    # `splitlines` rather than `split("\n")` so a patch using CR/LF or CR
    # endings is walked line-wise too, and so a trailing newline does not
    # produce a phantom empty final line.
    for raw_line in patch.splitlines():
        hunk_header = _HUNK_HEADER_PATTERN.match(raw_line)
        if hunk_header is not None:
            line_number = int(hunk_header.group("new_start"))
            continue

        if line_number is None:
            continue

        if raw_line.startswith("+"):
            yield line_number, raw_line[1:]
            line_number += 1
        elif raw_line.startswith("-"):
            continue
        elif raw_line.startswith("\\"):
            continue
        else:
            line_number += 1
