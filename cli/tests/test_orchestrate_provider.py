"""Step 2: the Gemini provider over a mocked HTTP transport. No network, no real key."""

import json

import httpx
import pytest

from vaultshield.orchestrate import keys, provider as prov
from vaultshield.orchestrate.provider import (
    DEFAULT_MODEL,
    ENDPOINT,
    CallRecord,
    ConfigFileError,
    GeminiProvider,
    InvalidReplyError,
    ModelProvider,
    ProviderError,
)

from orchestrate_fakes import fenced, gemini_2_5_flash_reply

FAKE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345wxyz"
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}

pytestmark = pytest.mark.usefixtures("no_real_model_key")


def reply(payload, status="completed", extra_steps=()):
    """A reply shaped like the documented Interactions API response."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": "v1_fake",
        "object": "interaction",
        "model": DEFAULT_MODEL,
        "status": status,
        "steps": [*extra_steps, {"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {"total_tokens": 49},
    }


class Server:
    """A mocked service: answers from a list, one per request, and keeps the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def provider(self, **kw):
        kw.setdefault("sleep", lambda s: None)
        return GeminiProvider(FAKE_KEY, kw.pop("model", DEFAULT_MODEL), transport=httpx.MockTransport(self), **kw)


# --- the request --------------------------------------------------------------


def test_the_request_follows_the_documented_format():
    server = Server(reply({"a": "x"}))

    server.provider().generate_json("SYSTEM TEXT", "USER TEXT", SCHEMA)

    [request] = server.requests
    assert request.method == "POST"
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "model": "gemini-2.5-flash",
        "system_instruction": "SYSTEM TEXT",
        "input": "USER TEXT",
        "store": False,
        "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
    }


def test_a_gemini_3_model_is_sent_the_same_request_plus_a_low_thinking_level():
    server = Server(reply({"a": "x"}))

    server.provider(model="gemini-3.8-flash").generate_json("SYSTEM TEXT", "USER TEXT", SCHEMA)

    assert json.loads(server.requests[0].content) == {
        "model": "gemini-3.8-flash",
        "system_instruction": "SYSTEM TEXT",
        "input": "USER TEXT",
        "store": False,
        "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
        "generation_config": {"thinking_level": "low"},
    }


def test_the_default_model_is_the_fastest_one_that_answered_in_the_live_check():
    # 2026-10-22, same request: gemini-2.5-flash 4.9 s, gemini-3.5-flash 14.3 s,
    # gemini-3.8-flash no answer within 25 s. See the README, "The model".
    assert DEFAULT_MODEL == "gemini-2.5-flash"
    assert ENDPOINT == "https://generativelanguage.googleapis.com/v1beta/interactions"


def test_the_key_goes_in_a_header_and_nowhere_else():
    server = Server(reply({"a": "x"}))

    server.provider().generate_json("system", "user")

    [request] = server.requests
    assert request.headers["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in str(request.url)
    assert request.url.query == b""
    assert FAKE_KEY.encode() not in request.content


def test_requests_are_never_stored_on_the_service():
    server = Server(reply({"a": "x"}))
    server.provider().generate_json("system", "user")
    assert json.loads(server.requests[0].content)["store"] is False


def test_without_a_schema_none_is_sent():
    server = Server(reply({"a": "x"}))
    server.provider().generate_json("system", "user")
    assert "schema" not in json.loads(server.requests[0].content)["response_format"]


def test_the_thinking_level_is_only_sent_to_models_that_have_it():
    older = Server(reply({"a": "x"}))
    older.provider(model="gemini-2.5-flash").generate_json("s", "u")
    assert "generation_config" not in json.loads(older.requests[0].content)


def test_the_timeout_is_thirty_seconds():
    server = Server(reply({"a": "x"}))
    server.provider().generate_json("s", "u")
    assert server.requests[0].extensions["timeout"] == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}
    assert prov.TIMEOUT_S == 30.0


def test_non_ascii_text_is_sent_as_utf8():
    server = Server(reply({"a": "x"}))
    server.provider().generate_json("sys", "caf\u00e9 \u5024")
    assert json.loads(server.requests[0].content.decode("utf-8"))["input"] == "caf\u00e9 \u5024"


# --- the reply ----------------------------------------------------------------


