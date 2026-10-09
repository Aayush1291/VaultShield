"""Unit tests for `detect_framework`/`write_config` (Task 1.2) and
`load_config`/`ConfigError` (Task 1.4).

Covers each framework signature detected in isolation, the no-signature
case, and that `write_config` always produces valid YAML with all required
keys including placeholders. The property-based tests for framework
detection and for `load_config` (Tasks 1.3, 1.5, 1.6, 1.7) are separate,
optional tasks and are intentionally not included here — this file only
covers the fixed-example/edge-case unit tests for both functions.
"""

from pathlib import Path

import pytest
import yaml

from vaultshield.config import ConfigError, ProjectConfig, detect_framework, load_config, write_config


# --- detect_framework: individual signatures -------------------------------


def test_detect_framework_no_signature_yields_empty_fields(tmp_path: Path):
    result = detect_framework(tmp_path)
    assert result == {"start_command": "", "port": "", "ready_path": ""}


def test_detect_framework_django(tmp_path: Path):
    (tmp_path / "manage.py").write_text("")
    result = detect_framework(tmp_path)
    assert result["start_command"] == "python manage.py runserver 0.0.0.0:8000"
    assert result["port"] == 8000
    assert result["ready_path"] == "/"


def test_detect_framework_node_with_start_script(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"start": "node index.js"}}'
    )
    result = detect_framework(tmp_path)
    assert result["start_command"] == "npm start"
    assert result["port"] == 3000
    assert result["ready_path"] == "/"


def test_detect_framework_node_with_dev_script_only(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite"}}'
    )
    result = detect_framework(tmp_path)
    assert result["start_command"] == "npm run dev"


def test_detect_framework_node_with_no_scripts(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "app"}')
    result = detect_framework(tmp_path)
    assert result["start_command"] == "npm start"


def test_detect_framework_node_with_malformed_json_still_matches(tmp_path: Path):
    (tmp_path / "package.json").write_text("{not valid json")
    result = detect_framework(tmp_path)
    assert result["start_command"] == "npm start"
    assert result["port"] == 3000


def test_detect_framework_procfile_web_line(tmp_path: Path):
    (tmp_path / "Procfile").write_text("web: gunicorn app:app\nworker: celery worker\n")
    result = detect_framework(tmp_path)
    assert result["start_command"] == "gunicorn app:app"
    assert result["port"] == 3000
    assert result["ready_path"] == "/"


def test_detect_framework_procfile_without_web_line_is_not_a_match(tmp_path: Path):
    (tmp_path / "Procfile").write_text("worker: celery worker\n")
    result = detect_framework(tmp_path)
    assert result == {"start_command": "", "port": "", "ready_path": ""}


def test_detect_framework_docker_compose_short_port_syntax(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        """
services:
  web:
    command: "gunicorn app:app"
    ports:
      - "8080:80"
"""
    )
    result = detect_framework(tmp_path)
    assert result["start_command"] == "gunicorn app:app"
    assert result["port"] == 8080
    assert result["ready_path"] == "/"


def test_detect_framework_docker_compose_long_port_syntax(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        """
services:
  web:
    command: ["gunicorn", "app:app"]
    ports:
      - published: 9090
        target: 80
"""
    )
    result = detect_framework(tmp_path)
    assert result["start_command"] == "gunicorn app:app"
    assert result["port"] == 9090


def test_detect_framework_docker_compose_without_command_is_not_a_match(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text(
        """
services:
  web:
    image: nginx
    ports:
      - "8080:80"
"""
    )
    result = detect_framework(tmp_path)
    assert result == {"start_command": "", "port": "", "ready_path": ""}


def test_detect_framework_malformed_docker_compose_is_not_a_match(tmp_path: Path):
    (tmp_path / "docker-compose.yml").write_text("not: valid: yaml: [")
    result = detect_framework(tmp_path)
    assert result == {"start_command": "", "port": "", "ready_path": ""}


# --- detect_framework: precedence -------------------------------------------


def test_detect_framework_prefers_django_over_node(tmp_path: Path):
    (tmp_path / "manage.py").write_text("")
    (tmp_path / "package.json").write_text('{"scripts": {"start": "x"}}')
    result = detect_framework(tmp_path)
    assert result["start_command"] == "python manage.py runserver 0.0.0.0:8000"


def test_detect_framework_prefers_node_over_procfile(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "x"}}')
    (tmp_path / "Procfile").write_text("web: something else\n")
    result = detect_framework(tmp_path)
    assert result["start_command"] == "npm start"
    assert result["port"] == 3000


