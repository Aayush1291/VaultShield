"""A fake Gemini service and fixed fake data for the Orchestrate tests.

Everything here is invented: the API key, the project values, the replies."""

from __future__ import annotations

import json

import httpx

FAKE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345wxyz"
ENV_VALUE = "plainvalue-without-any-known-shape"

QUESTIONS = {
    "questions": [
        {"id": "db", "text": "Which database should it use?", "why": "It changes the queries."},
        {"id": "auth", "text": "Which login method do you want?", "why": "It changes the screens."},
        {"id": "tests", "text": "Should tests be added?", "why": "It changes the scope."},
    ]
}
APPROACH = {"steps": ["Add a login route in src/app.js.", "Write a test.", "Update the README."]}


def completed(payload) -> dict:
    """A reply shaped like the documented Interactions API response."""
    return {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(payload)}]}]}


def gemini_2_5_flash_reply(text: str) -> dict:
    """The reply shape captured live from gemini-2.5-flash (October 2026), with the
    text replaced by `text`: a thought step holding only a signature, then one
    model_output step. The service sends no top-level `id`. The signature is a short
    invented stand-in for the long opaque one."""
    return {
        "status": "completed",
        "usage": {"total_tokens": 850, "total_input_tokens": 180, "total_output_tokens": 141, "total_thought_tokens": 529},
        "created": "2026-10-22T19:52:47Z",
        "updated": "2026-10-22T19:52:47Z",
        "service_tier": "standard",
        "steps": [
            {"signature": "c2lnbmF0dXJlLXN0YW5kLWlu", "type": "thought"},
            {"type": "model_output", "content": [{"type": "text", "text": text}]},
        ],
        "object": "interaction",
        "model": "gemini-2.5-flash",
    }


def fenced(payload, tag: str = "json") -> str:
    """JSON wrapped the way gemini-2.5-flash wrapped it: a markdown code fence."""
    return f"```{tag}\n{json.dumps(payload, indent=2)}\n```"


class FakeGemini:
    """A mocked service. It tells the two calls apart by the JSON shape the system
    prompt asks for, and keeps every request it receives."""

    def __init__(self, questions=QUESTIONS, approach=APPROACH):
        self.questions, self.approach = questions, approach
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _is_approach(body: dict) -> bool:
        return '{"steps"' in body["system_instruction"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.approach if self._is_approach(json.loads(request.content)) else self.questions
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=completed(answer))

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def kinds(self) -> list[str]:
        return ["approach" if self._is_approach(b) else "questions" for b in self.bodies()]

    def all_request_bytes(self) -> bytes:
        """Every byte of every request body, joined."""
        return b"\n".join(r.content for r in self.requests)
