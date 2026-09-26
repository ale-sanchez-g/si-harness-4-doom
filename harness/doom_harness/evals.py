"""Decision-quality checks: fixed game situations with a known correct action.

Run them against any model/playbook without a game server:

    python -m doom_harness eval --model granite4.2:3b --playbook default

Every case is a real-shaped observation plus (optionally) some history. A case
passes when the model picks one of the expected actions. Use it to compare
playbooks or models before spending an hour watching them play.
"""

from __future__ import annotations

import copy
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .actions import TurnView
from .llm import LLM
from .memory import Memory, StepRecord
from .policies import LLMPolicy
from .prompts import Playbook, system_prompt
from .telemetry import Tracer

BASE_OBS: dict = {
    "episode": {"id": "eval", "scenario": "freedoom2", "title": "Freedoom: Phase 2", "map": "MAP01", "skill": 3,
                "tic": 350, "time": 10.0, "finished": False, "end_reason": None,
                "goal": "Survive, kill monsters and find the level exit (usually a switch on a wall or a special "
                        "floor). Open doors, collect weapons, ammo, armor, health and keys on the way.",
                "campaign": True,
                "commands": ["attack", "turn", "face", "move", "goto", "explore", "use", "retreat", "dodge",
                             "select_weapon", "wait", "goto_exit"],
                "tips": ["Doors open with 'use'. Coloured doors need the matching key."], "explored_percent": 20.0},
    "player": {"x": 0.0, "y": 0.0, "angle": 0.0, "health": 100, "armor": 0, "weapon": "pistol", "weapon_slot": 2,
               "ammo": 50, "weapons": [{"slot": 1, "name": "fist", "ammo": None, "usable": True},
                                       {"slot": 2, "name": "pistol", "ammo": 50, "usable": True}],
               "ammo_by_type": {"bullets": 50, "shells": 0, "rockets": 0, "cells": 0}, "keys": [], "kills": 2,
               "items": 0, "secrets": 0, "damage_dealt": 40, "damage_taken": 0, "attack_ready": True, "dead": False,
               "on_damaging_floor": False},
    "enemies": [], "items": [], "known_items": [], "hazards": [], "projectiles": [], "heard": [],
    "walls": {name: {"distance": d, "blocked_by": "wall"} for name, d in
              [("front", 400), ("front_left", 250), ("left", 200), ("back_left", 150), ("back", 300),
               ("back_right", 150), ("right", 180), ("front_right", 260)]},
    "exit": None, "doors": [], "switches": [], "recent_events": [],
}


def monster(id_: int, name: str, distance: int, bearing: float, threat: int = 1, note: str = "") -> dict:
    return {"id": id_, "name": name, "distance": distance, "bearing": bearing, "aimed": abs(bearing) < 2,
            "threat": threat, "attack": "hitscan", "note": note, "height": 0, "screen_width": 30}


def pickup(id_: int, name: str, kind: str, label: str, distance: int, bearing: float) -> dict:
    return {"id": id_, "name": name, "kind": kind, "label": label, "distance": distance, "bearing": bearing,
            "value": 5, "useful": True, "path_distance": int(distance * 1.1)}


ZOMBIE = monster(11, "Zombieman", 700, 19.0, 1, "weak zombie with a rifle")
IMP = monster(12, "DoomImp", 450, -25.0, 2, "imp, throws fireballs")
MEDIKIT = pickup(21, "Medikit", "health", "medikit (+25 health)", 220, -30.0)
SHOTGUN = pickup(22, "Shotgun", "weapon", "shotgun", 260, 15.0)
CLIP = pickup(23, "Clip", "ammo", "bullet clip", 270, 13.0)
EXIT = {"distance": 600, "bearing": -30.0, "path_distance": 900, "type": "switch", "secret": False, "x": 0, "y": 0}


@dataclass
class EvalCase:
    name: str
    expect: set[str]
    changes: dict = field(default_factory=dict)
    history: list[StepRecord] = field(default_factory=list)
    expect_arg: str | None = None

    def observation(self) -> dict:
        obs = copy.deepcopy(BASE_OBS)
        for key, value in self.changes.items():
            if isinstance(value, dict) and isinstance(obs.get(key), dict):
                obs[key].update(value)
            else:
                obs[key] = value
        return obs

    def memory(self) -> Memory:
        m = Memory()
        for rec in self.history:
            m.add(rec)
        return m


