"""HTTP API around the Doom session (FastAPI).

Interactive docs are served at /docs. The web viewer (live video, map and the
agent's reasoning) is served at /.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .commands import COMMAND_SPECS, CommandArgs, CommandRunner
from .config import Settings
from .minimap import render_map
from .perception import observe, player_status
from .scenarios import SCENARIOS, next_map
from .session import BUTTON_NAMES, DoomSession

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

CommandName = Literal["attack", "turn", "face", "move", "goto", "goto_exit", "explore", "use",
                      "retreat", "dodge", "select_weapon", "wait"]


class EpisodeRequest(BaseModel):
    scenario: str | None = Field(None, description="Scenario name, see GET /api/scenarios")
    map: str | None = Field(None, description="Map lump, e.g. MAP01 or E1M1 ('next' = next map)")
    skill: int | None = Field(None, ge=1, le=5, description="1 (easy) .. 5 (nightmare)")
    seed: int | None = Field(None, description="RNG seed for reproducible episodes")
    timeout: float | None = Field(None, ge=0, description="Game-time limit in seconds (0 = none)")


class CommandRequest(BaseModel):
    command: CommandName
    target_id: int | None = Field(None, description="Object id from the observation (enemy, item, barrel)")
    x: float | None = Field(None, description="Map x coordinate (goto/face)")
    y: float | None = Field(None, description="Map y coordinate (goto/face)")
    degrees: float | None = Field(None, description="turn: positive = left, negative = right")
    direction: str | None = Field(None, description="move: forward|backward|left|right; turn: left|right|around; "
                                                    "dodge: left|right|auto")
    distance: float | None = Field(None, gt=0, description="move/retreat distance in map units")
    duration: float | None = Field(None, gt=0, description="seconds of game time (attack/explore/goto/wait)")
    weapon: str | None = Field(None, description="select_weapon: name (pistol, shotgun...) or slot 1-7")
    interrupt_on_enemy: bool = Field(True, description="stop when a new enemy comes into view")
    interrupt_on_damage: bool = Field(True, description="stop when hurt by something unseen")
    auto_weapon: bool = Field(True, description="attack: pick the best weapon for the distance")


class StepRequest(BaseModel):
    buttons: dict[str, float] = Field(default_factory=dict, description=f"Any of {BUTTON_NAMES}")
    tics: int = Field(1, ge=1, le=350)


class AgentLogEntry(BaseModel):
    step: int | None = None
    thought: str | None = None
    action: str | None = None
    args: dict | None = None
    result: str | None = None
    status: str | None = None
    model: str | None = None
    latency: float | None = None
    extra: dict | None = None


class RuntimeSettings(BaseModel):
    playback_fps: float | None = Field(None, ge=0, le=350, description="35 = real time, 0 = unthrottled")


def create_app(settings: Settings | None = None, session: DoomSession | None = None) -> FastAPI:
    settings = settings or Settings()
    session = session or DoomSession(settings)
    runner = CommandRunner(session)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.autostart:
            try:
                await asyncio.to_thread(session.start_episode)
            except Exception:  # keep the API up so the problem can be fixed via /api/episode
                log.exception("autostart failed")
        yield
        session.close()

    app = FastAPI(title="Doom API", version=__version__, lifespan=lifespan,
                  description="Turn-based Doom (ViZDoom) with structured observations and "
                              "high-level commands, built for LLM agents.")
    app.state.session = session

    def _observation() -> dict:
        with session.lock:
            if not session.running:
                raise HTTPException(409, "no episode running: POST /api/episode first")
            return observe(session)

    @app.get("/", include_in_schema=False)
    def viewer():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "episode_running": session.running}

    @app.get("/api/scenarios")
    def scenarios():
        return [{"name": s.name, "title": s.title, "goal": s.goal, "default_map": s.default_map,
                 "campaign": s.campaign, "requires": s.iwad, "commands": list(s.commands)}
                for s in SCENARIOS.values()]

    @app.get("/api/commands")
    def commands():
        return COMMAND_SPECS

    @app.post("/api/episode")
    def new_episode(req: EpisodeRequest):
        map_name = req.map
        if map_name and map_name.lower() == "next":
            current = session.scenario.name if session.scenario else req.scenario
            map_name = next_map(req.scenario or current or "", session.map) or None
        try:
            session.start_episode(req.scenario, map_name, req.skill, req.seed, req.timeout)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        except FileNotFoundError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _observation()

    @app.get("/api/observation")
    def observation():
        return _observation()

    @app.post("/api/command")
    def command(req: CommandRequest):
        if not session.running:
            raise HTTPException(409, "no episode running: POST /api/episode first")
        return runner.run(CommandArgs(**req.model_dump()))

    @app.post("/api/step")
    def step(req: StepRequest):
        try:
            session.step(req.buttons, req.tics)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _observation()

    @app.get("/api/status")
    def status():
        """Cheap, lock-free status for the viewer (safe to poll while a command runs)."""
        snap, tr = session.snapshot, session.tracker
        if snap is None or tr is None:
            return {"running": False}
        return {
            "running": True, "episode": tr.id, "scenario": tr.scenario, "map": tr.map,
            "tic": snap.tic, "time": round(snap.tic / 35.0, 1), "finished": tr.finished,
            "end_reason": tr.end_reason, "command": session.current_command,
            "player": player_status(session),
            "explored_percent": round(session.nav.explored_fraction() * 100, 1) if session.nav else 0,
            "events": [e.to_dict() for e in tr.events[-8:]],
            "playback_fps": session.playback_fps,
        }

    @app.get("/api/frame.jpg")
    def frame():
        jpeg, _ = session.frames.jpeg()
        if jpeg is None:
            raise HTTPException(404, "no frame yet")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/map.png")
    def map_png(size: int = Query(640, ge=128, le=2048)):
        png = render_map(session, size)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/stream.mjpg")
    async def stream(fps: float = Query(30, gt=0, le=60)):
        async def frames():
            last = -1
            while True:
                jpeg, version = await asyncio.to_thread(session.frames.jpeg)
                if jpeg is not None and version != last:
                    last = version
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                await asyncio.sleep(1.0 / fps)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/api/agent/log")
    def agent_log(entry: AgentLogEntry):
        return session.add_agent_log(entry.model_dump(exclude_none=True))

    @app.get("/api/agent/log")
    def agent_log_list(after: int = 0):
        return session.agent_log_after(after)

    @app.put("/api/settings")
    def update_settings(req: RuntimeSettings):
        if req.playback_fps is not None:
            session.playback_fps = req.playback_fps
        return {"playback_fps": session.playback_fps}

    return app
