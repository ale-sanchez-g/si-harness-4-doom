"""Playable scenarios: full Freedoom/Doom campaigns plus the classic ViZDoom tasks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import vizdoom as vzd

ALL_COMMANDS = ("attack", "turn", "face", "move", "goto", "explore", "use", "retreat",
                "dodge", "select_weapon", "wait", "goto_exit")

# Aim-and-shoot only (the ViZDoom scenario forbids walking around).
STATIONARY = ("attack", "turn", "face", "select_weapon", "wait")


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    goal: str
    config: str  # ViZDoom .cfg file (bundled scenario or absolute path)
    default_map: str = "MAP01"
    iwad: str | None = None  # IWAD the user must provide (commercial Doom), if any
    campaign: bool = False  # multi-map game with exits, doors and keys
    commands: tuple[str, ...] = ALL_COMMANDS
    tips: tuple[str, ...] = field(default_factory=tuple)

    @property
    def config_path(self) -> str:
        if os.path.isabs(self.config):
            return self.config
        return os.path.join(vzd.scenarios_path, self.config)


def _maps(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(1, n + 1)]


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario(
        "freedoom2", "Freedoom: Phase 2 (full campaign)",
        "Survive, kill monsters and find the level exit (usually a switch on a wall or a "
        "special floor). Open doors, collect weapons, ammo, armor, health and keys on the way.",
        "freedoom2.cfg", "MAP01", campaign=True,
        tips=("Doors open with 'use'. Coloured doors need the matching key.",)),
    Scenario(
        "freedoom1", "Freedoom: Phase 1 (full campaign)",
        "Survive, kill monsters and find the level exit. Open doors and collect items and keys.",
        "freedoom1.cfg", "E1M1", campaign=True),
    Scenario(
        "doom2", "Doom II (requires your own doom2.wad)",
        "Survive, kill monsters and find the level exit.",
        "doom2.cfg", "MAP01", iwad="doom2.wad", campaign=True),
    Scenario(
        "doom", "The Ultimate Doom (requires your own doom.wad)",
        "Survive, kill monsters and find the level exit.",
        "doom.cfg", "E1M1", iwad="doom.wad", campaign=True),
    Scenario(
        "basic", "Basic",
        "A single monster is in the room in front of you. Kill it as fast as possible.",
        "basic.cfg", commands=("attack", "move", "face", "wait")),
    Scenario(
        "defend_the_center", "Defend the center",
        "You stand in the middle of a round arena and cannot move. Monsters come at you from "
        "all sides. Turn and shoot to kill as many as possible. Ammo is limited, do not waste it.",
        "defend_the_center.cfg", commands=STATIONARY),
    Scenario(
        "defend_the_line", "Defend the line",
        "Monsters approach from the far side of the room. Hold your position and kill them "
        "before they reach you.",
        "defend_the_line.cfg", commands=STATIONARY),
    Scenario(
        "deadly_corridor", "Deadly corridor",
        "Reach the green armor at the far end of the corridor. Monsters guard both sides; "
        "kill them as you advance or you will not make it.",
        "deadly_corridor.cfg"),
    Scenario(
        "health_gathering", "Health gathering",
        "The floor is acid and slowly kills you. Keep picking up medikits to survive as long "
        "as possible. There are no monsters.",
        "health_gathering.cfg"),
    Scenario(
        "health_gathering_supreme", "Health gathering (supreme)",
        "The floor is acid. Pick up medikits to survive; avoid the poison vials. The level is "
        "a maze.",
        "health_gathering_supreme.cfg"),
    Scenario(
        "my_way_home", "My way home",
        "Find the green armor vest somewhere in this maze of rooms and pick it up.",
        "my_way_home.cfg"),
    Scenario(
        "take_cover", "Take cover",
        "Monsters at the far wall throw fireballs. You have no weapon: dodge left and right "
        "to survive as long as possible.",
        "take_cover.cfg", commands=("dodge", "move", "wait")),
    Scenario(
        "predict_position", "Predict position",
        "A monster moves across the room. Hit it with a single rocket (lead your shot).",
        "predict_position.cfg", commands=STATIONARY),
]}

CAMPAIGN_MAPS = {
    "freedoom2": _maps("MAP", 32),
    "doom2": _maps("MAP", 32),
    "freedoom1": [f"E{e}M{m}" for e in range(1, 5) for m in range(1, 10)],
    "doom": [f"E{e}M{m}" for e in range(1, 5) for m in range(1, 10)],
}


def next_map(scenario: str, current: str) -> str | None:
    maps = CAMPAIGN_MAPS.get(scenario, [])
    if current.upper() in maps:
        i = maps.index(current.upper())
        return maps[i + 1] if i + 1 < len(maps) else None
    return None
