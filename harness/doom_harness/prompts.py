"""Prompt construction: the playbook (system prompt) and the per-turn situation report.

The system prompt is built once per episode and never changes, so Ollama can keep
it in its KV cache; only the short situation report is new each turn. That keeps
a 3B model fast even on a CPU.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .actions import ACTIONS, TurnView
from .config import DEFAULT_PLAYBOOK_DIR
from .memory import Memory

ANSWER_FORMAT = """Reply with ONE JSON object and nothing else:
{"Thought": "<one short sentence>", "action": "<action name>", "arg": "<argument or none>"}
- attack: arg is the enemy tag (E1, E2, ... or B1 for a barrel)
- pickup: arg is the item tag (I1, I2, ...)
- turn: arg is left, right or around
- move: arg is forward, backward, left or right
- dodge: arg is left or right
- switch_weapon: arg is the weapon name
- every other action: arg is none"""

ANSWER_FORMAT_NO_THOUGHT = """Reply with ONE JSON object and nothing else:
{"action": "<action name>", "arg": "<argument or none>"}
- attack: arg is the enemy tag (E1, E2, ...); pickup: arg is the item tag (I1, I2, ...)
- turn: left, right or around; move: forward, backward, left or right; dodge: left or right
- switch_weapon: arg is the weapon name; every other action: arg is none"""


def where(bearing: float, distance: float) -> str:
    dist = int(round(distance / 10.0) * 10)
    b = float(bearing)
    if abs(b) <= 12:
        side = "straight ahead"
    elif abs(b) >= 150:
        side = "behind you"
    else:
        side = f"{abs(b):.0f} deg to your {'left' if b > 0 else 'right'}"
    return f"{dist} away, {side}"


@dataclass
class Playbook:
    """A playbook file: the system prompt template plus an optional per-turn reminder.

    Everything after a ``# TURN REMINDER`` heading is not part of the system
    prompt; it is appended to every situation report instead (small models pay
    most attention to the end of the prompt).
    """

    template: str
    reminder: str = ""
    # Optional front matter ("---\nactions: attack, explore\n---") limiting what the
    # model may choose. Smaller models do better with a shorter menu.
    actions: list[str] | None = None
    # "facts: true" adds a pre-digested FACTS line to every report (for tiny models).
    facts: bool = False

    @classmethod
    def parse(cls, text: str) -> "Playbook":
        actions, facts = None, False
        front = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.S)
        if front:
            text = text[front.end():]
            for line in front.group(1).splitlines():
                key, _, value = line.partition(":")
                key = key.strip().lower()
                if key == "actions":
                    actions = [a.strip() for a in value.split(",") if a.strip()]
                elif key == "facts":
                    facts = value.strip().lower() in ("true", "yes", "1", "on")
        parts = re.split(r"^#\s*TURN REMINDER\s*$", text, maxsplit=1, flags=re.M)
        return cls(parts[0].strip(), parts[1].strip() if len(parts) > 1 else "", actions, facts)


def load_playbook(path: Path) -> Playbook:
    return Playbook.parse(path.read_text(encoding="utf-8"))


def scenario_notes(scenario: str, playbook_dir: Path = DEFAULT_PLAYBOOK_DIR) -> str:
    p = playbook_dir / "scenarios" / f"{scenario}.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def system_prompt(playbook: Playbook | str, obs: dict, reasoning: bool = True,
                  playbook_dir: Path = DEFAULT_PLAYBOOK_DIR) -> str:
    """Fill the playbook template for this episode (stable across turns)."""
    if isinstance(playbook, str):
        playbook = Playbook.parse(playbook)
    ep = obs.get("episode", {})
    view = TurnView(obs, allowed=playbook.actions)
    actions = "\n".join(f"- {a.usage}: {a.description}" for a in view.scenario_actions)
    goal = ep.get("goal", "Survive and kill monsters.")
    tips = ep.get("tips") or []
    if tips:
        goal += "\n" + "\n".join(f"- {t}" for t in tips)
    fmt = ANSWER_FORMAT if reasoning else ANSWER_FORMAT_NO_THOUGHT
    notes = scenario_notes(ep.get("scenario", ""), playbook_dir)
    text = playbook.template
    for key, value in {"{goal}": goal, "{actions}": actions, "{answer_format}": fmt,
                       "{scenario_notes}": notes or "(none)"}.items():
        text = text.replace(key, value)
    return text.strip()


def facts_line(view: TurnView, hints: list[str]) -> str:
    """The playbook's checklist answered by the harness: small models read literal
    values reliably but mis-derive them from prose ("an enemy is in view, so DANGER")."""
    obs = view.obs
    yes = {True: "yes", False: "no"}
    danger = any(p.get("incoming") for p in obs.get("projectiles", []))
    ex = obs.get("exit")
    enemies = sum(1 for t in view.enemies if t.tag.startswith("E"))
    return (f"FACTS: DANGER {yes[danger]} | LOW HEALTH {yes[obs['player']['health'] <= 30]} | "
            f"ENEMIES {enemies} | HINT {yes[bool(hints)]} | ITEMS {len(view.items)} | "
            f"EXIT {yes[bool(ex) and ex.get('path_distance') is not None]}")


def situation_report(view: TurnView, memory: Memory, turn: int, history: int = 4,
                     reminder: str = "", facts: bool = False) -> str:
    obs = view.obs
    ep, p = obs["episode"], obs["player"]
    lines = [f"TURN {turn} | {ep['scenario']} {ep['map']} | game time {ep['time']}s"]
    # History first, current state last: small models anchor on what they read last,
    # and reasoning from old turns ("I already killed it") is a common failure.
    recent = memory.recent(history)
    if recent:
        lines.append("YOUR LAST TURNS (the past, may be out of date):")
        lines += [f"  {r.summary()}" for r in recent]
    lines.append("NOW:")

    weapon = p["weapon"].replace("_", " ")
    if p.get("ammo") is not None:
        weapon += f" ({p['ammo']} ammo)"
    others = [w["name"].replace("_", " ") for w in p["weapons"]
              if w["usable"] and w["name"] not in (p["weapon"], "fist")]
    # ~1B models misread bare numbers ("health 100 is low"), so FACTS-style playbooks get
    # a label. The 3B model reasons better from the number and rule 2's "30 or less": with
    # "20 (LOW HEALTH!)" it attacked instead of retreating (eval: 1/6 vs 6/6 without).
    health = p["health"]
    you = f"YOU: health {health}"
    if facts:
        you += " (LOW HEALTH!)" if health <= 30 else (" (hurt)" if health < 70 else " (good)")
    you += f", armor {p['armor']}, weapon {weapon}"
    if others:
        you += f", also carrying {', '.join(others)}"
    you += f", kills {p['kills']}"
    if p.get("keys"):
        you += f", keys: {', '.join(p['keys'])}"
    lines.append(you)

    if view.enemies:
        lines.append(f"ENEMIES IN VIEW: {len(view.enemies)} (attack them)")
        for t in view.enemies:
            e = t.info
            tag = f"  {t.tag} {e['name']} - {where(e['bearing'], e['distance'])}"
            if e.get("note"):
                tag += f" - {e['note']}"
            if e.get("aimed"):
                tag += " [in your crosshair]"
            lines.append(tag)
    else:
        lines.append("ENEMIES IN VIEW: none")
    for h in obs.get("heard", [])[:3]:
        lines.append(f"  (heard, not visible) {h['name']} - {where(h['bearing'], h['distance'])}")
    for pr in obs.get("projectiles", []):
        if pr.get("incoming"):
            lines.append(f"DANGER: a {pr['name']} is flying at you ({where(pr['bearing'], pr['distance'])})!")
            break

    if view.items:
        lines.append(f"USEFUL ITEMS: {len(view.items)} (pickup them when no enemy is in view)")
        for t in view.items:
            i = t.info
            text = f"  {t.tag} {i['label']} - {where(i['bearing'], i['path_distance'] or i['distance'])}"
            if t.remembered:
                text += " (seen earlier)"
            lines.append(text)
    else:
        lines.append("USEFUL ITEMS: none in view")

    walls = obs.get("walls", {})
    if walls:
        def w(name: str) -> str:
            d = walls[name]
            return f"{d['distance']}" + ("" if d["blocked_by"] in ("open", "wall") else f" ({d['blocked_by']})")
        lines.append(f"SPACE AROUND YOU (units until blocked): ahead {w('front')}, left {w('left')}, "
                     f"right {w('right')}, behind {w('back')}")
    for d in obs.get("doors", [])[:2]:
        kind = f"{d['key']} door" if d.get("key") else "door"
        extra = " - LOCKED, you need the " + d["key"] + " key" if d.get("locked") else ""
        lines.append(f"CLOSED {kind.upper()}: {where(d['bearing'], d['distance'])}{extra}")
    ex = obs.get("exit")
    if ex:
        walk = ex.get("path_distance")
        reach = f"{walk} units walk" if walk is not None else "no path yet (find a door, key or switch)"
        lines.append(f"EXIT: {where(ex['bearing'], ex['distance'])}, {reach}")
    elif ep.get("campaign"):
        lines.append(f"EXIT: not found yet (explored {ep.get('explored_percent', 0):.0f}% of the map)")

    # Only what the previous command caused: stale news ("Zombieman died") next to a
    # new Zombieman in view makes small models think the coast is clear. Kills are
    # already in YOUR LAST TURNS; pickups arrive as game messages.
    events = [e["text"] for e in memory.last_events
              if e["type"] in ("message", "key", "secret") and not e["text"].startswith("+")]
    if events:
        lines.append("JUST HAPPENED: " + "; ".join(dict.fromkeys(events[-5:])))

    hints = memory.hints(obs)
    for hint in hints:
        lines.append(f"HINT: {hint}")

    if facts:
        lines.append(facts_line(view, hints))
    if reminder:
        lines.append(reminder)
    choices = ", ".join(view.action_names)
    lines.append(f"Available actions now: {choices}.")
    lines.append("What do you do?")
    return "\n".join(lines)


def describe_actions() -> str:
    return "\n".join(f"- {a.usage}: {a.description}" for a in ACTIONS.values())
