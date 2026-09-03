"""Credentials_Module: shape classification and dummy generation.

`credentials.py` had no test module before this one, which is how F-5 went
unnoticed: nothing pinned what `generate_dummy` did to a value that was neither
an http(s) URL nor a long alphanumeric key, so every connection string was
quietly collapsed to the literal `dummy-value`.

Two invariants are asserted for every case, because together they are the whole
contract:

    STRUCTURE  the dummy is shaped like the real value (Requirement 3.2), so an
               application in the Sandbox can parse it and start;
    SECRECY    the dummy contains no part of the real value (Requirement 3.3 /
               design.md's Property 5), so nothing real reaches the Sandbox.

Either one alone is easy and useless: `dummy-value` is perfectly secret and
structurally wrong, and echoing the input is perfectly structured and a leak.
"""

from __future__ import annotations

import pytest

from vaultshield.credentials import (
    _DUMMY_PASSWORD,
    _DUMMY_USER,
    _GENERIC_PLACEHOLDER,
    classify_shape,
    generate_dummy,
)

# (value, expected scheme). One per scheme F-5 names, each carrying an obviously
# fake secret so the secrecy assertion has something to detect.
CONNECTION_STRINGS = [
    ("postgresql://user:PGSECRET@localhost:5432/db", "postgresql"),
    ("postgres://user:PGSECRET@db.internal:5432/app?sslmode=require", "postgres"),
    ("mongodb://admin:MONGOSECRET@mongo:27017/mydb", "mongodb"),
    (
        "mongodb+srv://admin:MONGOSECRET@cluster0.abcd.mongodb.net/?retryWrites=true",
        "mongodb+srv",
    ),
    ("mysql://root:MYSQLSECRET@127.0.0.1:3306/shop", "mysql"),
    ("mariadb://root:MARIASECRET@127.0.0.1:3306/shop", "mariadb"),
    ("redis://:REDISSECRET@redis:6379/0", "redis"),
    ("redis+tls://user:REDISSECRET@redis:6380/1", "redis+tls"),
    ("rediss://user:REDISSECRET@redis:6380/1", "rediss"),
    ("amqp://guest:AMQPSECRET@rabbit:5672/vhost", "amqp"),
    ("amqps://guest:AMQPSECRET@rabbit:5671/vhost", "amqps"),
    # http(s) count as connection strings ONLY when they carry userinfo, because
    # that is what makes them a credential rather than an endpoint.
    ("http://user:HTTPSECRET@internal:8080/hook", "http"),
    ("https://user:HTTPSSECRET@api.example.com/v1", "https"),
]

SECRET_FRAGMENTS = [
    "PGSECRET",
    "MONGOSECRET",
    "MYSQLSECRET",
    "MARIASECRET",
    "REDISSECRET",
    "AMQPSECRET",
    "HTTPSECRET",
    "HTTPSSECRET",
]


