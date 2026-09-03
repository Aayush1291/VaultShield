"""Security finding persistence.

design.md's "Backend apps" layout gives this app the `SecurityFinding`
model (Requirement 11.1). The static rule registry that produces findings
lands in `security_scan/rules.py` (Task 22.1) and the Celery task that
applies it to a Run's diff in `security_scan/tasks.py` (Task 22.3).

The model sits in this app rather than alongside `Run` in `runs/` because
it belongs to the scanner's bounded context: `runs/` owns what the CLI
uploaded, `security_scan/` owns what the backend derived from it. The
`related_name="security_findings"` reverse accessor is what lets the run
detail endpoint (Requirement 12.3) and the dashboard's Security tab
(Requirement 20.5) read findings back off a Run without this app's
internals leaking into `runs/`.
"""

from django.db import models

from runs.models import Run


class SecurityFinding(models.Model):
    """One rule match against a Run's diff.

    Produced in bulk by `run_security_scan` (Requirement 17.1) from static
    pattern matching over added lines only — the scanner never executes
    diff content (Requirement 17.3).
    """

    run = models.ForeignKey(
        Run, on_delete=models.CASCADE, related_name="security_findings"
    )
    # The registry key of the rule that matched ("hardcoded_secret",
    # "destructive_sql", ...). A plain CharField, not choices: the rule set
    # is defined in `rules.py` and grows there, and a stored finding must
    # stay readable even after its rule is renamed or retired.
    rule_id = models.CharField(max_length=64)
    file_path = models.CharField(max_length=500)
    # Nullable because not every rule matches at a single line — a
    # file-level finding has no meaningful line number, and 0 would read as
    # a real position.
    line_number = models.PositiveIntegerField(null=True)
    description = models.TextField()

    def __str__(self) -> str:
        return f"{self.rule_id} in {self.file_path}"
