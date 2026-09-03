"""Shared test helpers.

Kept out of any app package because the helpers here are about Django/DRF
mechanics rather than about one app's data.
"""

from __future__ import annotations

# DRF aliases HEAD onto GET, and does it by *mutating* the `actions` dict the
# router handed the view — `rest_framework/viewsets.py`:
#
#     if 'get' in actions and 'head' not in actions:
#         actions['head'] = actions['get']
#
# That line runs inside the view closure, so it fires on the first *dispatched
# request* to the route rather than when the URLconf is built. The router keeps
# a reference to the same dict, so `resolve(path).func.actions` gains a `head`
# entry once any request has reached that route in the current process — and
# does not have it otherwise.
#
# Route-registration assertions comparing that dict for exact equality are
# therefore order-dependent: they pass in a suite that never issues a request
# and fail in one that does. That is a property of DRF, not of the routing under
# test, so this helper drops the alias and lets those assertions say what they
# mean — which HTTP methods are routed to which viewset action.
HEAD_ALIAS_METHOD = "head"


def routed_methods(resolver_match) -> dict[str, str]:
    """The `{method: action}` map for a resolved route, without DRF's HEAD alias.

    Args:
        resolver_match: the return value of `django.urls.resolve()`.

    Returns:
        A copy, so callers cannot mutate the router's own dict — the exact
        hazard documented above.
    """
    actions = dict(resolver_match.func.actions)
    if actions.get(HEAD_ALIAS_METHOD) == actions.get("get"):
        actions.pop(HEAD_ALIAS_METHOD, None)
    return actions