class TestConnectionStringShapes:
    """F-5: every supported scheme keeps its structure and loses its secret."""

    @pytest.mark.parametrize("value,scheme", CONNECTION_STRINGS)
    def test_is_classified_as_a_connection_string(self, value, scheme):
        assert classify_shape(value) == "connection_string"

    @pytest.mark.parametrize("value,scheme", CONNECTION_STRINGS)
    def test_scheme_is_preserved(self, value, scheme):
        """An application dispatches its driver on the scheme, so changing it
        makes the value unusable even though it still looks like a URL."""
        assert generate_dummy(value).startswith(f"{scheme}://")

    @pytest.mark.parametrize("value,scheme", CONNECTION_STRINGS)
    def test_result_is_still_a_parseable_connection_string(self, value, scheme):
        dummy = generate_dummy(value)
        assert "://" in dummy
        # Userinfo is always present in the output, which is what makes the
        # result parseable by a driver expecting credentials.
        assert f"{_DUMMY_USER}:{_DUMMY_PASSWORD}@" in dummy
        assert dummy != _GENERIC_PLACEHOLDER

    @pytest.mark.parametrize("value,scheme", CONNECTION_STRINGS)
    def test_no_secret_fragment_survives(self, value, scheme):
        dummy = generate_dummy(value)
        for fragment in SECRET_FRAGMENTS:
            if fragment in value:
                assert fragment not in dummy, f"{fragment} leaked into {dummy}"

    @pytest.mark.parametrize("value,scheme", CONNECTION_STRINGS)
    def test_the_whole_value_is_never_a_substring_of_the_dummy(self, value, scheme):
        """design.md's Property 5, restated for this shape."""
        assert value not in generate_dummy(value)

    def test_host_port_and_path_are_preserved(self):
        """The parts that are structural but not secret are kept, so the Sandbox
        talks to the same place with different credentials."""
        dummy = generate_dummy("postgresql://u:pw@db.internal:5432/appdb?sslmode=require")
        assert dummy == (
            f"postgresql://{_DUMMY_USER}:{_DUMMY_PASSWORD}@db.internal:5432/appdb"
            "?sslmode=require"
        )

    def test_the_username_is_replaced_too_not_just_the_password(self):
        """A username can itself identify an account, and Requirement 3.3 is
        about the whole credential rather than only its password half."""
        dummy = generate_dummy("mongodb://realadminname:MONGOSECRET@mongo:27017/db")
        assert "realadminname" not in dummy
        assert _DUMMY_USER in dummy

    def test_a_connection_string_without_userinfo_still_gets_dummy_userinfo(self):
        """Otherwise every part would be preserved and the output would echo the
        input verbatim, breaking Property 5 for the one shape where nothing had
        to be replaced."""
        value = "redis://localhost:6379/0"
        dummy = generate_dummy(value)
        assert value not in dummy
        assert dummy == f"redis://{_DUMMY_USER}:{_DUMMY_PASSWORD}@localhost:6379/0"

    def test_dummy_values_are_deterministic_for_connection_strings(self):
        """Unlike the random `key` shape: there is nothing to randomise here, and
        a stable value keeps a re-run's `.env.sandbox` diffable."""
        value = "postgresql://u:pw@h:5432/d"
        assert generate_dummy(value) == generate_dummy(value)


class TestEmptyValues:
    """An empty value stays empty."""

    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_empty_stays_empty(self, value):
        """Substituting a placeholder turns "unset" into "set" and changes which
        branch an application takes — `if not os.environ.get("X")` behaves
        differently in the Sandbox than outside it. Emptiness IS the structural
        shape here, and an empty value has no secret to replace."""
        assert generate_dummy(value) == value


class TestPreexistingShapesAreUnchanged:
    """The shapes that already worked must keep working exactly as before.

    F-5 added a branch; it must not have redirected the existing ones.
    """

    def test_a_plain_http_url_keeps_the_fixed_dummy_url(self):
        """No userinfo means no credential, so an endpoint stays an endpoint."""
        assert classify_shape("https://example.com/api") == "url"
        assert generate_dummy("https://example.com/api") == "https://example.invalid/dummy"

    def test_a_long_token_is_classified_as_a_key(self):
        assert classify_shape("sk_test_abcdef1234567890") == "key"

    def test_a_key_dummy_is_random_same_length_and_leaks_nothing(self):
        value = "sk_test_abcdef1234567890"
        first, second = generate_dummy(value), generate_dummy(value)
        assert len(first) == len(value)
        assert first != second, "key dummies are freshly random per call"
        assert value not in first

    def test_an_unrecognised_value_gets_the_generic_placeholder(self):
        assert classify_shape("not a url or a key") == "generic"
        assert generate_dummy("not a url or a key") == _GENERIC_PLACEHOLDER

    def test_a_bare_hostport_is_not_mistaken_for_a_connection_string(self):
        """No scheme, so there is no structure to preserve."""
        assert classify_shape("localhost:5432") == "generic"

    def test_an_unsupported_scheme_falls_through_to_generic(self):
        """Only the schemes F-5 enumerates are rebuilt. Anything else has no
        known structure, so guessing at one would be worse than a placeholder."""
        assert classify_shape("ldap://dc.example.com/dc=example") == "generic"
        assert generate_dummy("ldap://x:y@h/z") == _GENERIC_PLACEHOLDER
