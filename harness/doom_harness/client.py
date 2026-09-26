"""Tiny typed client for the Doom API server."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class DoomAPIError(RuntimeError):
    pass


class DoomClient:
    def __init__(self, base_url: str, timeout: float = 300.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw) -> dict | list:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise DoomAPIError(f"cannot reach the Doom server at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            raise DoomAPIError(f"{method} {path} -> {resp.status_code}: {detail}")
        return resp.json()

    def wait_ready(self, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self._call("GET", "/api/health")
            except DoomAPIError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1.0)

    def scenarios(self) -> list[dict]:
        return self._call("GET", "/api/scenarios")  # type: ignore[return-value]

    def new_episode(self, **kwargs) -> dict:
        body = {k: v for k, v in kwargs.items() if v is not None}
        return self._call("POST", "/api/episode", json=body)  # type: ignore[return-value]

    def observation(self) -> dict:
        return self._call("GET", "/api/observation")  # type: ignore[return-value]

    def command(self, command: str, **kwargs) -> dict:
        body = {"command": command, **{k: v for k, v in kwargs.items() if v is not None}}
        return self._call("POST", "/api/command", json=body)  # type: ignore[return-value]

    def post_agent_log(self, entry: dict) -> None:
        """Best effort: the viewer is nice to have, never worth crashing for."""
        try:
            self._http.post("/api/agent/log", json=entry, timeout=5.0)
        except httpx.HTTPError as exc:
            log.debug("could not post agent log: %s", exc)