# --- write_config ------------------------------------------------------------


def test_write_config_writes_all_required_keys_when_detected(tmp_path: Path):
    detected = {"start_command": "npm start", "port": 3000, "ready_path": "/"}
    path = write_config(tmp_path, detected)

    assert path == tmp_path / ".vaultshield.yml"
    assert path.is_file()

    loaded = yaml.safe_load(path.read_text())
    assert loaded == {
        "start_command": "npm start",
        "port": 3000,
        "ready_path": "/",
        "agent": "",
        "routes": [],
        "off_limits_paths": [],
    }


def test_write_config_writes_placeholders_when_nothing_detected(tmp_path: Path):
    detected = {"start_command": "", "port": "", "ready_path": ""}
    path = write_config(tmp_path, detected)

    loaded = yaml.safe_load(path.read_text())
    assert loaded == {
        "start_command": "",
        "port": "",
        "ready_path": "",
        "agent": "",
        "routes": [],
        "off_limits_paths": [],
    }


def test_write_config_tolerates_partial_detected_dict(tmp_path: Path):
    path = write_config(tmp_path, {})
    loaded = yaml.safe_load(path.read_text())
    assert loaded["start_command"] == ""
    assert loaded["port"] == ""
    assert loaded["ready_path"] == ""
    assert loaded["agent"] == ""
    assert loaded["routes"] == []
    assert loaded["off_limits_paths"] == []


def test_write_config_returns_path_inside_repo_root(tmp_path: Path):
    path = write_config(tmp_path, {"start_command": "", "port": "", "ready_path": ""})
    assert path.parent == tmp_path
    assert path.name == ".vaultshield.yml"


# --- load_config: valid configuration ---------------------------------------


def _write_yaml(path: Path, content: dict) -> Path:
    config_path = path / ".vaultshield.yml"
    config_path.write_text(yaml.safe_dump(content, sort_keys=False))
    return config_path


def test_load_config_returns_project_config_for_fully_valid_file(tmp_path: Path):
    _write_yaml(
        tmp_path,
        {
            "start_command": "npm start",
            "port": 3000,
            "ready_path": "/",
            "agent": "codex",
            "routes": ["/", "/about"],
            "off_limits_paths": [".env", "secrets/"],
        },
    )

    config = load_config(tmp_path)

    assert config == ProjectConfig(
        start_command="npm start",
        port=3000,
        ready_path="/",
        agent="codex",
        routes=["/", "/about"],
        off_limits_paths=[".env", "secrets/"],
    )


def test_load_config_defaults_optional_fields_when_absent(tmp_path: Path):
    _write_yaml(
        tmp_path,
        {
            "start_command": "python manage.py runserver 0.0.0.0:8000",
            "port": 8000,
            "ready_path": "/",
        },
    )

    config = load_config(tmp_path)

    assert config.agent == ""
    assert config.routes == []
    assert config.off_limits_paths == []


# --- load_config: the optional backend `project` id -------------------------
#
# `run` registers its Run under this Project before any Sandbox exists, and
# `--project` takes precedence over it. Absent is normal (the option supplies
# it), so only a wrong type is an error.

_MINIMAL_CONFIG = {"start_command": "npm start", "port": 3000, "ready_path": "/"}


def test_load_config_defaults_project_to_empty_when_absent(tmp_path: Path):
    _write_yaml(tmp_path, dict(_MINIMAL_CONFIG))

    assert load_config(tmp_path).project == ""


def test_load_config_reads_a_present_project(tmp_path: Path):
    _write_yaml(tmp_path, {**_MINIMAL_CONFIG, "project": "0123456789abcdef01234567"})

    assert load_config(tmp_path).project == "0123456789abcdef01234567"


def test_load_config_rejects_a_project_of_the_wrong_type(tmp_path: Path):
    _write_yaml(tmp_path, {**_MINIMAL_CONFIG, "project": ["not", "a", "string"]})

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)

    assert "`project`" in str(excinfo.value)
    assert str(tmp_path / ".vaultshield.yml") in str(excinfo.value)


# --- load_config: missing required fields -----------------------------------


def test_load_config_raises_config_error_for_single_missing_field(tmp_path: Path):
    _write_yaml(tmp_path, {"start_command": "npm start", "ready_path": "/"})

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)

    assert "port" in str(excinfo.value)
    assert "start_command" not in str(excinfo.value)
    assert "ready_path" not in str(excinfo.value)


def test_load_config_raises_config_error_naming_all_missing_fields(tmp_path: Path):
    _write_yaml(tmp_path, {"agent": "mock"})

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)

    message = str(excinfo.value)
    assert "start_command" in message
    assert "port" in message
    assert "ready_path" in message


