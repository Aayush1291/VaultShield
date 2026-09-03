"""Django admin registration for `SandboxRecord`.

design.md calls this file out by name: all models are registered via
`admin.site.register(...)`, "including `SandboxRecord` in
`sandbox_tracking/admin.py`". This is the eighth and last of Requirement
11.3's eight models.

The explicit mention is worth honouring rather than treating as
incidental. `SandboxRecord` is the one model in the schema with no
dashboard view and no REST endpoint — its only programmatic consumer is
`cleanup_orphaned_sandboxes` (Task 33.1). The admin is therefore the only
way a developer can see what the reaper sees: which containers are still
claimed `active`, how stale their `last_heartbeat_at` is, and which ones
have a `torn_down_at` recorded. `last_heartbeat_at` is `auto_now_add` and
so non-editable in the change form, which is deliberate — the TTL clock
must only advance on a real heartbeat, never on an admin save.
"""

from django.contrib import admin

from sandbox_tracking.models import SandboxRecord

admin.site.register(SandboxRecord)