def test_the_json_object_in_the_reply_is_returned():
    assert Server(reply({"questions": [1, 2]})).provider().generate_json("s", "u") == {"questions": [1, 2]}


def test_thought_steps_before_the_output_are_ignored():
    thought = {"type": "thought", "summary": [{"type": "text", "text": "thinking about it"}]}
    server = Server(reply({"a": "x"}, extra_steps=[thought]))
    assert server.provider().generate_json("s", "u") == {"a": "x"}


def test_text_split_over_several_pieces_is_joined():
    body = {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": '{"a": '}, {"type": "text", "text": '"x"}'}]}],
    }
    assert Server(body).provider().generate_json("s", "u") == {"a": "x"}


@pytest.mark.parametrize(
    "bad",
    [
        reply("this is not json"),
        reply("[1, 2, 3]"),
        reply('"just a string"'),
        reply(""),
        reply("   "),
        reply({"a": "x"}, status="failed"),
        reply({"a": "x"}, status="in_progress"),
        reply({"a": "x"}, status="requires_action"),
        {"status": "completed"},
        {"status": "completed", "steps": []},
        {"status": "completed", "steps": [{"type": "model_output", "content": []}]},
        {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "image"}]}]},
        ["not", "an", "object"],
    ],
)
def test_a_reply_that_is_not_a_json_object_is_an_invalid_reply(bad):
    with pytest.raises(InvalidReplyError):
        Server(bad).provider().generate_json("s", "u")


def test_a_200_that_is_not_json_at_all_is_an_invalid_reply():
    server = Server(httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(InvalidReplyError):
        server.provider().generate_json("s", "u")


# --- the reply shapes seen live ---------------------------------------------------
# gemini-2.5-flash answered with the documented structure, but its text block held
# the JSON inside a markdown code fence. These pin that and its neighbours.

PAYLOAD = {"questions": [{"id": "auth", "text": "Which login method?", "why": "It changes the screens."}]}
DUMPED = json.dumps(PAYLOAD, indent=2)


def parse(body):
    return Server(body).provider().generate_json("s", "u")


def test_the_reply_captured_live_from_gemini_2_5_flash_parses():
    # signature-only thought step, then the JSON in a ```json fence, no top-level id
    assert parse(gemini_2_5_flash_reply(fenced(PAYLOAD))) == PAYLOAD


def test_the_reply_captured_live_from_gemini_3_5_flash_parses():
    # same structure, but the JSON came bare (no fence), and the service named a
    # different model (gemini-3.6-flash) from the one asked for
    body = {**gemini_2_5_flash_reply(DUMPED), "model": "gemini-3.6-flash"}
    assert Server(body).provider(model="gemini-3.5-flash").generate_json("s", "u") == PAYLOAD


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"```json\n{DUMPED}\n```", id="json-fence"),
        pytest.param(f"```\n{DUMPED}\n```", id="bare-fence"),
        pytest.param(f"```JSON\n{DUMPED}\n```", id="upper-case-tag"),
        pytest.param(f"```json\n{DUMPED}```", id="no-newline-before-closing-fence"),
        pytest.param(f"```json\r\n{DUMPED}\r\n```", id="windows-line-ends"),
        pytest.param(f"  \n```json\n{DUMPED}\n```\n\n", id="blank-space-around-the-fence"),
        pytest.param(f"```json\n{json.dumps(PAYLOAD)}\n```", id="one-line-json"),
        pytest.param(DUMPED, id="bare-object-as-before"),
        pytest.param(f"Here are the questions:\n```json\n{DUMPED}\n```", id="sentence-before-the-fence"),
        pytest.param(f"```json\n{DUMPED}\n```\nHope this helps.", id="sentence-after-the-fence"),
        pytest.param(f"Here you go: {DUMPED} Anything else?", id="object-inside-prose-without-a-fence"),
        pytest.param(f"Use {{name}} as the key.\n```json\n{DUMPED}\n```", id="braces-in-the-prose-before"),
        pytest.param(f"Use {{name}} as the key.\n{DUMPED}", id="braces-in-the-prose-before-no-fence"),
    ],
)
def test_the_object_is_found_in_the_text_the_way_the_models_wrap_it(text):
    assert parse(gemini_2_5_flash_reply(text)) == PAYLOAD