def test_load_config_treats_empty_string_field_as_missing(tmp_path: Path):
    _write_yaml(tmp_path, {"start_command": "", "port": 3000, "ready_path": "/"})

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)

    assert "start_command" in str(excinfo.value)


def test_load_config_error_names_the_config_file_path(tmp_path: Path):
    _write_yaml(tmp_path, {})

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)

    assert str(tmp_path / ".vaultshield.yml") in str(excinfo.value)


# --- load_config: malformed YAML / non-mapping documents --------------------


def test_load_config_raises_config_error_for_malformed_yaml(tmp_path: Path):
    config_path = tmp_path / ".vaultshield.yml"
    config_path.write_text("start_command: [unclosed\nport: 3000\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)

    assert str(config_path) in str(excinfo.value)


def test_load_config_raises_config_error_for_empty_file(tmp_path: Path):
    config_path = tmp_path / ".vaultshield.yml"
    config_path.write_text("")

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_raises_config_error_when_document_root_is_not_a_mapping(tmp_path: Path):
    config_path = tmp_path / ".vaultshield.yml"
    config_path.write_text("- just\n- a\n- list\n")

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_config_raises_config_error_when_file_is_missing(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path)


# --- F-4: strict typed validation -------------------------------------------
#
# The presence/non-empty check these tests already cover is necessary but not
# sufficient: YAML assigns a type, so a field can be present and non-empty and
# still be the wrong type. Before these checks, a wrong type either surfaced much
# later as an opaque Docker error — after a container had already been created —
# or did not surface at all.
#
# The `off_limits_paths` case is the one that mattered most: a bare string is
# iterable, so `off_limits_paths: protected.txt` was consumed one character at a
# time. The file was NOT protected and thirteen single-character paths were, and
# the Run proceeded looking healthy. A security control that silently protects
# nothing is worse than one that refuses to start.


def _config_dir(tmp_path, body: str):
    """Writes `body` as a `.vaultshield.yml` and returns its directory."""
    (tmp_path / ".vaultshield.yml").write_text(body)
    return tmp_path


VALID_BODY = """\
start_command: "npm start"
port: 3000
ready_path: "/health"
routes:
  - "/"
  - "/login"
off_limits_paths:
  - "protected.txt"
  - "secrets/"
"""


class TestStrictTypeValidation:
    """Every invalid form must be rejected by `load_config` itself.

    `load_config` is step 1 of `run` — before the Run_Directory exists, before
    credentials are generated, and before any container is created — so a
    `ConfigError` from here is by construction a failure that precedes every side
    effect.
    """

    @pytest.mark.parametrize(
        "field,body",
        [
            # port: not-a-number
            ("port", VALID_BODY.replace("port: 3000", "port: not-a-number")),
            ("port", VALID_BODY.replace("port: 3000", "port: 3.5")),
            ("port", VALID_BODY.replace("port: 3000", "port: true")),
            ("port", VALID_BODY.replace("port: 3000", "port: 0")),
            ("port", VALID_BODY.replace("port: 3000", "port: 70000")),
            # start_command: 12345
            (
                "start_command",
                VALID_BODY.replace('start_command: "npm start"', "start_command: 12345"),
            ),
            (
                "start_command",
                VALID_BODY.replace(
                    'start_command: "npm start"', "start_command:\n  - npm\n  - start"
                ),
            ),
            # ready_path as a list
            (
                "ready_path",
                VALID_BODY.replace(
                    'ready_path: "/health"', "ready_path:\n  - /health"
                ),
            ),
            # routes: /home  (a bare string)
            ("routes", VALID_BODY.replace('routes:\n  - "/"\n  - "/login"', "routes: /home")),
            ("routes", VALID_BODY.replace('routes:\n  - "/"\n  - "/login"', "routes: {}")),
            (
                "routes",
                VALID_BODY.replace('routes:\n  - "/"\n  - "/login"', "routes:\n  - 42"),
            ),
            # off_limits_paths: protected.txt  (a bare string)
            (
                "off_limits_paths",
                VALID_BODY.replace(
                    'off_limits_paths:\n  - "protected.txt"\n  - "secrets/"',
                    "off_limits_paths: protected.txt",
                ),
            ),
            (
                "off_limits_paths",
                VALID_BODY.replace(
                    'off_limits_paths:\n  - "protected.txt"\n  - "secrets/"',
                    "off_limits_paths: {}",
                ),
            ),
            (
                "off_limits_paths",
                VALID_BODY.replace(
                    'off_limits_paths:\n  - "protected.txt"\n  - "secrets/"',
                    "off_limits_paths:\n  - 7",
                ),
            ),
        ],
    )
    def test_an_invalid_type_is_rejected_and_names_the_field(self, tmp_path, field, body):
        with pytest.raises(ConfigError) as excinfo:
            load_config(_config_dir(tmp_path, body))
        assert field in str(excinfo.value), (
            f"the error must name the offending field; got {excinfo.value}"
        )

    def test_a_bare_string_off_limits_entry_is_never_iterated_per_character(self, tmp_path):
        """The specific silent failure F-4 removes.

        Accepting the string would have produced thirteen one-character
        "protected paths" and left `protected.txt` unprotected while the Run
        reported success.
        """
        body = VALID_BODY.replace(
            'off_limits_paths:\n  - "protected.txt"\n  - "secrets/"',
            "off_limits_paths: protected.txt",
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(_config_dir(tmp_path, body))
        assert "list of strings" in str(excinfo.value)

    def test_the_valid_form_is_accepted_with_the_right_types(self, tmp_path):
        config = load_config(_config_dir(tmp_path, VALID_BODY))

        assert config.start_command == "npm start"
        assert config.port == 3000 and isinstance(config.port, int)
        assert config.ready_path == "/health"
        assert config.routes == ["/", "/login"]
        assert config.off_limits_paths == ["protected.txt", "secrets/"]

    def test_a_quoted_numeric_port_is_accepted_as_an_integer(self, tmp_path):
        """`port: "3000"` is a reasonable thing to write and unambiguous; it is
        coerced rather than rejected, unlike `port: not-a-number`."""
        config = load_config(_config_dir(tmp_path, VALID_BODY.replace("port: 3000", 'port: "3000"')))

        assert config.port == 3000 and isinstance(config.port, int)

    def test_absent_optional_lists_default_to_empty(self, tmp_path):
        """`init` writes `routes: []`/`off_limits_paths: []` as placeholders, and
        omitting them entirely must behave the same rather than erroring."""
        body = "start_command: npm start\nport: 3000\nready_path: /\n"
        config = load_config(_config_dir(tmp_path, body))

        assert config.routes == []
        assert config.off_limits_paths == []
        assert config.agent == ""

    def test_a_wrong_typed_agent_is_rejected(self, tmp_path):
        body = VALID_BODY + "agent: [mock]\n"
        with pytest.raises(ConfigError) as excinfo:
            load_config(_config_dir(tmp_path, body))
        assert "agent" in str(excinfo.value)

    def test_a_blank_list_entry_is_rejected(self, tmp_path):
        """An empty route or off-limits entry would match everything or nothing
        depending on the consumer; neither is a thing anyone meant to write."""
        body = VALID_BODY.replace('routes:\n  - "/"\n  - "/login"', 'routes:\n  - ""')
        with pytest.raises(ConfigError) as excinfo:
            load_config(_config_dir(tmp_path, body))
        assert "routes[0]" in str(excinfo.value)


# --- Batch 3 FR3: the `egress` key ------------------------------------------


def test_egress_defaults_to_off(tmp_path):
    config = load_config(_config_dir(tmp_path, VALID_BODY))
    assert config.egress.mode == "off" and config.egress.allow == ()


@pytest.mark.parametrize("mode_yaml, mode", [("off", "off"), ("'off'", "off"),
                                             ("log-only", "log-only"), ("allowlist", "allowlist")])
def test_egress_modes(tmp_path, mode_yaml, mode):
    body = VALID_BODY + f"egress:\n  mode: {mode_yaml}\n  allow:\n    - API.Example.com.\n    - '*.example.org:8443'\n"
    config = load_config(_config_dir(tmp_path, body))
    assert config.egress.mode == mode
    assert [str(e) for e in config.egress.allow] == ["api.example.com:443", "*.example.org:8443"]


@pytest.mark.parametrize("egress_yaml", [
    "egress: allowlist\n",
    "egress:\n  mode: true\n",
    "egress:\n  mode: strict\n",
    "egress:\n  mode: allowlist\n  extra: 1\n",
    "egress:\n  allow: api.example.com\n",
    "egress:\n  allow:\n    - https://api.example.com\n",
    "egress:\n  allow:\n    - 10.0.0.1\n",
])
def test_bad_egress_blocks_are_config_errors(tmp_path, egress_yaml):
    with pytest.raises(ConfigError, match="egress"):
        load_config(_config_dir(tmp_path, VALID_BODY + egress_yaml))
