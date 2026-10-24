"""Project REST endpoints.

Task 16.1 — `ProjectViewSet` serves `POST /api/projects/`
(Requirement 12.1) plus the list the dashboard's `ProjectListView` reads
(Requirement 19.1). Validation of the create payload lives in
`projects/serializers.py` (Requirement 12.5).

The route is registered on the DRF router in `vaultshield_backend/urls.py`
rather than in a per-app `urls.py`: design.md's REST API surface is one
flat `/api/` table, and keeping the registrations in one file is what
makes that table checkable against the code in a single read.
"""

from bson import ObjectId
from django.core.files.storage import default_storage
from django_mongodb_backend import transaction
from rest_framework import mixins, viewsets
from rest_framework.exceptions import NotFound

from projects.models import Project
from projects.serializers import ProjectSerializer
from runs.models import Run, Screenshot
from vaultshield_backend.mongodb import retry_on_transient_transaction_error


class ProjectViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """`POST /api/projects/` (create), `GET /api/projects/` (list), and
    `DELETE /api/projects/:id/` (delete the Project and everything under it).

    ### Which operations are routed, and which are not

    `ModelViewSet` would also route retrieve, update, and partial-update.
    design.md's REST API surface gives `Project` create (Requirement 12.1)
    and the list `ProjectListView` fetches (Requirement 19.1); delete is the
    dashboard's "remove a project" control, which cascades the Project's
    Runs and every artifact under them. Composing `CreateModelMixin`,
    `ListModelMixin`, and `DestroyModelMixin` routes exactly those three
    verbs — `GET`/`POST` on the collection and `DELETE` on the detail URL —
    and leaves `GET`/`PUT`/`PATCH` on the detail URL unbound, so they answer
    `405` rather than exposing a full-replacement or read-detail endpoint
    nothing specified. A Project has no mutable field a `PATCH` would reach
    (`name` and `repo_url` identify the repository; changing either renames
    a different project), so no update verb is a design choice, not an
    omission.

    ### Deleting a Project removes its whole subtree, in one transaction

    `perform_destroy` leans on the schema's own cascade: every table hangs
    off `Run`, and `Run.project` plus every artifact FK is
    `on_delete=CASCADE`, so deleting the `Project` row deletes its Runs and
    with them every `RunEvent`, `RunDiff`, `Screenshot`, `Approval`,
    `SecurityFinding`, and `SandboxRecord`. That delete runs inside
    `django_mongodb_backend`'s transaction (Django's native
    `transaction.atomic` is a silent no-op on this backend; see
    `runs/views.py`) so the whole subtree disappears together or not at all,
    and it is retried on MongoDB's `TransientTransactionError` like every
    other transaction in the codebase.

    Django's cascade deletes `Screenshot` *rows* but not the image files
    those rows point at — `ImageField` deletion does not remove the file —
    so the file names are collected before the delete and unlinked from
    storage afterwards, best effort. See `perform_destroy`.

    ### Access control

    No `permission_classes` here on purpose. `settings.REST_FRAMEWORK`
    sets `TokenAuthentication` + `IsAuthenticated` as project-wide
    defaults (Task 15.2, Requirements 27.3-27.5), so every endpoint —
    including delete — requires a valid `Dashboard_Auth_Token` by
    inheritance, the same policy as list and create. Restating the defaults
    would be harmless; overriding them is what would break the requirement,
    so the class stays silent on the subject and inherits.
    """

    # `Project.Meta.ordering` is `-created_at`, so the paginated list
    # (`PAGE_SIZE = 50`) has a stable page split without this viewset
    # sorting again.
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer

    def get_object(self):
        """Resolve the `:id` to a Project, or 404 — never 500 on a bad id.

        A Project's pk is a MongoDB ObjectId (`ObjectIdAutoField`), so a
        `:id` that is not a 24-character hex string is not a value the driver
        can look up: letting it reach the queryset risks an error deep in
        `bson` rather than a clean miss. A syntactically invalid id names no
        Project, which is a `404` (`NotFound`), the same status a
        syntactically valid id that happens to match no row already gets from
        DRF's `get_object` via `DoesNotExist`. Checking up front collapses
        both "no such project" cases to one response.
        """
        lookup_value = self.kwargs.get(self.lookup_field)
        if not ObjectId.is_valid(lookup_value):
            raise NotFound("No Project matches the given query.")

        return super().get_object()

    def perform_destroy(self, instance):
        """Delete the Project, its Runs, every artifact, and the image files.

        The row deletes drive off the schema's cascade — `instance.delete()`
        collects and removes the Project's Runs and every dependent document
        (events, diff, screenshots, approval, security findings, sandbox
        records) because each FK back to `Run`, and `Run.project` itself, is
        `on_delete=CASCADE`. It runs inside `django_mongodb_backend`'s
        transaction so the subtree is removed atomically, retried on a
        transient abort like every other transaction here.

        The one thing the cascade does not do is unlink the screenshot image
        files: deleting a `Screenshot` row leaves its `ImageField` file on
        disk. So the file names are read *before* the delete — once the rows
        are gone the paths are unrecoverable — by two single-hop queries
        rather than a `Screenshot.objects.filter(run__project=...)` join,
        which keeps the lookup to the equality filters this backend handles
        plainly. The files are then removed after the rows commit, each in
        its own `try` so a file already missing (or a storage hiccup) cannot
        turn a succeeded delete into a `500`: the records are the source of
        truth, and a stray orphaned file is harmless next to a half-deleted
        Project.
        """
        run_ids = list(Run.objects.filter(project=instance).values_list("pk", flat=True))
        screenshot_names = list(
            Screenshot.objects.filter(run_id__in=run_ids)
            .values_list("image", flat=True)
        )

        def delete_project():
            with transaction.atomic():
                instance.delete()

        retry_on_transient_transaction_error(delete_project)

        for name in screenshot_names:
            if not name:
                continue
            try:
                default_storage.delete(name)
            except Exception:
                # Best effort: the Project and its rows are already gone, and
                # an orphaned image file is not worth failing the request over.
                pass
