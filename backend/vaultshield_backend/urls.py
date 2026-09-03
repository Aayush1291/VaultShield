"""Root URL configuration for the VaultShield backend.

Mounts the Django admin (the surface Requirement 11.3 / Task 14.4 is
checked through), dashboard login, and the router-registered `/api/`
viewsets.

The rest of the `/api/` surface from design.md's "REST API surface" table
is added by the tasks that implement the views behind it, so that a route
never exists pointing at a view that does not:

    POST   /api/projects/                 projects.views    (Task 16.1)
    GET    /api/projects/                 projects.views    (Task 16.1)
    POST   /api/runs/                     runs.views        (Task 16.2)
    GET    /api/runs/:id/                 runs.views        (Task 16.2)
    PATCH  /api/runs/:id/                 runs.views        (Task 16.2)
    GET    /api/runs/?status=&submitted=  runs.views        (Task 16.10)
    POST   /api/runs/:id/events/          runs.views        (Task 16.7)
    POST   /api/runs/:id/diff/            runs.views        (Task 16.7)
    POST   /api/runs/:id/screenshots/     runs.views        (Task 16.7)
    POST   /api/runs/:id/approve/         runs.views        (Task 28.1)
    POST   /api/runs/:id/reject/          runs.views        (Task 28.1)

Registering a viewset does not by itself route every verb in that table:
the router binds only the actions the viewset actually defines, so a verb
appears as its mixin or `@action` method is written — `GET /api/runs/`
arrived with Task 16.10's filtered list, and `POST /api/runs/:id/approve/`
and `.../reject/` with Task 28.1's decision actions, which completes the
table above.

Uploaded screenshots are served straight off `MEDIA_ROOT` while `DEBUG` is
on. That is Django's development-only file serving and it is intentionally
conditional: in the compose stack the `web` service is fronted by
gunicorn, which does not serve media, so a deployed setup has to route
`MEDIA_URL` itself.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.views import LoginView
from projects.views import ProjectViewSet
from runs.views import RunViewSet

# One router for the whole `/api/` viewset surface, which is the point of a
# shared router: the collection paths in design.md's REST API table are all
# visible in one place instead of scattered across per-app `urls.py` files.
#
# `DefaultRouter` over `SimpleRouter` for the browsable index it adds at
# `/api/` — useful for hand-checking endpoints during the build, which is
# also why `BrowsableAPIRenderer` is enabled while `DEBUG` is on. The
# index lists only router-registered routes, so `POST /api/auth/login/`
# does not appear in it; that is cosmetic, and the index requires a
# `Dashboard_Auth_Token` like every other `/api/` path.
router = DefaultRouter()
# `basename` is stated rather than derived from `queryset` so the reverse
# names (`project-list`) stay fixed if `ProjectViewSet` ever swaps its
# `queryset` attribute for a `get_queryset()` override — which is what
# Task 16.10 did to `RunViewSet`.
router.register(r"projects", ProjectViewSet, basename="project")
# `RunViewSet` sets `lookup_field = "run_id"`, so the detail route is
# `/api/runs/<run_id>/` — the CLI-generated id, not the database pk, which
# is what design.md's `upload_run`/`submit` signatures address. `basename`
# is required here rather than merely tidy: Task 16.10 replaced the
# viewset's `queryset` attribute with a `get_queryset()` override for the
# `status`/`submitted` filters, and a derived basename reads that attribute.
router.register(r"runs", RunViewSet, basename="run")

urlpatterns = [
    path("admin/", admin.site.urls),
    # The exact path is part of the contract: `vaultshield login`
    # (Task 18.1) and the dashboard's API client both post here, and it is
    # the single path Task 15.2's global token requirement has to exempt.
    # Trailing slash included to match Django's `APPEND_SLASH` convention
    # used by every other route in the table above.
    #
    # Listed before the router include so this exact path is matched by
    # its own view: Django resolves top to bottom, and nothing routed
    # under the `api/` prefix below should be able to shadow login.
    path("api/auth/login/", LoginView.as_view(), name="login"),
    path("api/", include(router.urls)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
