"""Django admin registration for the run models.

Five of Requirement 11.3's eight models live in this app — `Run` plus the
four artifact tables that hang off it — so this file carries the bulk of
the admin surface. `Project`, `SecurityFinding`, and `SandboxRecord` are
registered in their own apps' `admin.py`.

Each model is registered with a plain `admin.site.register(...)` call, as
design.md specifies, rather than through a `ModelAdmin`. Registration
order below follows the data model's own dependency order (`Run` first,
then the tables that reference it) purely for readability — the admin
registry is a dict keyed by model, so order has no runtime effect.

Note that the timestamp fields on these models (`Run.created_at`,
`Run.updated_at`, `Approval.decided_at`) are `auto_now_add`/`auto_now` and
therefore non-editable, so Django omits them from the admin change forms
while still showing them on the detail pages of related objects. That is
the correct behaviour here: `Run.updated_at` is the clock
`flag_stuck_approvals` (Task 33.2) selects on, and hand-editing it through
the admin would corrupt that query's input.
"""

from django.contrib import admin

from runs.models import Approval, Run, RunDiff, RunEvent, Screenshot

admin.site.register(Run)
admin.site.register(RunEvent)
admin.site.register(RunDiff)
admin.site.register(Screenshot)
admin.site.register(Approval)
