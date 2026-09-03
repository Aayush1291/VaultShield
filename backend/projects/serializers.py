"""Serialization and validation for `Project`.

Task 16.1 — the request/response shape behind `POST /api/projects/`
(Requirement 12.1) and the validation that rejects a malformed create
request (Requirement 12.5, Property 21).

### Why the validation rules are derived from the model, not restated here

`ProjectSerializer` is a plain `ModelSerializer` with no hand-written
`validate_*` methods. That is the whole implementation of Requirement
12.5, not a gap in it: `ModelSerializer` builds each field from the model
column, so the constraints already declared on `Project` become request
validation automatically, and there is exactly one place they are defined.

For the two writable fields (`name`, `repo_url`, both
`models.CharField` with no `blank=True`) that yields:

- **missing** — the key absent from the payload is a `required` error.
- **null** — `None` is rejected; neither column is nullable.
- **blank** — `""` is rejected, and so is a whitespace-only string:
  DRF's `CharField` strips before the blank check (`trim_whitespace`
  defaults to `True`), so `"   "` is treated as blank rather than stored
  as a `Project` with an invisible name.
- **too long** — over `max_length` (255 / 500) is rejected up front
  rather than at `INSERT`, where PostgreSQL would raise a
  `DataError` and surface as a 500 instead of a validation error.
- **wrong type** — a dict, list, or bool is rejected; `CharField` only
  coerces genuine scalars.

Each of those comes back as DRF's standard 400 with a per-field error
detail, and because DRF validates before `create()` runs, a rejected
request performs no write — the "SHALL NOT create any record" half of
Property 21 holds by ordering, not by a check in this file.

Restating any of it here (an explicit `required=True`, a length
validator) would create a second source of truth that could silently
drift from the column it mirrors.

### What is deliberately not validated

`repo_url` is not format-checked. `Project.repo_url` is a `CharField`
rather than a `URLField` precisely because `git@github.com:org/repo.git`
is a remote the CLI clones happily but not a URL any URL validator
accepts, and the spec fixes no grammar for the field. Inventing one here
would reject working repositories to satisfy a rule nothing asked for.

One consequence worth naming, since it borders on Requirement 22.5: a
developer *can* post a credential-embedding remote such as
`https://user:token@host/org/repo.git`, and it would be stored as typed.
Requirement 22.5 is about the schema never carrying a
repository-hosting access token as a field of its own, which holds — no
token field exists on `Project` and none is added below. Screening
credentials out of the URL text itself is a separate rule the spec does
not define, so it is left alone rather than half-enforced here.
"""

from rest_framework import serializers

from projects.models import Project


class ProjectSerializer(serializers.ModelSerializer):
    """The `Project` representation `POST /api/projects/` accepts and returns.

    The same serializer renders the list `GET /api/projects/` that the
    dashboard's `ProjectListView` reads (Requirement 19.1), so what a
    client submits and what it later reads back are the same four fields
    by construction.
    """

    class Meta:
        model = Project
        # Every column on the model, and nothing else. Listing the fields
        # explicitly rather than using `__all__` means a column added to
        # `Project` later has to be opted in to the API surface
        # deliberately — relevant to Requirement 22.5, which forbids the
        # backend schema from carrying a repository-hosting access token:
        # there is no token field here, and `__all__` is exactly the
        # mechanism that would quietly expose one if a future migration
        # ever added it.
        fields = ["id", "name", "repo_url", "created_at"]
        # Both are already read-only by derivation (`id` is the primary
        # key, `created_at` is `auto_now_add`), so this pins existing
        # behaviour rather than changing it. Stated because it is part of
        # the contract: a client cannot choose a Project's id or forge its
        # creation time, and a payload that tries to is ignored rather
        # than honoured.
        read_only_fields = ["id", "created_at"]