def test_a_fence_inside_a_string_value_does_not_end_the_object():
    payload = {"a": "text with ``` three backticks and a { brace"}
    assert parse(gemini_2_5_flash_reply(fenced(payload))) == payload
    assert parse(gemini_2_5_flash_reply(json.dumps(payload))) == payload


def test_the_outer_object_wins_over_the_objects_inside_it():
    payload = {"outer": {"inner": {"deep": 1}}, "list": [{"k": "v"}]}
    assert parse(gemini_2_5_flash_reply(fenced(payload))) == payload


def test_a_fence_split_over_several_text_blocks_is_joined():
    body = {
        "status": "completed",
        "steps": [
            {"type": "thought", "signature": "c2ln"},
            {
                "type": "model_output",
                "content": [{"type": "text", "text": '```json\n{"a": '}, {"type": "text", "text": '"x"}\n```'}],
            },
        ],
    }
    assert parse(body) == {"a": "x"}


def test_a_sentence_in_one_text_block_and_the_json_in_the_next():
    body = {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "Sure, here it is:\n"}, {"type": "text", "text": fenced({"a": "x"})}],
            }
        ],
    }
    assert parse(body) == {"a": "x"}


def _output(text):
    return {"type": "model_output", "content": [{"type": "text", "text": text}]}


def test_with_several_output_steps_the_last_one_with_an_object_wins():
    body = {"status": "completed", "steps": [_output(fenced({"a": "first"})), _output(fenced({"a": "last"}))]}
    assert parse(body) == {"a": "last"}


def test_an_earlier_output_step_is_used_when_the_last_has_no_object():
    body = {"status": "completed", "steps": [_output(fenced({"a": "first"})), _output("All done.")]}
    assert parse(body) == {"a": "first"}


def test_text_in_a_thought_step_is_never_taken_as_the_answer():
    thought = {"type": "thought", "summary": [{"type": "text", "text": '{"a": "from the thoughts"}'}]}
    body = {"status": "completed", "steps": [thought, {"type": "thought", "signature": "c2ln"}]}
    with pytest.raises(InvalidReplyError, match="no text"):
        parse(body)
    assert parse(gemini_2_5_flash_reply('{"a": "from the answer"}')) == {"a": "from the answer"}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("```json\n[1, 2, 3]\n```", id="fenced-array"),
        pytest.param('```json\n"just a string"\n```', id="fenced-string"),
        pytest.param("```json\n42\n```", id="fenced-number"),
        pytest.param('[{"a": "x"}]', id="array-of-objects"),
        pytest.param('```json\n[{"a": "x"}]\n```', id="fenced-array-of-objects"),
    ],
)
def test_json_that_is_not_an_object_is_still_refused(text):
    with pytest.raises(InvalidReplyError, match="not a JSON object"):
        parse(gemini_2_5_flash_reply(text))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("I could not do that.", id="prose-only"),
        pytest.param("```json\n```", id="empty-fence"),
        pytest.param("```json\nnot json at all\n```", id="fenced-prose"),
        pytest.param('```json\n{"a": "x"', id="cut-off-object-and-fence"),
        pytest.param('{"a": "x", "b": ', id="cut-off-object"),
        pytest.param("Use {name} and {other} as keys.", id="braces-that-are-not-json"),
        pytest.param("{" * 100_000, id="very-deep-nesting"),
    ],
)
def test_text_with_no_json_object_in_it_is_an_invalid_reply(text):
    with pytest.raises(InvalidReplyError, match="not valid JSON"):
        parse(gemini_2_5_flash_reply(text))


def test_an_invalid_reply_message_never_repeats_what_the_model_said():
    said = "MODEL-SAID-THIS-PRIVATE-TEXT"
    for text in (said, f"```json\n[\"{said}\"]\n```", f'{{"a": "{said}"'):
        with pytest.raises(InvalidReplyError) as info:
            parse(gemini_2_5_flash_reply(text))
        assert said not in str(info.value)


def test_a_reply_with_no_object_is_recorded_as_invalid_reply_and_not_retried():
    records = []
    server = Server(gemini_2_5_flash_reply("I could not do that."))
    with pytest.raises(InvalidReplyError):
        server.provider(recorder=records.append).generate_json("s", "u")
    assert len(server.requests) == 1
    assert [r.outcome for r in records] == ["invalid_reply"]


