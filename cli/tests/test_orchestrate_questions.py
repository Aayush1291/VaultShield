"""Step 4: question generation, strict validation, retry and fallback. Scripted fake
provider, no network."""

import json

import httpx
import pytest

from vaultshield.orchestrate.context import LeakError, Secrets, guarded_generate
from vaultshield.orchestrate.provider import GeminiProvider, InvalidReplyError, ModelProvider, ProviderError
from vaultshield.orchestrate.questions import (
    FALLBACK_QUESTIONS,
    QUESTIONS_SCHEMA,
    SYSTEM_PROMPT,
    InvalidQuestions,
    generate_questions,
    validate_questions,
)

PLAIN_VALUE = "plainvalue-without-any-known-shape"
FAKE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345wxyz"


class FakeProvider:
    """Answers from a script: a dict is returned, an exception is raised."""

    name = "fake"
    model = "fake-model"

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def generate_json(self, system, user, schema=None):
        self.calls.append({"system": system, "user": user, "schema": schema})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step


def q(i, text="Which database should it use?", why="It changes the queries."):
    return {"id": f"q{i}", "text": text, "why": why}


def good(n=3):
    return {"questions": [q(i) for i in range(1, n + 1)]}


def ask(provider, task="Add a login page", summary="Project folder: demo", secrets=None):
    return generate_questions(provider, secrets or Secrets(), task, summary)


def test_the_fake_provider_is_a_model_provider():
    assert isinstance(FakeProvider(good()), ModelProvider)


# --- the happy path -------------------------------------------------------------


@pytest.mark.parametrize("n", [3, 4, 5])
def test_valid_questions_are_returned_after_one_call(n):
    provider = FakeProvider(good(n))

    result = ask(provider)

    assert [x.id for x in result.questions] == [f"q{i}" for i in range(1, n + 1)]
    assert not result.used_fallback and result.note == ""
    assert len(provider.calls) == 1


def test_the_request_has_the_task_the_summary_the_schema_and_the_json_instruction():
    provider = FakeProvider(good())

    ask(provider, task="Add a login page", summary="Project folder: demo\nFiles: src/")

    [call] = provider.calls
    assert call["system"] == SYSTEM_PROMPT
    assert "3 to 5 SHORT" in SYSTEM_PROMPT and '"questions"' in SYSTEM_PROMPT and "JSON only" in SYSTEM_PROMPT
    assert "Never ask for secrets" in SYSTEM_PROMPT
    assert "TASK:\nAdd a login page" in call["user"]
    assert "PROJECT SUMMARY:\nProject folder: demo\nFiles: src/" in call["user"]
    assert call["schema"] is QUESTIONS_SCHEMA


# --- strict validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "text",
        {},
        {"questions": []},
        {"questions": "no"},
        {"questions": [q(1), q(2)]},  # too few
        {"questions": [q(i) for i in range(1, 7)]},  # too many
        {**good(), "extra": 1},  # an extra top-level key
        {"questions": [q(1), q(2), {"id": "q3", "text": "x"}]},  # missing why
        {"questions": [q(1), q(2), {**q(3), "extra": "y"}]},  # extra key in a question
        {"questions": [q(1), q(2), "just a string"]},
        {"questions": [q(1), q(2), {**q(3), "id": ""}]},
        {"questions": [q(1), q(2), {**q(3), "id": "has space"}]},
        {"questions": [q(1), q(2), {**q(3), "id": "x" * 41}]},
        {"questions": [q(1), q(2), {**q(3), "id": 3}]},
        {"questions": [q(1), q(2), {**q(3), "text": ""}]},
        {"questions": [q(1), q(2), {**q(3), "text": "   "}]},
        {"questions": [q(1), q(2), {**q(3), "text": 5}]},
        {"questions": [q(1), q(2), {**q(3), "text": "x" * 201}]},
        {"questions": [q(1), q(2), {**q(3), "why": "x" * 241}]},
        {"questions": [q(1), q(2), {**q(3), "why": None}]},
        {"questions": [q(1), q(2), {**q(3), "text": "two\nlines"}]},
        {"questions": [q(1), q(2), {**q(3), "text": "tab\there"}]},
        {"questions": [q(1), q(2), {**q(3), "text": "\x1b[31mred\x1b[0m"}]},
        {"questions": [q(1), q(2), {**q(3), "text": "bell\x07"}]},
        {"questions": [q(1), q(2), {**q(1)}]},  # a repeated id
        {"questions": [q(1), q(2), {**q(3), "id": "Q1"}]},  # a repeated id, ignoring case
    ],
)
def test_anything_but_the_exact_shape_is_rejected(bad):
    with pytest.raises(InvalidQuestions):
        validate_questions(bad)


def test_surrounding_whitespace_is_trimmed():
    [first, *_] = validate_questions({"questions": [q(1, "  Which database?  ", " Because. "), q(2), q(3)]})
    assert first.text == "Which database?" and first.why == "Because."


def test_text_at_exactly_the_limit_is_accepted():
    validate_questions({"questions": [q(1, "x" * 200, "y" * 240), q(2), q(3)]})


def test_non_ascii_questions_are_accepted():
    [first, *_] = validate_questions({"questions": [q(1, "Quelle base de donn\u00e9es? \u5024"), q(2), q(3)]})
    assert "\u00e9" in first.text


