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
    _DUMMY_HOST,
    _DUMMY_PASSWORD,
    _DUMMY_USER,
    _GENERIC_PLACEHOLDER,
    _is_preservable_host,
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

    def test_port_and_path_are_preserved_for_a_local_host(self):
        """Scheme, port and path/query are structural and identify nothing, so
        they are kept; a local host is kept with them.

        REPLACES an earlier version of this test that used `db.internal` and
        asserted that host survived verbatim. That expectation was the F-5 leak,
        not a requirement: it encoded "a host is not a credential", which is how a
        real Atlas cluster hostname reached `.env.sandbox`. The non-local half of
        the old case now lives in
        `test_an_internal_looking_dns_host_is_still_replaced`, so the coverage the
        old test intended (structure survives) is kept and only the wrong claim
        about hosts is dropped.
        """
        dummy = generate_dummy("postgresql://u:pw@10.0.0.5:5432/appdb?sslmode=require")
        assert dummy == (
            f"postgresql://{_DUMMY_USER}:{_DUMMY_PASSWORD}@10.0.0.5:5432/appdb"
            "?sslmode=require"
        )

    def test_an_internal_looking_dns_host_is_still_replaced(self):
        """The other half of the old `db.internal` case, with the correct
        expectation: structure survives, the hostname does not."""
        dummy = generate_dummy("postgresql://u:pw@db.internal:5432/appdb?sslmode=require")
        assert dummy == (
            f"postgresql://{_DUMMY_USER}:{_DUMMY_PASSWORD}@{_DUMMY_HOST}:5432/appdb"
            "?sslmode=require"
        )
        assert "db.internal" not in dummy

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


# --- host preservation policy (F-5 follow-up) --------------------------------
#
# The original F-5 fix preserved the host unconditionally, reasoning that a host
# is not a credential. That leaked: a real MongoDB Atlas URI's cluster hostname
# reached `.env.sandbox` verbatim, handing the sandboxed agent the identity of
# real infrastructure and contradicting Requirement 3.3.
#
# The policy is now an allowlist. A host is REPLACED unless it is provably local,
# so a host that nobody thought about is replaced rather than leaked.

# Hosts kept, each of which identifies nothing outside the developer's own
# machine or private network. `[::1]` is bracketed because RFC 3986 requires that
# form inside a URI; `_is_preservable_host` is unit-tested on the bare `::1`
# below.
PRESERVED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "10.0.0.5", "172.16.0.1", "192.168.1.20"]

# Hosts replaced. `0.0.0.0` is a wildcard bind address rather than a destination
# and is the easiest of these to get wrong, because it looks local and several
# "is this private?" predicates accept it.
REPLACED_HOSTS = [
    "0.0.0.0",
    "8.8.8.8",
    "cluster0.abcde.mongodb.net",
    "db.company.internal",
    "myapp.rds.amazonaws.com",
]

# (scheme, port_suffix, rest) — one row per scheme F-5 names, so each is checked
# with both a preserved and a replaced host.
SCHEME_CASES = [
    ("postgresql", ":5432", "/appdb?sslmode=require"),
    ("redis", ":6379", "/0"),
    ("mongodb", ":27017", "/mydb"),
    ("mongodb+srv", "", "/?retryWrites=true"),
    ("https", ":8443", "/v1?key=value"),
]

_REAL_USER = "realadminname"
_REAL_PASSWORD = "REALSECRETVALUE"


def _assert_structure_and_secrecy(dummy, *, scheme, port_suffix, rest, original_host, expected_host):
    """Both halves of the contract, for every case.

    STRUCTURE: scheme, port and path/query survive, so the value still parses and
    an application in the Sandbox still starts.
    SECRECY: the original host, username and password appear nowhere in the
    output.
    """
    assert dummy == f"{scheme}://{_DUMMY_USER}:{_DUMMY_PASSWORD}@{expected_host}{port_suffix}{rest}"
    # Structure, asserted independently of the equality above so a future change
    # to the dummy userinfo cannot quietly drop these.
    assert dummy.startswith(f"{scheme}://")
    if port_suffix:
        assert port_suffix in dummy
    assert dummy.endswith(rest)
    # Secrecy.
    assert _REAL_USER not in dummy
    assert _REAL_PASSWORD not in dummy
    if original_host != expected_host:
        assert original_host not in dummy