# --- errors and retries -------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_one_retry_on_429_and_5xx_then_success(status):
    sleeps = []
    server = Server(httpx.Response(status, json={"error": {"code": "unavailable"}}), reply({"a": "x"}))

    result = server.provider(sleep=sleeps.append).generate_json("s", "u")

    assert result == {"a": "x"}
    assert len(server.requests) == 2
    assert len(sleeps) == 1 and 1.0 <= sleeps[0] <= 1.5  # backoff of about a second


@pytest.mark.parametrize("status", [429, 503])
def test_only_one_retry_then_the_error(status):
    server = Server(httpx.Response(status, json={"error": {"code": "resource_exhausted"}}))

    with pytest.raises(ProviderError) as excinfo:
        server.provider().generate_json("s", "u")

    assert len(server.requests) == 2
    assert excinfo.value.outcome == f"http_{status}"
    assert f"HTTP {status}" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408])
def test_other_errors_are_not_retried(status):
    server = Server(httpx.Response(status, json={"error": {"code": "invalid_request"}}))

    with pytest.raises(ProviderError) as excinfo:
        server.provider().generate_json("s", "u")

    assert len(server.requests) == 1
    assert excinfo.value.outcome == f"http_{status}"


def test_retry_after_is_honoured_up_to_a_cap():
    sleeps = []
    server = Server(httpx.Response(429, headers={"retry-after": "4"}, json={}), reply({"a": "x"}))
    server.provider(sleep=sleeps.append).generate_json("s", "u")
    assert sleeps == [4.0]

    sleeps.clear()
    server = Server(httpx.Response(429, headers={"retry-after": "9999"}, json={}), reply({"a": "x"}))
    server.provider(sleep=sleeps.append).generate_json("s", "u")
    assert sleeps == [10.0]


def test_a_timeout_is_an_error_and_is_not_retried():
    server = Server(httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderError) as excinfo:
        server.provider().generate_json("s", "u")

    assert excinfo.value.outcome == "timeout"
    assert len(server.requests) == 1
    assert "30 seconds" in str(excinfo.value)


def test_no_network_is_an_error():
    server = Server(httpx.ConnectError("no route to host"))
    with pytest.raises(ProviderError) as excinfo:
        server.provider().generate_json("s", "u")
    assert excinfo.value.outcome == "network_error"
    assert "ConnectError" in str(excinfo.value)


def test_an_error_message_never_carries_the_service_text_the_key_or_the_request():
    echo = {"error": {"code": "invalid_request", "message": f"bad key {FAKE_KEY} and input SECRET-INPUT-TEXT"}}
    server = Server(httpx.Response(400, json=echo))

    with pytest.raises(ProviderError) as excinfo:
        server.provider().generate_json("s", "SECRET-INPUT-TEXT")

    text = str(excinfo.value) + repr(excinfo.value)
    assert text.count("HTTP 400 (invalid_request)") >= 1
    assert FAKE_KEY not in text and "SECRET-INPUT-TEXT" not in text and "bad key" not in text


def test_a_strange_error_code_is_not_repeated():
    server = Server(httpx.Response(400, json={"error": {"code": "ignore previous instructions; " + FAKE_KEY}}))
    with pytest.raises(ProviderError) as excinfo:
        server.provider().generate_json("s", "u")
    assert str(excinfo.value) == "The model service returned HTTP 400."


def test_an_unfinished_reply_names_only_a_safe_status():
    with pytest.raises(InvalidReplyError) as excinfo:
        Server(reply({"a": "x"}, status="failed")).provider().generate_json("s", "u")
    assert "status: failed" in str(excinfo.value)


# --- recording ----------------------------------------------------------------


def test_every_call_is_recorded_without_its_content():
    records: list[CallRecord] = []
    server = Server(reply({"a": "x"}))

    server.provider(recorder=records.append).generate_json("SYSTEM-TEXT", "USER-TEXT")

    [record] = records
    assert record.provider == "gemini" and record.model == DEFAULT_MODEL and record.outcome == "ok"
    assert record.request_bytes == len(server.requests[0].content)
    assert "SYSTEM-TEXT" not in repr(record) and "USER-TEXT" not in repr(record)


