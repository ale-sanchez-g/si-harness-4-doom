"""An in-process stand-in for the Ollama HTTP API (httpx MockTransport handler)."""

from __future__ import annotations

import json
from typing import Callable

import httpx


def heuristic_responder(body: dict) -> str:
    """Answer like a sensible model would, always within the requested schema."""
    schema = body["format"]
    actions = schema["properties"]["action"]["enum"]
    args = schema["properties"]["arg"]["enum"]
    if "attack" in actions and "E1" in args:
        answer = {"thought": "An enemy is in view, shoot it.", "action": "attack", "arg": "E1"}
    elif "pickup" in actions and "I1" in args:
        answer = {"thought": "Grab the item.", "action": "pickup", "arg": "I1"}
    elif "goto_exit" in actions:
        answer = {"thought": "Head for the exit.", "action": "goto_exit", "arg": "none"}
    elif "explore" in actions:
        answer = {"thought": "Nothing here, explore.", "action": "explore", "arg": "none"}
    else:
        answer = {"thought": "Look around.", "action": actions[0], "arg": args[0]}
    thought = answer.pop("thought")
    if "Thought" in schema["properties"]:
        answer = {"Thought": thought, **answer}
    return json.dumps(answer)


class FakeOllama:
    def __init__(self, models: tuple[str, ...] = ("granite4.2:3b",),
                 responder: Callable[[dict], str] = heuristic_responder,
                 capabilities: tuple[str, ...] = ("completion", "tools", "thinking")):
        self.models = list(models)
        self.responder = responder
        self.capabilities = list(capabilities)
        self.requests: list[tuple[str, dict]] = []

    @property
    def chats(self) -> list[dict]:
        return [body for path, body in self.requests if path == "/api/chat"]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.requests.append((path, body))
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path == "/api/pull":
            self.models.append(body["model"])
            lines = [{"status": "pulling manifest"},
                     {"status": "downloading", "total": 100, "completed": 50},
                     {"status": "downloading", "total": 100, "completed": 100},
                     {"status": "success"}]
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": self.capabilities})
        if path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat":
            content = self.responder(body)
            return httpx.Response(200, json={
                "model": body["model"], "message": {"role": "assistant", "content": content},
                "prompt_eval_count": 420, "eval_count": 28, "eval_duration": 1_400_000_000, "done": True})
        return httpx.Response(404, json={"error": f"no route {path}"})