class TestHostIsPreservedOnlyWhenProvablyLocal:
    """Requirement 3.3 applied to the host, not just the userinfo."""

    @pytest.mark.parametrize("host", PRESERVED_HOSTS)
    def test_a_local_host_is_preserved(self, host):
        """Loopback, localhost and RFC 1918 literals name nothing outside the
        developer's own machine or private network, so only their shape is
        information and keeping them leaks nothing."""
        value = f"postgresql://{_REAL_USER}:{_REAL_PASSWORD}@{host}:5432/appdb?sslmode=require"
        _assert_structure_and_secrecy(
            generate_dummy(value),
            scheme="postgresql",
            port_suffix=":5432",
            rest="/appdb?sslmode=require",
            original_host=host,
            expected_host=host,
        )

    @pytest.mark.parametrize("host", REPLACED_HOSTS)
    def test_a_non_local_host_is_replaced(self, host):
        """Everything else, whether it is a public address or a hostname that
        merely sounds internal."""
        value = f"postgresql://{_REAL_USER}:{_REAL_PASSWORD}@{host}:5432/appdb?sslmode=require"
        _assert_structure_and_secrecy(
            generate_dummy(value),
            scheme="postgresql",
            port_suffix=":5432",
            rest="/appdb?sslmode=require",
            original_host=host,
            expected_host=_DUMMY_HOST,
        )

    def test_the_wildcard_bind_address_is_replaced_not_preserved(self):
        """`0.0.0.0` called out on its own because it is the one case most likely
        to be waved through: it looks local, and `ipaddress.is_private` treats
        0.0.0.0/8 as private. It is a wildcard bind address, not a destination, so
        it is not evidence that anything is local."""
        value = f"postgresql://{_REAL_USER}:{_REAL_PASSWORD}@0.0.0.0:5432/appdb"
        dummy = generate_dummy(value)
        assert dummy == f"postgresql://{_DUMMY_USER}:{_DUMMY_PASSWORD}@{_DUMMY_HOST}:5432/appdb"
        assert "0.0.0.0" not in dummy
        assert _is_preservable_host("0.0.0.0") is False

    @pytest.mark.parametrize("scheme,port_suffix,rest", SCHEME_CASES)
    def test_every_scheme_preserves_a_local_host(self, scheme, port_suffix, rest):
        """The policy is a property of the host, not of the scheme."""
        value = f"{scheme}://{_REAL_USER}:{_REAL_PASSWORD}@192.168.1.20{port_suffix}{rest}"
        _assert_structure_and_secrecy(
            generate_dummy(value),
            scheme=scheme,
            port_suffix=port_suffix,
            rest=rest,
            original_host="192.168.1.20",
            expected_host="192.168.1.20",
        )

    @pytest.mark.parametrize("scheme,port_suffix,rest", SCHEME_CASES)
    def test_every_scheme_replaces_a_remote_host(self, scheme, port_suffix, rest):
        """Same, for a host that must not survive under any scheme."""
        host = "myapp.rds.amazonaws.com"
        value = f"{scheme}://{_REAL_USER}:{_REAL_PASSWORD}@{host}{port_suffix}{rest}"
        _assert_structure_and_secrecy(
            generate_dummy(value),
            scheme=scheme,
            port_suffix=port_suffix,
            rest=rest,
            original_host=host,
            expected_host=_DUMMY_HOST,
        )

    def test_the_atlas_cluster_hostname_that_leaked_no_longer_survives(self):
        """The exact regression. A real `.env` carrying a MongoDB Atlas URI put
        its cluster hostname into `.env.sandbox` unchanged, which is how this was
        found. The hostname identifies the provider, the cluster and effectively
        the account."""
        value = (
            f"mongodb+srv://{_REAL_USER}:{_REAL_PASSWORD}"
            "@cluster0.abcde.mongodb.net/mydb?retryWrites=true&w=majority"
        )
        dummy = generate_dummy(value)
        assert dummy == (
            f"mongodb+srv://{_DUMMY_USER}:{_DUMMY_PASSWORD}@{_DUMMY_HOST}"
            "/mydb?retryWrites=true&w=majority"
        )
        # No fragment of the hostname survives, not just the whole string.
        for fragment in ("cluster0", "abcde", "mongodb.net", "cluster0.abcde.mongodb.net"):
            assert fragment not in dummy
        assert _REAL_USER not in dummy and _REAL_PASSWORD not in dummy
        # Structure intact: an Atlas driver still accepts this.
        assert dummy.startswith("mongodb+srv://")
        assert dummy.endswith("/mydb?retryWrites=true&w=majority")

    def test_a_dns_name_is_never_preserved_however_internal_it_sounds(self):
        """"Sounds internal" is not a security property. An attacker-visible
        difference between these two would itself be a disclosure, so they are
        treated identically."""
        internal = generate_dummy(f"postgresql://{_REAL_USER}:{_REAL_PASSWORD}@db.company.internal:5432/d")
        public = generate_dummy(f"postgresql://{_REAL_USER}:{_REAL_PASSWORD}@db.company.com:5432/d")
        assert internal == public
        assert "company" not in internal


class TestPreservableHostAllowlist:
    """`_is_preservable_host` directly, including boundaries a URI-level test
    cannot reach and the range edges most likely to be got wrong."""

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "LOCALHOST",  # case-insensitive
            "127.0.0.1",
            "127.255.255.254",  # 127.0.0.0/8 is loopback in full, not just .0.0.1
            "::1",  # bare, as the policy states it
            "[::1]",  # bracketed, as a URI must spell it
            "0:0:0:0:0:0:0:1",  # unabbreviated spelling of ::1
            "10.0.0.5",
            "10.255.255.254",
            "172.16.0.1",
            "172.31.255.254",  # top of 172.16.0.0/12
            "192.168.1.20",
        ],
    )
    def test_allowed(self, host):
        assert _is_preservable_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",  # wildcard bind address
            "8.8.8.8",
            "172.15.0.1",  # just below 172.16.0.0/12
            "172.32.0.1",  # just above 172.16.0.0/12
            "192.167.0.1",  # near-miss on 192.168.0.0/16
            "192.169.0.1",
            "11.0.0.1",  # near-miss on 10.0.0.0/8
            "126.0.0.1",  # near-miss on 127.0.0.0/8
            "128.0.0.1",
            "169.254.1.1",  # link-local: private per stdlib, not per this policy
            "100.64.0.1",  # carrier-grade NAT: same
            "fc00::1",  # IPv6 ULA: private but not reviewed, so replaced
            "2001:4860:4860::8888",
            "cluster0.abcde.mongodb.net",
            "db.company.internal",
            "myapp.rds.amazonaws.com",
            "localhost.evil.com",  # prefix must not be enough
            "notlocalhost",
            "10.0.0.5.evil.com",  # an IP-looking prefix on a DNS name
            "",
        ],
    )
    def test_rejected(self, host):
        assert _is_preservable_host(host) is False
