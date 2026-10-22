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
