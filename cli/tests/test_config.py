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
