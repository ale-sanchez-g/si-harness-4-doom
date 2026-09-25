"""One ViZDoom game exposed as a thread-safe, turn-based session.

ViZDoom runs in synchronous PLAYER mode: the world only advances when we call
``make_action``. Between API calls the game is frozen, which turns Doom into a
turn-based game - perfect for slow thinkers such as a small LLM on a CPU.
"""

from __future__ import annotations

import io
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import vizdoom as vzd
from PIL import Image

from . import knowledge as K
from .config import Settings
from .navigation import NavMap
from .scenarios import SCENARIOS, Scenario
from .wad import load_map_info

log = logging.getLogger(__name__)

BUTTONS = [
    vzd.Button.ATTACK, vzd.Button.USE, vzd.Button.SPEED,
    vzd.Button.MOVE_FORWARD, vzd.Button.MOVE_BACKWARD, vzd.Button.MOVE_LEFT, vzd.Button.MOVE_RIGHT,
    vzd.Button.TURN_LEFT_RIGHT_DELTA,
    vzd.Button.SELECT_WEAPON1, vzd.Button.SELECT_WEAPON2, vzd.Button.SELECT_WEAPON3,
    vzd.Button.SELECT_WEAPON4, vzd.Button.SELECT_WEAPON5, vzd.Button.SELECT_WEAPON6,
    vzd.Button.SELECT_WEAPON7,
]
BUTTON_NAMES = [b.name for b in BUTTONS]
_BUTTON_INDEX = {name: i for i, name in enumerate(BUTTON_NAMES)}

GV = vzd.GameVariable
VARIABLES = [
    GV.HEALTH, GV.ARMOR, GV.SELECTED_WEAPON, GV.SELECTED_WEAPON_AMMO, GV.ATTACK_READY,
    GV.KILLCOUNT, GV.ITEMCOUNT, GV.SECRETCOUNT, GV.DAMAGECOUNT, GV.DAMAGE_TAKEN, GV.HITCOUNT,
    GV.DEAD, GV.POSITION_X, GV.POSITION_Y, GV.POSITION_Z, GV.ANGLE, GV.VELOCITY_X, GV.VELOCITY_Y,
] + [getattr(GV, f"AMMO{i}") for i in range(10)] + [getattr(GV, f"WEAPON{i}") for i in range(10)]
VARIABLE_NAMES = [v.name for v in VARIABLES]

WARMUP_TICS = 10  # ViZDoom ignores turn deltas during the first few tics of an episode
SEEN_EVERY = 4  # tics between exploration-map updates


class EpisodeOver(Exception):
    """Raised inside command loops when the episode ends."""


@dataclass
class Snapshot:
    tic: int
    vars: dict[str, float]
    labels: list
    objects: list

    @property
    def x(self) -> float:
        return self.vars["POSITION_X"]

    @property
    def y(self) -> float:
        return self.vars["POSITION_Y"]

    @property
    def angle(self) -> float:
        return self.vars["ANGLE"]


@dataclass
class Event:
    tic: int
    type: str  # pickup | kill | key | message | teleport | secret | death | exit | timeout | info
    text: str

    def to_dict(self) -> dict:
        return {"tic": self.tic, "type": self.type, "text": self.text}


@dataclass
class EpisodeTracker:
    id: str
    scenario: str
    map: str
    skill: int
    started_at: float = field(default_factory=time.time)
    events: list[Event] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    alive: dict[int, tuple[str, float, float]] = field(default_factory=dict)  # monsters
    items: dict[int, tuple[str, float, float]] = field(default_factory=dict)  # items present
    seen_items: dict[int, dict] = field(default_factory=dict)  # items we have seen
    trail: list[tuple[float, float]] = field(default_factory=list)
    finished: bool = False
    end_reason: str | None = None  # died | exit | timeout | completed
    last_health: float = 100.0
    last_pos: tuple[float, float] | None = None
    commands_run: int = 0

    def add(self, tic: int, type_: str, text: str) -> None:
        self.events.append(Event(tic, type_, text))


