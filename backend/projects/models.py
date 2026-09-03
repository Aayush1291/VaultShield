"""Project persistence.

design.md's "Backend apps" layout gives this app exactly one model:
`Project`, the repository a Run belongs to (Requirement 11.1).

`Project` is the only model in the MVP schema with no owner, team, or
tenant field. That is deliberate rather than unfinished: Requirement 26
scopes the MVP to a single developer's machine, and design.md's Overview
enforces that boundary structurally by never adding multi-tenancy columns
to the data model. Sharing a dashboard between developers is a post-MVP
schema change, not a missing nullable field.
"""

from django.db import models


class Project(models.Model):
    """A repository VaultShield runs agents against.

    Created through `POST /api/projects/` (Requirement 12.1) and listed by
    the dashboard's `ProjectListView` (Requirement 19.1).
    """

    name = models.CharField(max_length=255)
    # A plain CharField, not a URLField: the CLI clones whatever remote the
    # developer configured, and `git@github.com:org/repo.git` is a valid
    # remote but not a valid URL, so URL validation would reject working
    # repositories.
    repo_url = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # The API list endpoints paginate by default
        # (`DEFAULT_PAGINATION_CLASS` in settings), and paginating an
        # unordered queryset gives an arbitrary, unstable page split. A
        # model-level default ordering makes every list view deterministic
        # without each viewset having to remember to sort. Newest first,
        # because that is what a dashboard listing wants.
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name
