"""Short-term memory and guard rails.

Small models loop: they retry the same failing action, walk into walls, or
forget that they were just shot from behind. The memory keeps a compact history
for the prompt and derives *hints* that nudge the model out of those traps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

MOVEMENT = {"explore", "move", "pickup", "goto_exit", "retreat", "dodge"}


@dataclass
class StepRecord:
    turn: int
    action: str
    arg: str
    status: str
    reason: str
    thought: str = ""
    events: list[str] = field(default_factory=list)
    moved: int = 0
    damage_taken: int = 0
    damage_dealt: int = 0
    kills: int = 0
    target_id: int | None = None

    @property
    def no_effect(self) -> bool:
        """Nothing happened: no movement, no damage dealt, no kill, no pickup."""
        return self.moved < 16 and self.damage_dealt == 0 and self.kills == 0 and not self.events

    def summary(self) -> str:
        what = self.action if self.arg in ("", "none") else f"{self.action} {self.arg}"
        text = f"T{self.turn} {what} -> {self.status}: {self.reason}"
        if self.damage_taken:
            text += f" (took {self.damage_taken} damage)"
        return text


class Memory:
    def __init__(self, maxlen: int = 100):
        self.records: deque[StepRecord] = deque(maxlen=maxlen)
        self._banned: dict[int, int] = {}  # object id -> banned until turn
        # Game events produced by the previous command only (older ones confuse the model).
        self.last_events: list[dict] = []

    def add(self, rec: StepRecord) -> None:
        self.records.append(rec)
        if rec.action == "pickup" and rec.status == "failed" and rec.target_id is not None:
            self._banned[rec.target_id] = rec.turn + 12

    def recent(self, n: int) -> list[StepRecord]:
        return list(self.records)[-n:] if n > 0 else []

    def banned_ids(self, turn: int) -> set[int]:
        return {oid for oid, until in self._banned.items() if until >= turn}

    # ----------------------------------------------------------------- hints
    def is_stuck(self) -> bool:
        moves = [r for r in self.recent(4) if r.action in MOVEMENT]
        return len(moves) >= 3 and sum(r.moved for r in moves) < 48

    def repeated_failure(self) -> StepRecord | None:
        last = self.recent(2)
        if len(last) == 2 and all(r.status == "failed" for r in last) and \
                last[0].action == last[1].action and last[0].arg == last[1].arg:
            return last[1]
        return None

    def repeating(self, n: int = 3) -> StepRecord | None:
        """The same action+arg n times in a row without any effect (a small-model loop)."""
        last = self.recent(n)
        if len(last) == n and len({(r.action, r.arg) for r in last}) == 1 and all(r.no_effect for r in last):
            return last[-1]
        return None

    def looping_actions(self, n: int = 4) -> set[str]:
        """Actions to take off the menu for one turn: repeated n times with no effect."""
        rec = self.repeating(n)
        return {rec.action} if rec else set()

    def futile_attacks(self) -> bool:
        """Three attacks in a row that did no damage at all (target out of reach)."""
        last = self.recent(3)
        return len(last) == 3 and all(r.action == "attack" and r.damage_dealt == 0 and r.kills == 0
                                      for r in last)

    def hints(self, obs: dict) -> list[str]:
        hints: list[str] = []
        player = obs.get("player", {})
        last = self.records[-1] if self.records else None
        if self.is_stuck():
            hints.append("You are stuck: your last moves got you nowhere. Try 'turn around', 'use' or 'explore'.")
        failing = self.repeated_failure()
        if failing:
            hints.append(f"'{failing.action} {failing.arg}'".replace(" none", "")
                         + " failed twice in a row. Do something different.")
        loop = self.repeating(3)
        if loop:
            what = loop.action if loop.arg in ("", "none") else f"{loop.action} {loop.arg}"
            hints.append(f"You did '{what}' 3 times in a row and nothing changed. Choose a different action.")
        if self.futile_attacks():
            hints.append("Your last attacks did no damage: the target is out of reach. "
                         "Explore or pickup items instead, and attack when it comes closer.")
        if player.get("on_damaging_floor"):
            hints.append("You are standing on a damaging floor (acid/lava): move off it.")
        elif last and last.damage_taken > 0 and not obs.get("enemies"):
            hints.append("You were hurt but no enemy is in view: it is probably behind you. Turn around.")
        if any(p.get("incoming") for p in obs.get("projectiles", [])):
            hints.append("A projectile is flying at you: dodge!")
        if player.get("health", 100) <= 30:
            hints.append("Health is critical: get health items or retreat from enemies.")
        weapons = player.get("weapons", [])
        if weapons and not any(w.get("usable") and w.get("ammo") is not None for w in weapons):
            fists = any(w.get("name") == "fist" for w in weapons)
            hints.append("You are out of ammo: pick up ammo" + (" or fight with your fists." if fists else
                                                                 ". You cannot attack until you find some."))
        for door in obs.get("doors", []):
            if door.get("locked"):
                hints.append(f"A {door['key']} door is locked: find the {door['key']} key first.")
                break
        ex = obs.get("exit")
        if ex and ex.get("path_distance") is not None and not obs.get("enemies"):
            hints.append("The exit is known and reachable: goto_exit now (rule 6).")
        return hints