class FrameHub:
    """Latest rendered frame, shared with MJPEG streaming clients."""

    def __init__(self, quality: int = 80):
        self.quality = quality
        self._frame: np.ndarray | None = None
        self._version = 0
        self._jpeg: bytes | None = None
        self._jpeg_version = -1
        self._lock = threading.Lock()

    @property
    def version(self) -> int:
        return self._version

    def publish(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._version += 1

    def jpeg(self) -> tuple[bytes | None, int]:
        with self._lock:
            if self._frame is None:
                return None, 0
            if self._jpeg_version != self._version:
                buf = io.BytesIO()
                Image.fromarray(self._frame).save(buf, format="JPEG", quality=self.quality)
                self._jpeg, self._jpeg_version = buf.getvalue(), self._version
            return self._jpeg, self._version

    def image(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()


class DoomSession:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.lock = threading.RLock()
        self.game: vzd.DoomGame | None = None
        self._game_key: tuple | None = None
        self.scenario: Scenario | None = None
        self.map: str = ""
        self.skill: int = self.settings.skill
        self.nav: NavMap | None = None
        self.tracker: EpisodeTracker | None = None
        self.snapshot: Snapshot | None = None
        self.frames = FrameHub(self.settings.jpeg_quality)
        self.agent_log: deque[dict] = deque(maxlen=500)
        self._agent_seq = 0
        self.playback_fps = self.settings.playback_fps
        self._next_frame_at = 0.0
        self.current_command: str | None = None
        self.last_path: list[tuple[float, float]] = []

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self.lock:
            if self.game is not None:
                self.game.close()
                self.game = None

    @property
    def running(self) -> bool:
        return self.game is not None and self.tracker is not None

    def _wad_search_paths(self, scenario: Scenario) -> list[str]:
        paths = [self.settings.wad_dir, os.getcwd(), os.path.dirname(vzd.__file__)]
        return [p for p in paths if p and os.path.isdir(p)]

    def _resolve_iwad(self, scenario: Scenario) -> str | None:
        if scenario.iwad is None:
            return None
        for d in self._wad_search_paths(scenario):
            for name in os.listdir(d):
                if name.lower() == scenario.iwad.lower():
                    return os.path.join(d, name)
        raise FileNotFoundError(
            f"Scenario '{scenario.name}' needs {scenario.iwad}. Put your copy in "
            f"{self.settings.wad_dir} (mounted from ./wads) or use a freedoom scenario.")

    def _init_game(self, scenario: Scenario, resolution: str) -> None:
        if self.game is not None:
            self.game.close()
        game = vzd.DoomGame()
        game.load_config(scenario.config_path)
        iwad = self._resolve_iwad(scenario)
        if iwad:
            game.set_doom_game_path(iwad)
        game.set_window_visible(False)
        game.set_sound_enabled(False)
        game.set_audio_buffer_enabled(False)
        game.set_automap_buffer_enabled(False)
        game.set_depth_buffer_enabled(False)
        game.set_labels_buffer_enabled(True)
        game.set_objects_info_enabled(True)
        game.set_sectors_info_enabled(True)
        game.set_notifications_buffer_enabled(True)
        game.set_notifications_buffer_size(1)
        game.set_mode(vzd.Mode.PLAYER)
        game.set_screen_format(vzd.ScreenFormat.RGB24)
        game.set_screen_resolution(_resolution(resolution))
        game.set_render_hud(self.settings.render_hud)
        game.set_render_crosshair(True)
        game.set_render_messages(True)
        game.set_available_buttons(BUTTONS)
        game.set_available_game_variables(VARIABLES)
        game.add_game_args("+sv_cheats 1")  # lets tests `summon` monsters; harmless otherwise
        game.init()
        self.game = game
        self._game_key = (scenario.name, resolution)
        log.info("initialised ViZDoom for scenario %s (%s)", scenario.name, resolution)

    def start_episode(self, scenario: str | None = None, map: str | None = None,
                      skill: int | None = None, seed: int | None = None,
                      timeout: float | None = None, resolution: str | None = None) -> None:
        with self.lock:
            name = scenario or self.settings.scenario
            if name not in SCENARIOS:
                raise KeyError(f"unknown scenario '{name}'. Available: {', '.join(SCENARIOS)}")
            sc = SCENARIOS[name]
            res = resolution or self.settings.resolution
            if self.game is None or self._game_key != (sc.name, res):
                self._init_game(sc, res)
            assert self.game is not None
            if map:
                map_name = map.upper()
            elif name == self.settings.scenario and self.settings.map:
                map_name = self.settings.map.upper()
            else:
                map_name = sc.default_map
            self.skill = int(skill if skill is not None else self.settings.skill)
            self.game.set_doom_map(map_name)
            self.game.set_doom_skill(self.skill)
            if seed is not None:
                self.game.set_seed(int(seed))
            limit = timeout if timeout is not None else self.settings.episode_timeout
            if limit and limit > 0:
                self.game.set_episode_timeout(int(limit * K.TICRATE) + WARMUP_TICS)
            self.game.new_episode()
            self.scenario, self.map = sc, map_name
            self.tracker = EpisodeTracker(uuid.uuid4().hex[:8], sc.name, map_name, self.skill)
            state = self.game.get_state()
            self.nav = NavMap(state.sectors, self._load_map_info(sc, map_name))
            self.last_path = []
            self._capture(state, first=True)
            for _ in range(WARMUP_TICS):
                if self.game.is_episode_finished():
                    break
                self._advance({})
            log.info("episode %s started: %s %s skill %d", self.tracker.id, sc.name, map_name, self.skill)

    def _load_map_info(self, sc: Scenario, map_name: str):
        assert self.game is not None
        wads = [self.game.get_doom_game_path(), self.game.get_doom_scenario_path()]
        wads = [w if os.path.isabs(w) else os.path.join(os.path.dirname(vzd.__file__), w)
                for w in wads if w]
        wads = [w for w in wads if os.path.exists(w)]
        try:
            return load_map_info(wads, map_name)
        except Exception:  # never let an exotic WAD break the game
            log.exception("could not read map info for %s", map_name)
            return None

    # ------------------------------------------------------------- stepping
    def _advance(self, buttons: dict[str, float], tics: int = 1) -> None:
        """Advance the game by ``tics`` with the given buttons held."""
        assert self.game is not None and self.tracker is not None
        if self.tracker.finished:
            raise EpisodeOver(self.tracker.end_reason or "finished")
        action = [0.0] * len(BUTTONS)
        for name, value in buttons.items():
            action[_BUTTON_INDEX[name]] = float(value)
        for _ in range(tics):
            self.game.make_action(action, 1)
            if self.game.is_episode_finished():
                self._finish()
                raise EpisodeOver(self.tracker.end_reason or "finished")
            self._capture(self.game.get_state())
            self._throttle()
            if action[_BUTTON_INDEX["TURN_LEFT_RIGHT_DELTA"]]:
                # Delta buttons act once per make_action; do not keep spinning.
                action[_BUTTON_INDEX["TURN_LEFT_RIGHT_DELTA"]] = 0.0

    def _throttle(self) -> None:
        if self.playback_fps and self.playback_fps > 0:
            now = time.monotonic()
            if self._next_frame_at > now:
                time.sleep(self._next_frame_at - now)
                now = self._next_frame_at
            self._next_frame_at = max(now, self._next_frame_at) + 1.0 / self.playback_fps

    def _finish(self) -> None:
        assert self.game is not None and self.tracker is not None
        tr = self.tracker
        tr.finished = True
        tic = self.snapshot.tic if self.snapshot else 0
        if self.game.is_player_dead():
            tr.end_reason = "died"
            tr.add(tic, "death", "You died.")
        elif self.game.is_episode_timeout_reached():
            tr.end_reason = "timeout"
            tr.add(tic, "timeout", "Time is up.")
        else:
            tr.end_reason = "exit" if self.scenario and self.scenario.campaign else "completed"
            tr.add(tic, "exit", "Level complete!" if tr.end_reason == "exit" else "Scenario complete.")
        if self.snapshot is not None:
            # The final state is not available once the episode ended; keep stats.
            self.snapshot.vars["DEAD"] = 1.0 if tr.end_reason == "died" else self.snapshot.vars.get("DEAD", 0)
            self.snapshot.vars["KILLCOUNT"] = self.game.get_game_variable(GV.KILLCOUNT)
            if tr.end_reason == "died":
                self.snapshot.vars["HEALTH"] = min(0.0, self.game.get_game_variable(GV.HEALTH))
        log.info("episode %s finished: %s", tr.id, tr.end_reason)

    def _capture(self, state, first: bool = False) -> None:
        """Update the snapshot and all trackers from a fresh game state."""
        assert self.tracker is not None and self.nav is not None
        values = state.game_variables
        v = {name: float(values[i]) for i, name in enumerate(VARIABLE_NAMES)}
        snap = Snapshot(state.tic, v, list(state.labels or []), list(state.objects or []))
        prev = self.snapshot
        self.snapshot = snap
        tr = self.tracker
        if state.screen_buffer is not None:
            self.frames.publish(state.screen_buffer)
        x, y = snap.x, snap.y

        alive: dict[int, tuple[str, float, float]] = {}
        items: dict[int, tuple[str, float, float]] = {}
        for o in snap.objects:
            if o.category == "Monster":
                alive[o.id] = (o.name, o.position_x, o.position_y)
            elif o.category in K.ITEM_CATEGORIES:
                items[o.id] = (o.name, o.position_x, o.position_y)
        if not first:
            for oid, (name, ox, oy) in tr.alive.items():
                if oid not in alive:
                    tr.add(snap.tic, "kill", f"{name} died")
            for oid, (name, ox, oy) in tr.items.items():
                if oid not in items and math.hypot(ox - x, oy - y) < 96:
                    info = K.item_info(name)
                    tr.add(snap.tic, "pickup", f"picked up {info.label}")
                    if name in K.KEY_COLORS:
                        tr.keys.add(K.KEY_COLORS[name])
                        self.nav.keys = set(tr.keys)
                        tr.add(snap.tic, "key", f"now carrying the {K.KEY_COLORS[name]} key")
                    tr.seen_items.pop(oid, None)
        tr.alive, tr.items = alive, items

        for lab in snap.labels:
            if lab.object_category in K.ITEM_CATEGORIES and lab.object_id in items:
                tr.seen_items[lab.object_id] = {"name": lab.object_name, "x": lab.object_position_x,
                                                "y": lab.object_position_y, "tic": snap.tic}
        text = state.notifications_buffer
        if text:
            for line in str(text).splitlines():
                line = line.strip()
                if line:
                    tr.add(snap.tic, "message", line)
        tr.last_health = v["HEALTH"]
        if tr.last_pos is not None and math.hypot(x - tr.last_pos[0], y - tr.last_pos[1]) > 96:
            tr.add(snap.tic, "teleport", "teleported")
        tr.last_pos = (x, y)
        if first or snap.tic % SEEN_EVERY == 0:
            self.nav.update_heights(state.sectors)  # doors and lifts move
            self.nav.mark_seen(x, y)
            if not tr.trail or math.hypot(x - tr.trail[-1][0], y - tr.trail[-1][1]) > 24:
                tr.trail.append((x, y))
                if len(tr.trail) > 4000:
                    del tr.trail[:1000]
        if prev is not None and v["SECRETCOUNT"] > prev.vars["SECRETCOUNT"]:
            tr.add(snap.tic, "secret", "found a secret area!")

    def refresh_geometry(self) -> None:
        """Pull fresh sector heights (doors, lifts) into the navigation map."""
        if self.game is None or self.nav is None or (self.tracker and self.tracker.finished):
            return
        state = self.game.get_state()
        if state is not None:
            self.nav.update_heights(state.sectors)

    # -------------------------------------------------------------- raw API
    def step(self, buttons: dict[str, float], tics: int = 1) -> None:
        with self.lock:
            self._require_running()
            unknown = set(buttons) - set(BUTTON_NAMES)
            if unknown:
                raise ValueError(f"unknown buttons: {sorted(unknown)}. Valid: {BUTTON_NAMES}")
            try:
                self._advance(buttons, max(1, min(int(tics), 350)))
            except EpisodeOver:
                pass

    def _require_running(self) -> None:
        if not self.running:
            raise RuntimeError("no episode running: POST /api/episode first")

    # --------------------------------------------------------- agent log API
    def add_agent_log(self, entry: dict) -> dict:
        with self.lock:
            self._agent_seq += 1
            entry = {**entry, "seq": self._agent_seq, "time": time.time(),
                     "episode": self.tracker.id if self.tracker else None}
            self.agent_log.append(entry)
            return entry

    def agent_log_after(self, seq: int) -> list[dict]:
        return [e for e in list(self.agent_log) if e["seq"] > seq]


def _resolution(text: str) -> vzd.ScreenResolution:
    name = "RES_" + text.upper()
    if not hasattr(vzd.ScreenResolution, name):
        raise ValueError(f"unsupported resolution {text}; try 640x480, 800x600, 320x240")
    return getattr(vzd.ScreenResolution, name)


def save_png(frame: np.ndarray, path: str | Path) -> None:
    Image.fromarray(frame).save(path)
