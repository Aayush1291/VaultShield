"""Tests for the `projects` app.

Task 16.1's own checks live here: that `ProjectSerializer` accepts a valid
`POST /api/projects/` payload and rejects a malformed one
(Requirements 12.1, 12.5), and that the route is wired to the viewset with
exactly the two operations design.md's REST API surface gives `Project`.

These are deliberately database-free — they exercise the serializer
directly and inspect the resolved URL conf, neither of which opens a
connection. The endpoint-level assertions (a `Project` row persisted and
echoed, a rejected payload leaving the table untouched) are Properties 18
and 21, which are Tasks 16.3 and 16.6 and need a reachable MongoDB. Admin
registration for `Project` is Task 14.5.

Endpoint-level coverage against a real database now lives in
`vaultshield_backend/test_mongodb.py::TestRestApiAgainstMongoDB`.
"""

from django.urls import resolve, reverse

from projects.serializers import ProjectSerializer
from projects.views import ProjectViewSet
from vaultshield_backend.testing import routed_methods

VALID_PAYLOAD = {"name": "vaultshield", "repo_url": "git@github.com:acme/vaultshield.git"}


class TestProjectSerializerAcceptsValidPayload:
    """Requirement 12.1: a valid create payload is accepted as submitted."""

    def test_valid_payload_is_accepted(self):
        serializer = ProjectSerializer(data=VALID_PAYLOAD)

        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data == VALID_PAYLOAD

    def test_scp_style_and_https_remotes_are_both_accepted(self):
        """`repo_url` is not format-checked, so both remote forms pass.

        `git@host:org/repo.git` is not a URL any URL validator accepts,
        which is why `Project.repo_url` is a `CharField`. This pins that
        decision: a serializer change that started validating URL format
        would reject a repository the CLI clones fine.
        """
        for remote in (
            "git@github.com:acme/vaultshield.git",
            "https://github.com/acme/vaultshield.git",
            "/srv/git/vaultshield.git",
        ):
            serializer = ProjectSerializer(data={"name": "vaultshield", "repo_url": remote})

            assert serializer.is_valid(), serializer.errors

    def test_client_supplied_id_and_created_at_are_ignored(self):
        """`id`/`created_at` are read-only: a client cannot forge either."""
        serializer = ProjectSerializer(
            data={**VALID_PAYLOAD, "id": 4321, "created_at": "2001-01-01T00:00:00Z"}
        )

        assert serializer.is_valid(), serializer.errors
        assert "id" not in serializer.validated_data
        assert "created_at" not in serializer.validated_data


class TestProjectSerializerRejectsInvalidPayload:
    """Requirement 12.5: missing or invalid required fields are rejected.

    Each case asserts both that validation failed and that the error is
    attributed to the offending field, since "rejected with a validation
    error response" is what the client has to be able to act on.
    """

    def test_empty_payload_reports_both_required_fields(self):
        serializer = ProjectSerializer(data={})

        assert not serializer.is_valid()
        assert set(serializer.errors) == {"name", "repo_url"}

    def test_missing_name_is_rejected(self):
        serializer = ProjectSerializer(data={"repo_url": VALID_PAYLOAD["repo_url"]})

        assert not serializer.is_valid()
        assert "name" in serializer.errors

    def test_missing_repo_url_is_rejected(self):
        serializer = ProjectSerializer(data={"name": VALID_PAYLOAD["name"]})

        assert not serializer.is_valid()
        assert "repo_url" in serializer.errors

    def test_blank_and_whitespace_only_values_are_rejected(self):
        """A whitespace-only value must not become a stored blank field."""
        for bad_value in ("", "   ", "\t\n"):
            serializer = ProjectSerializer(data={**VALID_PAYLOAD, "name": bad_value})

            assert not serializer.is_valid(), f"accepted name={bad_value!r}"
            assert "name" in serializer.errors

    def test_null_values_are_rejected(self):
        for field in ("name", "repo_url"):
            serializer = ProjectSerializer(data={**VALID_PAYLOAD, field: None})

            assert not serializer.is_valid(), f"accepted {field}=None"
            assert field in serializer.errors

    def test_over_max_length_values_are_rejected(self):
        """Caught in validation, not as a database error at INSERT."""
        too_long = {"name": "n" * 256, "repo_url": "r" * 501}

        for field, value in too_long.items():
            serializer = ProjectSerializer(data={**VALID_PAYLOAD, field: value})

            assert not serializer.is_valid(), f"accepted over-length {field}"
            assert field in serializer.errors

    def test_non_string_structures_are_rejected(self):
        for bad_value in ({"nested": "object"}, ["list"]):
            serializer = ProjectSerializer(data={**VALID_PAYLOAD, "repo_url": bad_value})

            assert not serializer.is_valid(), f"accepted repo_url={bad_value!r}"
            assert "repo_url" in serializer.errors


class TestProjectRouteRegistration:
    """The router exposes `/api/projects/` and only the two specified verbs."""

    def test_collection_path_matches_the_design_api_surface(self):
        assert reverse("project-list") == "/api/projects/"

    def test_collection_path_resolves_to_the_viewset(self):
        match = resolve("/api/projects/")

        assert match.func.cls is ProjectViewSet
        assert routed_methods(match) == {"get": "list", "post": "create"}

    def test_no_update_or_destroy_operations_are_routed(self):
        """design.md gives `Project` create and list, nothing else.

        In particular no `DELETE`, which would cascade every Run and
        artifact belonging to the Project.
        """
        routed_actions = set(routed_methods(resolve("/api/projects/")))

        assert routed_actions == {"get", "post"}
        assert ProjectViewSet.get_extra_actions() == []