CASES: list[EvalCase] = [
    EvalCase("enemy in view, items around", {"attack"}, {"enemies": [ZOMBIE], "items": [SHOTGUN]}),
    EvalCase("new enemy right after a kill", {"attack"}, {"enemies": [ZOMBIE], "items": [CLIP]}, history=[
        StepRecord(6, "attack", "E1", "completed", "killed Zombieman (3 shots)", kills=1),
        StepRecord(7, "pickup", "I1", "interrupted",
                   "stopped on the way to the bullet clip: a Zombieman came into view")]),
    EvalCase("item and no enemy", {"pickup"}, {"items": [SHOTGUN]}),
    EvalCase("item after a kill", {"pickup"}, {"items": [CLIP]}, history=[
        StepRecord(4, "attack", "E1", "completed", "killed Zombieman (2 shots)", kills=1)]),
    EvalCase("low health and a medikit", {"pickup"}, {"items": [MEDIKIT], "player": {"health": 25}}),
    EvalCase("low health, imp close, no health", {"retreat"},
             {"enemies": [monster(12, "DoomImp", 180, 5.0, 2, "imp, throws fireballs")],
              "player": {"health": 20}}),
    EvalCase("fireball incoming", {"dodge"}, {"enemies": [IMP], "projectiles": [
        {"id": 90, "name": "DoomImpBall", "distance": 110, "bearing": 3.0, "incoming": True}]}),
    EvalCase("exit known, area clear", {"goto_exit"}, {"exit": EXIT}),
    EvalCase("nothing around", {"explore"}, {}),
    EvalCase("hurt by something unseen", {"turn"}, {}, history=[
        StepRecord(3, "explore", "none", "interrupted", "took 12 damage from an unseen attacker",
                   damage_taken=12)]),
    EvalCase("two enemies", {"attack"}, {"enemies": [monster(11, "Zombieman", 500, -5.0, 1, "weak zombie with a rifle"),
                                                      IMP]}),
]


@dataclass
class EvalResult:
    case: str
    ok: bool
    action: str
    arg: str
    thought: str
    latency: float
    source: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def run_evals(llm: LLM, playbook: Playbook, reasoning: bool = True, repeat: int = 1,
              cases: list[EvalCase] | None = None, out: Callable[[str], None] = print,
              tracer: Tracer | None = None) -> dict:
    tracer = tracer or Tracer(enabled=False)
    results: list[EvalResult] = []
    for case in cases or CASES:
        obs = case.observation()
        policy = LLMPolicy(llm, system_prompt(playbook, obs, reasoning), reasoning=reasoning,
                           reminder=playbook.reminder, facts=playbook.facts, retries=0)
        for sample in range(repeat):
            t0 = time.monotonic()
            with tracer.span("eval.case", case=case.name, sample=sample + 1,
                             expected=sorted(case.expect)) as span:
                d = policy.decide(TurnView(obs, allowed=playbook.actions), case.memory(), turn=8)
                ok = d.source == "llm" and d.resolved.action in case.expect and \
                    (case.expect_arg is None or d.resolved.arg == case.expect_arg)
                span.set(passed=ok, action=d.resolved.action, arg=d.resolved.arg, thought=d.thought,
                         prompt_tokens=d.prompt_tokens, completion_tokens=d.completion_tokens)
                if not ok:
                    span.error(f"expected {'/'.join(sorted(case.expect))}, got {d.resolved.action}")
            r = EvalResult(case.name, ok, d.resolved.action, d.resolved.arg, d.thought,
                           time.monotonic() - t0, d.source, d.prompt_tokens, d.completion_tokens)
            results.append(r)
            out(f"{'PASS' if ok else 'FAIL'} {case.name:<34} {r.latency:5.1f}s -> {r.action} {r.arg}"
                f"{'' if d.source == 'llm' else ' [' + d.source + ']'} | {r.thought}")
    passed = sum(r.ok for r in results)
    lat = sorted(r.latency for r in results)
    summary = {"model": llm.model, "passed": passed, "total": len(results),
               "accuracy": round(passed / len(results), 3) if results else 0.0,
               "median_latency": round(lat[len(lat) // 2], 2) if lat else 0.0,
               "prompt_tokens": sum(r.prompt_tokens for r in results),
               "completion_tokens": sum(r.completion_tokens for r in results),
               "cases": [asdict(r) for r in results]}
    out(f"\n{passed}/{len(results)} correct ({summary['accuracy']:.0%}), "
        f"median {summary['median_latency']}s per decision")
    return summary
