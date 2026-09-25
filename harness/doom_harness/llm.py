"""LLM backends. The harness only needs one call: messages + JSON schema -> dict."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    data: dict
    text: str
    latency: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_second: float = 0.0
    raw: dict = field(default_factory=dict)


class LLM(Protocol):
    model: str

    def chat(self, messages: list[dict], schema: dict) -> LLMReply: ...


def parse_json_reply(text: str) -> dict:
    """Parse the model's JSON, tolerating code fences, chatter and truncation."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Truncated output (hit num_predict): salvage key/value pairs we can read.
    pairs = dict(re.findall(r'"(\w+)"\s*:\s*"([^"]*)"', text))
    if "action" in pairs:
        return pairs
    raise LLMError(f"model did not return JSON: {text[:200]!r}")


class OllamaLLM:
    """Ollama's native /api/chat with structured outputs (``format`` = JSON schema)."""

    def __init__(self, host: str, model: str, temperature: float = 0.2, num_ctx: int = 8192,
                 num_predict: int = 200, timeout: float = 180.0, keep_alive: str = "30m",
                 think: bool | None = None, transport: httpx.BaseTransport | None = None):
        self.host = host.rstrip("/")
        self.model = model
        self.options = {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict}
        self.keep_alive = keep_alive
        self.think = think  # None = auto: thinking off for models that support it
        self._think_value: tuple[bool | None] | None = None
        self._http = httpx.Client(base_url=self.host, timeout=timeout, transport=transport)

    # ------------------------------------------------------------ model mgmt
    def list_models(self) -> list[str]:
        try:
            resp = self._http.get("/api/tags", timeout=15.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"cannot reach Ollama at {self.host}: {exc}") from exc
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def has_model(self) -> bool:
        names = self.list_models()
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return any(n == wanted or n == self.model for n in names)

    def pull(self, progress: Callable[[str], None] = print) -> None:
        """Download the model through the Ollama API (streams progress)."""
        last = ""
        try:
            with self._http.stream("POST", "/api/pull", json={"model": self.model, "stream": True},
                                   timeout=None) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if "error" in msg:
                        raise LLMError(f"pull failed: {msg['error']}")
                    status = msg.get("status", "")
                    if msg.get("total"):
                        pct = 100.0 * msg.get("completed", 0) / msg["total"]
                        status = f"{status} {pct:5.1f}%"
                    if status != last and (not msg.get("total") or status.endswith("0%")):
                        progress(f"[ollama] {self.model}: {status}")
                        last = status
        except httpx.HTTPError as exc:
            raise LLMError(f"pull of {self.model} failed: {exc}") from exc

    def ensure_model(self, pull: bool = True, progress: Callable[[str], None] = print) -> None:
        if self.has_model():
            return
        if not pull:
            raise LLMError(f"model {self.model} is not available in Ollama (run: ollama pull {self.model})")
        progress(f"[ollama] pulling {self.model} (first run only, this can take a few minutes)...")
        self.pull(progress)

    def capabilities(self) -> list[str]:
        try:
            resp = self._http.post("/api/show", json={"model": self.model}, timeout=30.0)
            resp.raise_for_status()
            return list(resp.json().get("capabilities") or [])
        except (httpx.HTTPError, ValueError):
            return []

    def _think(self) -> bool | None:
        """Value for Ollama's ``think`` flag (None = do not send it).

        Reasoning models such as granite4.2 think by default, which costs hundreds of
        tokens per turn; the harness asks for a one-line "Thought" instead, so thinking
        is switched off unless explicitly requested.
        """
        if self._think_value is None:
            supports = "thinking" in self.capabilities()
            if self.think is None:
                value = False if supports else None
            else:
                value = self.think if (supports or self.think) else None
            self._think_value = (value,)
        return self._think_value[0]

    def warm_up(self) -> None:
        """Load the model into memory so the first turn is not slow."""
        try:
            self._http.post("/api/generate", json={"model": self.model, "prompt": "",
                                                   "keep_alive": self.keep_alive})
        except httpx.HTTPError as exc:
            log.warning("warm-up failed: %s", exc)

    # ----------------------------------------------------------------- chat
    def chat(self, messages: list[dict], schema: dict) -> LLMReply:
        body: dict = {"model": self.model, "messages": messages, "format": schema, "stream": False,
                      "options": self.options, "keep_alive": self.keep_alive}
        think = self._think()
        if think is not None:
            body["think"] = think
        t0 = time.monotonic()
        try:
            resp = self._http.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        latency = time.monotonic() - t0
        if resp.status_code >= 400:
            raise LLMError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")
        raw = resp.json()
        text = raw.get("message", {}).get("content", "")
        eval_count = int(raw.get("eval_count") or 0)
        eval_ns = int(raw.get("eval_duration") or 0)
        return LLMReply(
            data=parse_json_reply(text), text=text, latency=latency,
            prompt_tokens=int(raw.get("prompt_eval_count") or 0), completion_tokens=eval_count,
            tokens_per_second=(eval_count / (eval_ns / 1e9)) if eval_ns else 0.0, raw=raw)
