"""Dashboard authentication.

Task 15.1 — `LoginView` serves `POST /api/auth/login/`, the one `/api/`
path that does not itself require a `Dashboard_Auth_Token`
(Requirements 27.1, 27.2). It is the endpoint `vaultshield login`
(Task 18.1) and the dashboard both call to exchange a username/password
credential for the token every other `/api/` request has to carry.

This app defines no models: design.md keeps Django's built-in
`django.contrib.auth.models.User`, and the `Dashboard_Auth_Token` is
`rest_framework.authtoken.models.Token`. Both apps are already in
`INSTALLED_APPS`, and `rest_framework.authtoken` ships its own migration
for the token table, so there is nothing to migrate from `accounts`.

### Why subclass `ObtainAuthToken` instead of routing `obtain_auth_token`

DRF exposes the same behaviour as a ready-made view function
(`obtain_auth_token = ObtainAuthToken.as_view()`), but it is configured
for a project whose defaults are DRF's own. Task 15.2 is about to make
`TokenAuthentication` + `IsAuthenticated` the project-wide default, and
the pre-built view function gives no place to opt out of that. Subclassing
does, which is what the two class attributes below are for.
"""

from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.permissions import AllowAny


class LoginView(ObtainAuthToken):
    """POST /api/auth/login/ — validates username/password and returns
    ``{"token": "<Dashboard_Auth_Token>"}``. Rejects invalid credentials
    with no token issued.

    The success and failure behaviour is inherited unchanged from
    `ObtainAuthToken`: `AuthTokenSerializer` runs the credential through
    `django.contrib.auth.authenticate`, so a bad username, a bad password,
    or an inactive user all fail serializer validation and come back as a
    400 with the error detail and no `token` key (Requirement 27.2).
    Only a validated credential reaches `Token.objects.get_or_create`,
    which is what makes "rejects invalid credentials with no token issued"
    true by construction rather than by a check in this class.

    Both class attributes below exist so this endpoint survives Task 15.2,
    which sets `DEFAULT_AUTHENTICATION_CLASSES`/`DEFAULT_PERMISSION_CLASSES`
    globally (Requirements 27.3-27.5). Without them login would require the
    very token it exists to hand out.
    """

    # `ObtainAuthToken` already ships `permission_classes = ()`, which has
    # the same effect, but an empty tuple reads like an oversight next to
    # a global `IsAuthenticated` default. Naming `AllowAny` states the
    # exemption instead of implying one.
    permission_classes = [AllowAny]

    # The important one. `permission_classes` is not enough on its own:
    # `TokenAuthentication` rejects a malformed or unknown token from
    # inside the authenticator, before any permission class is consulted.
    # So once 15.2 makes it the default, a client retrying login with a
    # stale token still in its request headers would get 401 instead of a
    # fresh token. Authenticating a request whose entire purpose is to
    # establish identity from a username and password is meaningless, so
    # this endpoint runs no authenticators at all and reads the credential
    # from the request body only.
    authentication_classes = []
