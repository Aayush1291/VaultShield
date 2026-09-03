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

from rest_framework import mixins, viewsets

from projects.models import Project
from projects.serializers import ProjectSerializer


class ProjectViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """`POST /api/projects/` (create) and `GET /api/projects/` (list).

    ### Why two mixins instead of `ModelViewSet`

    `ModelViewSet` would also route retrieve, update, partial-update, and
    destroy. design.md's REST API surface gives `Project` exactly two
    operations — create (Requirement 12.1) and the list
    `ProjectListView` fetches (Requirement 19.1) — so composing the two
    mixins means the routed URLs match that table exactly instead of
    exposing four unspecified endpoints, including a `DELETE` that would
    cascade every Run, event, diff, screenshot, finding, and approval
    belonging to the Project. Adding an operation later is one mixin in
    this class declaration; un-shipping one that reviewers have started
    calling is not.

    ### Access control

    No `permission_classes` here on purpose. `settings.REST_FRAMEWORK`
    sets `TokenAuthentication` + `IsAuthenticated` as project-wide
    defaults (Task 15.2, Requirements 27.3-27.5), so both endpoints
    require a valid `Dashboard_Auth_Token` by inheritance. Requirement
    27.5 names project creation specifically. Restating the defaults would
    be harmless; overriding them is what would break the requirement, so
    the class stays silent on the subject and inherits.
    """

    # `Project.Meta.ordering` is `-created_at`, so the paginated list
    # (`PAGE_SIZE = 50`) has a stable page split without this viewset
    # sorting again.
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