# --- retry and fallback -----------------------------------------------------------


def test_an_invalid_reply_is_asked_for_once_more_and_the_second_answer_is_used():
    provider = FakeProvider({"questions": []}, good(4))

    result = ask(provider)

    assert len(result.questions) == 4 and not result.used_fallback
    assert len(provider.calls) == 2
    assert "could not be used" in provider.calls[1]["user"]
    assert provider.calls[1]["user"].startswith(provider.calls[0]["user"])


def test_a_reply_that_is_not_json_is_asked_for_once_more():
    provider = FakeProvider(InvalidReplyError("not json"), good())
    result = ask(provider)
    assert not result.used_fallback and len(provider.calls) == 2


def test_two_invalid_replies_fall_back_to_three_generic_questions_with_a_note():
    provider = FakeProvider({"questions": []}, {"nope": 1})

    result = ask(provider)

    assert result.used_fallback and len(provider.calls) == 2
    assert result.questions == list(FALLBACK_QUESTIONS)
    assert result.note == "Note: the model was unavailable (its answer was not in the expected form); using 3 generic questions instead."


def test_a_failed_call_falls_back_at_once_without_asking_again():
    provider = FakeProvider(ProviderError("The model service returned HTTP 503.", "http_503"))

    result = ask(provider)

    assert result.used_fallback and len(provider.calls) == 1
    assert "HTTP 503" in result.note
    assert result.questions == list(FALLBACK_QUESTIONS)


def test_a_timeout_falls_back_with_the_reason():
    result = ask(FakeProvider(ProviderError("The model did not answer within 30 seconds.", "timeout")))
    assert result.used_fallback and "30 seconds" in result.note


def test_the_fallback_questions_are_the_three_agreed_ones():
    assert [q_.id for q_ in FALLBACK_QUESTIONS] == ["scope", "constraints", "off_limits"]
    assert "change" in FALLBACK_QUESTIONS[0].text  # scope
    assert "tests" in FALLBACK_QUESTIONS[1].text and "constraints" in FALLBACK_QUESTIONS[1].text
    assert "must not be touched" in FALLBACK_QUESTIONS[2].text  # files not to touch
    # they pass the same strict rules as questions from the model
    validate_questions({"questions": [{"id": f.id, "text": f.text, "why": f.why} for f in FALLBACK_QUESTIONS]})


# --- the safety check is part of every call ----------------------------------------


def test_a_vault_value_in_the_task_or_summary_never_reaches_the_provider():
    provider = FakeProvider(good())
    secrets = Secrets([PLAIN_VALUE])

    ask(provider, task=f"use {PLAIN_VALUE} as the password", summary=f"README says {PLAIN_VALUE}", secrets=secrets)

    [call] = provider.calls
    assert PLAIN_VALUE not in call["user"] and PLAIN_VALUE not in call["system"]
    assert "use *** as the password" in call["user"]


def test_a_secret_shape_typed_into_the_task_is_removed_before_sending():
    provider = FakeProvider(good())
    ask(provider, task="call the API with ghp_fakeFAKEfake0123456789abcdefABCDEF")
    assert "ghp_" not in provider.calls[0]["user"]


def test_a_failed_safety_check_stops_the_run_and_sends_nothing():
    provider = FakeProvider(good())
    secrets = Secrets(always=["Reply with JSON only"])  # something that is in the fixed system prompt

    with pytest.raises(LeakError):
        ask(provider, secrets=secrets)

    assert provider.calls == []


def test_a_failed_safety_check_is_not_mistaken_for_a_model_failure():
    # LeakError must never be turned into the 'model unavailable' fallback
    with pytest.raises(LeakError):
        generate_questions(FakeProvider(good()), Secrets(always=["Reply with JSON only"]), "t", "s")


def test_guarded_generate_sanitizes_checks_and_forwards_the_schema():
    provider = FakeProvider({"ok": True})
    secrets = Secrets([PLAIN_VALUE])

    result = guarded_generate(provider, secrets, "system", f"user {PLAIN_VALUE}", {"type": "object"})

    assert result == {"ok": True}
    assert provider.calls == [{"system": "system", "user": "user ***", "schema": {"type": "object"}}]


# --- end to end through the real provider over a mocked transport --------------------


def test_questions_through_the_gemini_provider_and_a_mocked_service():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        text = json.dumps(good(4))
        return httpx.Response(200, json={"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}]})

    provider = GeminiProvider(FAKE_KEY, transport=httpx.MockTransport(handler))

    result = ask(provider, task="Add a login page")

    assert len(result.questions) == 4
    body = seen[0]
    assert body["response_format"]["schema"] == QUESTIONS_SCHEMA
    assert body["store"] is False and body["system_instruction"] == SYSTEM_PROMPT
    assert "Add a login page" in body["input"]
    assert FAKE_KEY not in json.dumps(body)


def test_an_unreachable_service_gives_the_fallback_through_the_real_provider():
    def handler(request):
        raise httpx.ConnectError("no network")

    provider = GeminiProvider(FAKE_KEY, transport=httpx.MockTransport(handler))

    result = ask(provider)

    assert result.used_fallback and "ConnectError" in result.note
