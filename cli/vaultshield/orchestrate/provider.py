"""The model provider: one small interface and a Gemini implementation.

Request format, from the official Gemini API docs (ai.google.dev, read on
2026-10-22; every example there uses this endpoint and `gemini-3.8-flash`):

    POST https://generativelanguage.googleapis.com/v1beta/interactions
    header  x-goog-api-key: <key>          the key goes in a header, never in the URL
    body    {"model": ..., "system_instruction": ..., "input": ...,
             "store": false,                the API stores requests by default
                                            (55 days paid, 1 day free); we never want that
             "response_format": {"type": "text", "mime_type": "application/json",
                                 "schema": {...}},
             "generation_config": {"thinking_level": "low"}}
    reply   {"status": "completed",
             "steps": [{"type": "model_output",
                        "content": [{"type": "text", "text": "<the JSON>"}]}]}
    errors  HTTP status plus {"error": {"code": "invalid_request", "message": "..."}}

Nothing here puts the key, or any request content, in an error message or a log.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import httpx
import yaml

from vaultshield.orchestrate import keys

# The current Stable fast model in the docs. Override with `orchestrator.model`
# in ~/.vaultshield/config.yml.
DEFAULT_MODEL = "gemini-3.8-flash"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
TIMEOUT_S = 30.0
MAX_RETRIES = 1  # one retry, on 429 or 5xx only
BACKOFF_S = 1.0
MAX_RETRY_AFTER_S = 10.0
PROVIDER_NAME = "gemini"

USER_CONFIG = Path(".vaultshield") / "config.yml"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NETWORK = "network_error"
OUTCOME_INVALID_REPLY = "invalid_reply"


class ModelError(Exception):
    """The model call did not give a usable answer. Never contains the key or content."""


class ProviderError(ModelError):
    """The call itself failed: HTTP error, timeout, no network."""

    def __init__(self, message: str, outcome: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class InvalidReplyError(ModelError):
    """The service answered, but not with a usable JSON object. A caller may ask again."""


class ConfigFileError(Exception):
    """~/.vaultshield/config.yml is unusable. Names the file and the setting, not its content."""


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    model: str

    def generate_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        """Ask the model and return its reply parsed as a JSON object.

        `schema` is optional: when given, the service constrains its output to it.
        Raises ProviderError if the call fails and InvalidReplyError if the reply
        is not a JSON object."""
        ...


@dataclass(frozen=True)
class CallRecord:
    """What is known about one model call, without any of its content."""

    provider: str
    model: str
    request_bytes: int
    outcome: str


# --- the user's config file ---------------------------------------------------


def load_model(home: Path | None = None) -> str:
    """The model id: `orchestrator.model` from ~/.vaultshield/config.yml, else the default."""
    path = (home if home is not None else Path.home()) / USER_CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_MODEL
    except (OSError, UnicodeDecodeError):
        raise ConfigFileError(f"Cannot read {path}.") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ConfigFileError(f"{path} is not valid YAML.") from None
    if data is None:
        return DEFAULT_MODEL
    if not isinstance(data, dict):
        raise ConfigFileError(f"{path}: expected a mapping at the top level.")
    section = data.get("orchestrator")
    if section is None:
        return DEFAULT_MODEL
    if not isinstance(section, dict):
        raise ConfigFileError(f"{path}: `orchestrator` must be a mapping.")
    model = section.get("model")
    if model is None:
        return DEFAULT_MODEL
    if not isinstance(model, str) or not _MODEL_ID.match(model):
        raise ConfigFileError(f"{path}: `orchestrator.model` must be a model id such as {DEFAULT_MODEL}.")
    return model


# --- Gemini -------------------------------------------------------------------


class GeminiProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        transport: httpx.BaseTransport | None = None,
        recorder: Callable[[CallRecord], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not _MODEL_ID.match(model):
            raise ValueError("not a valid model id")
        self._api_key = api_key
        self.model = model
        self._transport = transport
        self._recorder = recorder
        self._sleep = sleep

    def __repr__(self) -> str:  # never shows the key
        return f"GeminiProvider(model={self.model!r})"

    __str__ = __repr__

    def _body(self, system: str, user: str, schema: dict | None) -> bytes:
        response_format: dict = {"type": "text", "mime_type": "application/json"}
        if schema is not None:
            response_format["schema"] = schema
        body: dict = {
            "model": self.model,
            "system_instruction": system,
            "input": user,
            "store": False,
            "response_format": response_format,
        }
        if self.model.startswith("gemini-3"):
            body["generation_config"] = {"thinking_level": "low"}
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def generate_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        content = self._body(system, user, schema)
        outcome = OUTCOME_NETWORK
        try:
            answer = self._parse(self._post(content))
            outcome = OUTCOME_OK
            return answer
        except InvalidReplyError:
            outcome = OUTCOME_INVALID_REPLY
            raise
        except ProviderError as exc:
            outcome = exc.outcome
            raise
        finally:
            if self._recorder is not None:
                self._recorder(CallRecord(self.name, self.model, len(content), outcome))

    def _post(self, content: bytes) -> httpx.Response:
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        with httpx.Client(transport=self._transport, timeout=TIMEOUT_S) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = client.post(ENDPOINT, content=content, headers=headers)
                except httpx.TimeoutException:
                    raise ProviderError("The model did not answer within 30 seconds.", OUTCOME_TIMEOUT) from None
                except httpx.HTTPError as exc:
                    raise ProviderError(
                        f"Could not reach the model service ({type(exc).__name__}).", OUTCOME_NETWORK
                    ) from None
                status = response.status_code
                if status == 429 or status >= 500:
                    if attempt < MAX_RETRIES:
                        self._sleep(self._delay(response, attempt))
                        continue
                if status >= 400:
                    raise ProviderError(_http_message(response), f"http_{status}")
                return response
        raise ProviderError("The model service did not answer.", OUTCOME_NETWORK)  # pragma: no cover

    @staticmethod
    def _delay(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("retry-after", "")
        if header.isdigit():
            return min(float(header), MAX_RETRY_AFTER_S)
        return BACKOFF_S * (2**attempt) + random.uniform(0, 0.5)

    @staticmethod
    def _parse(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            raise InvalidReplyError("The model service sent something that is not JSON.") from None
        if not isinstance(data, dict) or data.get("status") != "completed":
            status = data.get("status") if isinstance(data, dict) else None
            shown = status if isinstance(status, str) and _ERROR_CODE.match(status) else "unknown"
            raise InvalidReplyError(f"The model did not finish its answer (status: {shown}).")
        parts: list[str] = []
        for step in data.get("steps") or []:
            if isinstance(step, dict) and step.get("type") == "model_output":
                for piece in step.get("content") or []:
                    if isinstance(piece, dict) and piece.get("type") == "text" and isinstance(piece.get("text"), str):
                        parts.append(piece["text"])
        text = "".join(parts).strip()
        if not text:
            raise InvalidReplyError("The model answered with no text.")
        try:
            answer = json.loads(text)
        except ValueError:
            raise InvalidReplyError("The model's answer is not valid JSON.") from None
        if not isinstance(answer, dict):
            raise InvalidReplyError("The model's answer is not a JSON object.")
        return answer


def _http_message(response: httpx.Response) -> str:
    """'HTTP 403 (permission_denied)': the status and the machine code, never the
    service's free-text message (it can echo what was sent)."""
    code = ""
    try:
        error = response.json().get("error")
        candidate = error.get("code") if isinstance(error, dict) else None
        if isinstance(candidate, str) and _ERROR_CODE.match(candidate):
            code = f" ({candidate})"
    except (ValueError, AttributeError):
        pass
    return f"The model service returned HTTP {response.status_code}{code}."


# --- building the provider for a command --------------------------------------


def default_transport() -> httpx.BaseTransport | None:
    """None means the real network. Tests replace this with an httpx.MockTransport."""
    return None


def build_provider(
    *,
    key: keys.ResolvedKey | None = None,
    home: Path | None = None,
    recorder: Callable[[CallRecord], None] | None = None,
) -> GeminiProvider:
    """A Gemini provider with the configured model and `key` (default: `keys.require_key`).

    Raises keys.ModelKeyError if there is no key and ConfigFileError for a bad config file."""
    key = key or keys.require_key(keys.PROVIDER_GEMINI)
    return GeminiProvider(key.value, load_model(home), transport=default_transport(), recorder=recorder)
