"""Django admin registration for `Project`.

Requirement 11.3 names eight models that must be reachable through the
Django admin. This file contributes the first of them; the other seven are
registered in `runs/admin.py` (five), `security_scan/admin.py`, and
`sandbox_tracking/admin.py`.

design.md specifies the mechanism literally — "All models are registered
in `admin.py` for each app via `admin.site.register(...)`" — so the bare
two-argument-less form is used throughout rather than `@admin.register`
plus a `ModelAdmin`. The admin here is an inspection and repair surface
for a single developer's own data, not a product surface; `__str__` on
each model already gives every changelist a readable row, so a
`ModelAdmin` would be configuration with nothing to configure.
"""

from django.contrib import admin

from projects.models import Project

admin.site.register(Project)
