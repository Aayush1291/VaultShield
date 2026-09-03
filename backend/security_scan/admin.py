"""Django admin registration for `SecurityFinding`.

The seventh of Requirement 11.3's eight models, registered with the plain
`admin.site.register(...)` call design.md specifies.

Findings are written in bulk by `run_security_scan` (Task 22.3), never
typed in by hand, so this registration exists to make the scanner's output
inspectable while the rule registry is being built (Task 22.1) — being
able to see which `rule_id` fired on which `file_path` without going
through the dashboard's Security tab (Requirement 20.5).
"""

from django.contrib import admin

from security_scan.models import SecurityFinding

admin.site.register(SecurityFinding)