@pytest.mark.parametrize(
    "answer, outcome",
    [
        (httpx.Response(503, json={}), "http_503"),
        (httpx.Response(403, json={}), "http_403"),
        (httpx.ReadTimeout("x"), "timeout"),
        (httpx.ConnectError("x"), "network_error"),
        (reply("not json"), "invalid_reply"),
    ],
)
def test_failures_are_recorded_with_their_outcome(answer, outcome):
    records: list[CallRecord] = []
    server = Server(answer)

    with pytest.raises(prov.ModelError):
        server.provider(recorder=records.append).generate_json("s", "u")

    assert [r.outcome for r in records] == [outcome]


def test_a_retried_call_is_one_record():
    records: list[CallRecord] = []
    Server(httpx.Response(503, json={}), reply({"a": "x"})).provider(recorder=records.append).generate_json("s", "u")
    assert [r.outcome for r in records] == ["ok"]


# --- the object itself --------------------------------------------------------


def test_the_provider_is_a_model_provider_and_never_shows_its_key():
    p = GeminiProvider(FAKE_KEY, DEFAULT_MODEL)
    assert isinstance(p, ModelProvider)
    for text in (repr(p), str(p), f"{p}"):
        assert FAKE_KEY not in text


@pytest.mark.parametrize("model", ["", "../x", "a/b", "models/gemini", "x" * 80, "a b", "gemini?key=1", "-x"])
def test_a_model_id_that_could_change_the_url_is_refused(model):
    with pytest.raises(ValueError):
        GeminiProvider(FAKE_KEY, model)


# --- ~/.vaultshield/config.yml ------------------------------------------------


def write_config(home, text):
    folder = home / ".vaultshield"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yml").write_text(text)


def test_without_a_config_file_the_default_model_is_used(tmp_path):
    assert prov.load_model(tmp_path) == "gemini-2.5-flash"


def test_the_model_setting_is_read_from_the_config_file(tmp_path):
    write_config(tmp_path, "orchestrator:\n  model: gemini-3.5-flash-lite\n")
    assert prov.load_model(tmp_path) == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("text", ["", "# only a comment\n", "other: 1\n", "orchestrator:\n", "orchestrator:\n  other: 1\n"])
def test_a_config_without_the_setting_uses_the_default(tmp_path, text):
    write_config(tmp_path, text)
    assert prov.load_model(tmp_path) == DEFAULT_MODEL


@pytest.mark.parametrize(
    "text",
    ["- a\n- b\n", "orchestrator: 5\n", "orchestrator:\n  model: 7\n", "orchestrator:\n  model: ../etc\n", "{ not yaml", "orchestrator:\n  model: [a]\n"],
)
def test_a_bad_config_file_is_an_error_that_names_the_file_not_its_content(tmp_path, text):
    write_config(tmp_path, text)
    with pytest.raises(ConfigFileError) as excinfo:
        prov.load_model(tmp_path)
    assert "config.yml" in str(excinfo.value)
    assert "../etc" not in str(excinfo.value)  # the bad value itself is not repeated


def test_the_config_file_is_read_from_the_home_directory_of_the_process(protect_project, tmp_path):
    write_config(tmp_path / "home", "orchestrator:\n  model: gemini-3.6-flash\n")
    assert prov.load_model() == "gemini-3.6-flash"


# --- building the provider ----------------------------------------------------


def test_build_provider_uses_the_resolved_key_and_the_configured_model(protect_project, tmp_path, monkeypatch):
    write_config(tmp_path / "home", "orchestrator:\n  model: gemini-3.6-flash\n")
    server = Server(reply({"a": "x"}))
    monkeypatch.setattr(prov, "default_transport", lambda: httpx.MockTransport(server))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    built = prov.build_provider()
    built.generate_json("s", "u")

    assert built.model == "gemini-3.6-flash"
    assert server.requests[0].headers["x-goog-api-key"] == FAKE_KEY


def test_build_provider_without_a_key_says_how_to_set_one(protect_project):
    with pytest.raises(keys.ModelKeyError) as excinfo:
        prov.build_provider()
    assert str(excinfo.value) == "No Gemini API key found. Run: vaultshield config set-key gemini"


def test_the_real_network_is_the_default_transport():
    assert prov.default_transport() is None
