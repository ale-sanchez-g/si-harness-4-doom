"""High-level commands ("macro actions") executed tic-by-tic inside the server.

An agent sends one command such as ``attack`` or ``explore``; the server runs a
small control loop (aiming, path following, door opening...) until the command
completes, fails, or is *interrupted* by something the agent should know about
(a new enemy appears, it takes damage out of nowhere). The result reports what
happened plus a fresh observation, so one HTTP round-trip == one agent turn.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import knowledge as K
from .navigation import CELL_DOOR, CELL_FREE, Feature, Path
from .perception import aim_tolerance, bearing_to, norm_angle, observe, weapons_owned
from .session import DoomSession, EpisodeOver

COMMAND_SPECS: list[dict] = [
    {"name": "attack", "params": ["target_id?", "duration?"],
     "description": "Aim at an enemy (or barrel) and shoot until it dies, disappears or `duration` "
                    "seconds pass (default 2). Without target_id the closest visible enemy is used. "
                    "Switches weapon automatically when out of ammo."},
    {"name": "turn", "params": ["degrees | direction"],
     "description": "Turn in place. degrees > 0 turns left, < 0 right. Or direction=left|right|around."},
    {"name": "face", "params": ["target_id | x,y"], "description": "Turn to face an object or a map point."},
    {"name": "move", "params": ["direction", "distance?"],
     "description": "Step forward/backward/left/right (strafe) up to `distance` units (default 128). "
                    "Stops early when blocked."},
    {"name": "goto", "params": ["target_id | x,y", "duration?"],
     "description": "Walk to an object (e.g. an item to pick it up) or a map point using path finding. "
                    "Opens doors on the way."},
    {"name": "goto_exit", "params": ["duration?"],
     "description": "Walk to the level exit (once it has been seen) and activate it."},
    {"name": "explore", "params": ["duration?"],
     "description": "Walk towards the closest unexplored area for `duration` seconds (default 5), "
                    "opening doors and pressing unused switches when nothing else is left."},
    {"name": "use", "params": [],
     "description": "Open the door / press the switch in front of you (walks up to it if it is close)."},
    {"name": "retreat", "params": ["distance?"],
     "description": "Back away from the closest enemy while facing it (default 192 units)."},
    {"name": "dodge", "params": ["direction?"],
     "description": "Quick sidestep left/right (auto picks the side with more room)."},
    {"name": "select_weapon", "params": ["weapon"],
     "description": "Switch weapon by name (pistol, shotgun, chaingun, ...) or slot number 1-7."},
    {"name": "wait", "params": ["duration?"], "description": "Do nothing for `duration` seconds (default 0.5)."},
]

AUTOAIM_RANGE = 900.0  # a bit below Doom's 1024-unit auto-aim search distance

MOVE_BUTTONS = {"forward": "MOVE_FORWARD", "backward": "MOVE_BACKWARD",
                "left": "MOVE_LEFT", "right": "MOVE_RIGHT"}


@dataclass
class CommandArgs:
    command: str
    target_id: int | None = None
    x: float | None = None
    y: float | None = None
    degrees: float | None = None
    direction: str | None = None
    distance: float | None = None
    duration: float | None = None
    weapon: str | None = None
    interrupt_on_enemy: bool = True
    interrupt_on_damage: bool = True
    auto_weapon: bool = True  # attack picks the best weapon for the distance


class CommandFailed(Exception):
    pass


class _Context:
    """Book-keeping for one command execution (interrupt rules, stats baseline)."""

    def __init__(self, session: DoomSession, args: CommandArgs):
        snap = session.snapshot
        assert snap is not None and session.tracker is not None
        self.session = session
        self.args = args
        self.start_tic = snap.tic
        self.start_vars = dict(snap.vars)
        self.start_pos = (snap.x, snap.y)
        self.start_event = len(session.tracker.events)
        self.known_enemies = {lab.object_id for lab in snap.labels if lab.object_category == "Monster"}
        self.enemies_at_start = bool(self.known_enemies)

    def interrupt_reason(self) -> str | None:
        snap = self.session.snapshot
        assert snap is not None
        if self.args.interrupt_on_enemy:
            for lab in snap.labels:
                if lab.object_category == "Monster" and lab.object_id not in self.known_enemies:
                    return f"enemy spotted: {lab.object_name}"
        if self.args.interrupt_on_damage and not self.enemies_at_start:
            taken = snap.vars["DAMAGE_TAKEN"] - self.start_vars["DAMAGE_TAKEN"]
            if taken >= 8:
                return f"took {taken:.0f} damage from something"
        return None


class CommandRunner:
    def __init__(self, session: DoomSession):
        self.s = session

    # ------------------------------------------------------------- entry point
    def run(self, args: CommandArgs) -> dict:
        s = self.s
        with s.lock:
            s._require_running()
            assert s.tracker is not None and s.scenario is not None
            ctx = _Context(s, args)
            handler = getattr(self, f"_cmd_{args.command}", None)
            if s.tracker.finished:
                status, reason = "episode_over", s.tracker.end_reason or "finished"
            elif handler is None:
                status, reason = "failed", f"unknown command '{args.command}'"
            elif args.command not in s.scenario.commands:
                status, reason = "failed", (f"'{args.command}' is not allowed in scenario "
                                            f"{s.scenario.name}; allowed: {', '.join(s.scenario.commands)}")
            else:
                s.current_command = args.command
                s.tracker.commands_run += 1
                try:
                    status, reason = handler(args, ctx)
                except EpisodeOver as exc:
                    status, reason = "episode_over", str(exc)
                except CommandFailed as exc:
                    status, reason = "failed", str(exc)
                finally:
                    s.current_command = None
            return self._result(args, ctx, status, reason)

    def _result(self, args: CommandArgs, ctx: _Context, status: str, reason: str) -> dict:
        s = self.s
        snap, tr = s.snapshot, s.tracker
        assert snap is not None and tr is not None
        v, sv = snap.vars, ctx.start_vars
        return {
            "command": args.command,
            "status": status,
            "reason": reason,
            "tics": snap.tic - ctx.start_tic,
            "seconds": round((snap.tic - ctx.start_tic) / K.TICRATE, 2),
            "events": [e.to_dict() for e in tr.events[ctx.start_event:]][-30:],
            "changes": {
                "health": int(v["HEALTH"] - sv["HEALTH"]),
                "armor": int(v["ARMOR"] - sv["ARMOR"]),
                "kills": int(v["KILLCOUNT"] - sv["KILLCOUNT"]),
                "damage_dealt": int(v["DAMAGECOUNT"] - sv["DAMAGECOUNT"]),
                "damage_taken": int(v["DAMAGE_TAKEN"] - sv["DAMAGE_TAKEN"]),
                "moved": int(round(math.hypot(snap.x - ctx.start_pos[0], snap.y - ctx.start_pos[1]))),
            },
            "observation": observe(s),
        }

    # ------------------------------------------------------------- primitives
    @property
    def snap(self):
        assert self.s.snapshot is not None
        return self.s.snapshot

    def tic(self, **buttons: float) -> None:
        self.s._advance(buttons)

    def _labels(self, category: str | None = None) -> dict[int, object]:
        return {lab.object_id: lab for lab in self.snap.labels
                if category is None or lab.object_category == category}

    def _object(self, oid: int):
        for o in self.snap.objects:
            if o.id == oid:
                return o
        return None

    def _bearing(self, x: float, y: float) -> float:
        return bearing_to(self.snap.x, self.snap.y, self.snap.angle, x, y)

    def _dist(self, x: float, y: float) -> float:
        return math.hypot(x - self.snap.x, y - self.snap.y)

    def _turn_towards(self, x: float, y: float, max_step: float = 30.0, **extra: float) -> float:
        """One tic of turning towards (x, y). Returns the bearing *after* the turn."""
        b = self._bearing(x, y)
        step = max(-max_step, min(max_step, b))
        self.tic(TURN_LEFT_RIGHT_DELTA=-step, **extra)
        return b - step

    def _turn_by(self, degrees: float, max_step: float = 20.0) -> None:
        remaining = degrees
        while abs(remaining) > 0.25:
            step = max(-max_step, min(max_step, remaining))
            self.tic(TURN_LEFT_RIGHT_DELTA=-step)
            remaining -= step

    def _face(self, x: float, y: float, tolerance: float = 2.0, max_tics: int = 12) -> None:
        for _ in range(max_tics):
            if abs(self._bearing(x, y)) <= tolerance:
                return
            self._turn_towards(x, y)

    def _select_slot(self, slot: int) -> bool:
        if int(self.snap.vars["SELECTED_WEAPON"]) == slot:
            return True
        self.tic(**{f"SELECT_WEAPON{slot}": 1})
        for _ in range(35):
            if int(self.snap.vars["SELECTED_WEAPON"]) == slot:
                return True
            self.tic()
        return int(self.snap.vars["SELECTED_WEAPON"]) == slot

    def _weapon_ok(self, slot: int, distance: float) -> bool:
        w = K.WEAPON_SLOTS.get(slot)
        owned = {o["slot"]: o for o in weapons_owned(self.snap.vars)}
        if w is None or slot not in owned or not owned[slot]["usable"]:
            return False
        return w.min_range <= distance <= w.max_range or (slot == 1 and len(owned) == 1)

    def _ensure_weapon(self, distance: float, best: bool = False) -> str | None:
        """Switch to a sensible weapon for this distance.

        With ``best`` the most powerful suitable weapon is chosen; otherwise we only
        switch when the current weapon is unusable (no ammo, rockets too close...).
        Returns the new weapon name when a switch happened.
        """
        current = int(self.snap.vars["SELECTED_WEAPON"])
        if not best and self._weapon_ok(current, distance):
            return None
        for slot in K.WEAPON_PREFERENCE:
            if self._weapon_ok(slot, distance):
                if slot == current:
                    return None
                return K.WEAPON_SLOTS[slot].name if self._select_slot(slot) else None
        # Nothing ideal: anything with ammo that will not blow us up, else fists.
        owned = {o["slot"]: o for o in weapons_owned(self.snap.vars)}
        if current in owned and owned[current]["usable"] and \
                K.WEAPON_SLOTS[current].min_range <= distance:
            return None
        for slot in K.WEAPON_PREFERENCE:
            o = owned.get(slot)
            if o and o["usable"] and K.WEAPON_SLOTS[slot].min_range <= distance:
                return K.WEAPON_SLOTS[slot].name if self._select_slot(slot) else None
        return None

    # --------------------------------------------------------------- commands
    def _cmd_wait(self, a: CommandArgs, ctx: _Context):
        tics = int(_clamp(a.duration or 0.5, 0.03, 5.0) * K.TICRATE)
        for _ in range(tics):
            self.tic()
            reason = ctx.interrupt_reason()
            if reason:
                return "interrupted", reason
        return "completed", f"waited {tics / K.TICRATE:.1f}s"

    def _cmd_turn(self, a: CommandArgs, ctx: _Context):
        deg = a.degrees
        if deg is None:
            deg = {"left": 90.0, "right": -90.0, "around": 180.0, "back": 180.0}.get((a.direction or "").lower())
        if deg is None:
            raise CommandFailed("turn needs degrees or direction=left|right|around")
        deg = norm_angle(float(deg)) if abs(float(deg)) != 180 else 180.0
        self._turn_by(deg)
        self.tic()  # let the renderer catch up so labels match the new view
        side = "left" if deg > 0 else "right"
        return "completed", f"turned {side} {abs(deg):.0f} degrees"

    def _cmd_face(self, a: CommandArgs, ctx: _Context):
        if a.target_id is not None:
            obj = self._object(a.target_id)
            if obj is None:
                raise CommandFailed(f"object {a.target_id} not found")
            x, y, what = obj.position_x, obj.position_y, obj.name
        elif a.x is not None and a.y is not None:
            x, y, what = a.x, a.y, f"({a.x:.0f}, {a.y:.0f})"
        else:
            raise CommandFailed("face needs target_id or x,y")
        self._face(x, y, tolerance=1.0, max_tics=15)
        self.tic()
        return "completed", f"facing {what}"

    def _cmd_select_weapon(self, a: CommandArgs, ctx: _Context):
        if a.weapon is None:
            raise CommandFailed("select_weapon needs weapon (name or slot)")
        text = str(a.weapon).strip().lower().replace(" ", "_")
        slot = int(text) if text.isdigit() else getattr(K.WEAPON_BY_NAME.get(text), "slot", None)
        if slot is None:
            raise CommandFailed(f"unknown weapon '{a.weapon}'; use one of {sorted(K.WEAPON_BY_NAME)}")
        owned = {o["slot"]: o for o in weapons_owned(self.snap.vars)}
        if slot not in owned:
            raise CommandFailed(f"you do not have the {K.WEAPON_SLOTS[slot].name}")
        if not owned[slot]["usable"]:
            raise CommandFailed(f"the {K.WEAPON_SLOTS[slot].name} has no ammo")
        if not self._select_slot(slot):
            raise CommandFailed("weapon switch did not complete")
        return "completed", f"now holding the {K.WEAPON_SLOTS[slot].name}"

    def _cmd_attack(self, a: CommandArgs, ctx: _Context):
        tr = self.s.tracker
        assert tr is not None
        monsters = self._labels("Monster")
        barrels = self._labels("Hazard")
        target = a.target_id
        if target is None:
            if not monsters:
                raise CommandFailed("no enemy in view")
            target = min(monsters.values(), key=lambda l: self._dist(l.object_position_x, l.object_position_y)).object_id
        is_barrel = target in barrels
        if not is_barrel and target not in monsters and target not in tr.alive:
            raise CommandFailed(f"target {target} is not a living enemy in view (dead or unknown id)")
        lab = monsters.get(target) or barrels.get(target)
        name = lab.object_name if lab is not None else tr.alive[target][0]
        tics = int(_clamp(a.duration or 2.0, 0.2, 6.0) * K.TICRATE)
        shots, lost, switched = 0, 0, None
        if a.auto_weapon and lab is not None:
            switched = self._ensure_weapon(self._dist(lab.object_position_x, lab.object_position_y), best=True)
        for _ in range(tics):
            if not is_barrel and target not in tr.alive:
                return "completed", f"killed {name} ({shots} shots)"
            labels = self._labels()
            lab = labels.get(target)
            if lab is None or lab.object_category not in ("Monster", "Hazard"):
                if is_barrel:
                    return "completed", f"the barrel exploded ({shots} shots)"
                lost += 1
                if lost > 24:
                    return "completed", f"lost sight of {name} after {shots} shots"
                _, ox, oy = tr.alive[target]
                self._turn_towards(ox, oy)
                continue
            lost = 0
            tx, ty = lab.object_position_x, lab.object_position_y
            dist = self._dist(tx, ty)
            switched = self._ensure_weapon(dist) or switched
            slot = int(self.snap.vars["SELECTED_WEAPON"])
            owned = {o["slot"]: o for o in weapons_owned(self.snap.vars)}
            if slot not in owned or not owned[slot]["usable"]:
                raise CommandFailed("out of ammo for every weapon")
            bearing = self._bearing(tx, ty)
            step = max(-35.0, min(35.0, bearing))
            aimed = abs(bearing - step) <= aim_tolerance(dist)
            ready = self.snap.vars["ATTACK_READY"] > 0
            buttons: dict[str, float] = {"TURN_LEFT_RIGHT_DELTA": -step}
            if aimed and ready:
                buttons["ATTACK"] = 1
                shots += 1
            if slot == 1 and dist > 56:  # melee: close the distance
                buttons.update(MOVE_FORWARD=1, SPEED=1)
            elif dist > AUTOAIM_RANGE and not is_barrel:
                # Doom only auto-aims (vertically) within 1024 units; further away
                # shots fly level and miss targets on other floors: walk closer.
                buttons.update(MOVE_FORWARD=1)
            self.tic(**buttons)
        alive = is_barrel or target in tr.alive
        extra = f", switched to {switched}" if switched else ""
        where = f" ({self._dist(*tr.alive[target][1:]):.0f} away)" if (alive and not is_barrel) else ""
        return "completed", (f"fired {shots} shots at {name}" + (f", it is still alive{where}" if alive else ", it died")
                             + extra)

    def _cmd_move(self, a: CommandArgs, ctx: _Context):
        direction = (a.direction or "forward").lower()
        if direction not in MOVE_BUTTONS:
            raise CommandFailed("direction must be forward, backward, left or right")
        distance = _clamp(a.distance or 128.0, 8.0, 1024.0)
        return self._walk(MOVE_BUTTONS[direction], distance, ctx, label=f"moved {direction}")

    def _walk(self, button: str, distance: float, ctx: _Context, label: str,
              face: tuple[float, float] | None = None):
        sx, sy = self.snap.x, self.snap.y
        hist: deque[tuple[float, float]] = deque(maxlen=8)
        for t in range(int(distance / 5) + 25):
            moved = math.hypot(self.snap.x - sx, self.snap.y - sy)
            if moved >= distance:
                break
            buttons = {button: 1, "SPEED": 1}
            if face is not None:
                b = self._bearing(*face)
                buttons["TURN_LEFT_RIGHT_DELTA"] = -max(-20.0, min(20.0, b))
            self.tic(**buttons)
            hist.append((self.snap.x, self.snap.y))
            if len(hist) == hist.maxlen and math.dist(hist[0], hist[-1]) < 6:
                return "failed", f"{label} {moved:.0f} units, then got blocked"
            reason = ctx.interrupt_reason()
            if reason:
                return "interrupted", f"{label} {moved:.0f} units; {reason}"
        moved = math.hypot(self.snap.x - sx, self.snap.y - sy)
        return "completed", f"{label} {moved:.0f} units"

    def _cmd_retreat(self, a: CommandArgs, ctx: _Context):
        monsters = self._labels("Monster")
        distance = _clamp(a.distance or 192.0, 32.0, 512.0)
        ctx.args.interrupt_on_enemy = False
        if not monsters:
            return self._walk("MOVE_BACKWARD", distance, ctx, label="backed off")
        near = min(monsters.values(), key=lambda l: self._dist(l.object_position_x, l.object_position_y))
        face = (near.object_position_x, near.object_position_y)
        status, reason = self._walk("MOVE_BACKWARD", distance, ctx, label=f"retreated from {near.object_name}",
                                    face=face)
        if status == "failed":  # wall behind us: slide sideways instead
            side = "MOVE_LEFT" if self.s.nav.raycast(self.snap.x, self.snap.y, self.snap.angle + 90)[0] > \
                self.s.nav.raycast(self.snap.x, self.snap.y, self.snap.angle - 90)[0] else "MOVE_RIGHT"
            return self._walk(side, distance / 2, ctx, label="blocked behind, sidestepped", face=face)
        return status, reason

    def _cmd_dodge(self, a: CommandArgs, ctx: _Context):
        nav = self.s.nav
        assert nav is not None
        direction = (a.direction or "auto").lower()
        if direction not in ("left", "right"):
            left = nav.raycast(self.snap.x, self.snap.y, self.snap.angle + 90)[0]
            right = nav.raycast(self.snap.x, self.snap.y, self.snap.angle - 90)[0]
            direction = "left" if left >= right else "right"
        ctx.args.interrupt_on_enemy = False
        return self._walk(MOVE_BUTTONS[direction], 96.0, ctx, label=f"dodged {direction}")

    def _cmd_use(self, a: CommandArgs, ctx: _Context):
        nav = self.s.nav
        assert nav is not None
        best: Feature | None = None
        best_score = 1e9
        for f in nav.features:
            if f.kind not in ("door", "switch", "exit") or (f.kind == "exit" and f.exit_type != "switch"):
                continue
            d = _dist_to_segment(self.snap.x, self.snap.y, f)
            b = abs(self._bearing(f.x, f.y))
            if d <= 160 and b <= 80:
                score = d + b * 2
                if score < best_score:
                    best, best_score = f, score
        if best is not None:
            for _ in range(40):  # walk up to it
                if _dist_to_segment(self.snap.x, self.snap.y, best) <= 44:
                    break
                self._turn_towards(best.x, best.y, MOVE_FORWARD=1)
            self._face(best.x, best.y)
        return self._press_use(best, ctx)

    def _press_use(self, feature: Feature | None, ctx: _Context):
        nav = self.s.nav
        tr = self.s.tracker
        assert nav is not None and tr is not None
        before = (nav.floor_h.copy(), nav.ceil_h.copy())
        n_events = len(tr.events)
        self.tic(USE=1)
        self.tic()
        for _ in range(12):
            self.tic()
        self.s.refresh_geometry()
        msgs = [e.text for e in tr.events[n_events:] if e.type == "message"]
        moved = (nav.floor_h != before[0]).any() or (nav.ceil_h != before[1]).any()
        if feature is not None and feature.kind in ("switch", "exit"):
            feature.used = True
        if msgs:
            return "completed", "pressed use: " + "; ".join(msgs)
        if moved:
            what = feature.kind if feature else "something"
            return "completed", f"pressed use on the {what}: something moved (door/lift opening)"
        if feature is None:
            return "completed", "pressed use but there was nothing to activate"
        return "completed", f"pressed use on the {feature.kind}"

    # -------------------------------------------------------------- movement
    def _cmd_goto(self, a: CommandArgs, ctx: _Context):
        nav = self.s.nav
        assert nav is not None
        stop: Callable[[], bool] | None = None
        arrive = 20.0
        if a.target_id is not None:
            obj = self._object(a.target_id)
            if obj is None:
                raise CommandFailed(f"object {a.target_id} not found (already picked up or dead?)")
            goal, what = (obj.position_x, obj.position_y), obj.name
            oid = obj.id
            if obj.category in K.ITEM_CATEGORIES:
                stop = lambda: self._object(oid) is None  # noqa: E731 - picked up
                arrive = 8.0
            elif obj.category == "Monster":
                arrive = 96.0
        elif a.x is not None and a.y is not None:
            goal, what = (a.x, a.y), f"({a.x:.0f}, {a.y:.0f})"
        else:
            raise CommandFailed("goto needs target_id or x,y")
        budget = int(_clamp(a.duration or 10.0, 1.0, 30.0) * K.TICRATE)
        status, reason = self._travel(goal, budget, ctx, arrive=arrive, stop=stop)
        if stop is not None and stop():
            return "completed", f"picked up the {what}"
        if status == "completed" and stop is not None:
            return "failed", f"reached the spot but could not pick up the {what} (not needed or out of reach)"
        return status, f"{what}: {reason}"

    def _cmd_goto_exit(self, a: CommandArgs, ctx: _Context):
        nav = self.s.nav
        assert nav is not None
        full = self.s.settings.knowledge == "full"
        exits = [f for f in nav.features_of("exit") if f.seen or full]
        if not exits:
            raise CommandFailed("the exit has not been found yet - explore more")
        exits.sort(key=lambda f: (f.secret, math.hypot(f.x - self.snap.x, f.y - self.snap.y)))
        ctx.args.interrupt_on_enemy = False
        budget = int(_clamp(a.duration or 20.0, 1.0, 60.0) * K.TICRATE)
        for f in exits:
            status, reason = self._travel(f.approach, budget, ctx, arrive=24.0)
            if status != "completed":
                if status in ("interrupted",):
                    return status, reason
                continue
            if f.exit_type in ("switch", "shoot"):
                self._face(f.x, f.y)
                for _ in range(3):
                    if f.exit_type == "shoot":
                        self.tic(ATTACK=1)
                    else:
                        self._walk("MOVE_FORWARD", 16.0, ctx, label="approached", face=(f.x, f.y))
                        self.tic(USE=1)
                    for _ in range(10):
                        self.tic()
            else:  # walk-over exit: step across the line
                self._walk("MOVE_FORWARD", 48.0, ctx, label="crossed", face=(f.x, f.y))
            return "failed", "reached the exit but it did not trigger"
        return "failed", "no path to the exit (a door, key or switch may be needed)"

    def _cmd_explore(self, a: CommandArgs, ctx: _Context):
        nav = self.s.nav
        assert nav is not None
        budget = int(_clamp(a.duration or 5.0, 1.0, 20.0) * K.TICRATE)
        start_pct = nav.explored_fraction()
        end_tic = self.snap.tic + budget
        legs = 0
        while self.snap.tic < end_tic:
            path = nav.nearest_frontier(self.snap.x, self.snap.y, self.snap.angle)
            if path is None:
                switch = self._nearest_switch()
                if switch is None:
                    note = "nothing left to explore" if legs else "no unexplored area reachable"
                    return "completed", note + " (look for switches, keys or the exit)"
                status, reason = self._travel(switch.approach, end_tic - self.snap.tic, ctx, arrive=24.0)
                if status != "completed":
                    switch.used = True  # unreachable for now; do not insist
                    if status == "interrupted":
                        return status, reason
                    continue
                self._face(switch.x, switch.y)
                status, reason = self._press_use(switch, ctx)
                return "completed", "explored everything nearby, " + reason
            legs += 1
            goal = path.points[-1]
            gr, gc = nav.to_cell(*goal)
            leg_end = min(end_tic, self.snap.tic + 50)
            status, reason = self._follow(path, leg_end - self.snap.tic, ctx, arrive=32.0,
                                          stop=lambda: bool(nav.seen[gr, gc]) and self._dist(*goal) < 200)
            if status in ("interrupted", "failed_hard"):
                return "interrupted" if status == "interrupted" else "failed", reason
            if status == "failed":
                nav.mark_unreachable(*goal, radius_cells=2)
        gained = (nav.explored_fraction() - start_pct) * 100
        return "completed", f"explored {gained:.1f}% more of the map ({nav.explored_fraction() * 100:.0f}% total)"

    def _nearest_switch(self) -> Feature | None:
        nav = self.s.nav
        assert nav is not None
        full = self.s.settings.knowledge == "full"
        field = nav.distance_field(self.snap.x, self.snap.y)
        best, best_d = None, math.inf
        for f in nav.features_of("switch"):
            if f.used or not (f.seen or full):
                continue
            if f.key and f.key not in (self.s.tracker.keys if self.s.tracker else set()):
                continue
            d = field.at(*f.approach) if field else None
            if d is not None and d < best_d:
                best, best_d = f, d
        return best

    def _travel(self, goal: tuple[float, float], budget: int, ctx: _Context, arrive: float,
                stop: Callable[[], bool] | None = None):
        """Plan and follow a path, re-planning a few times when stuck."""
        nav = self.s.nav
        assert nav is not None
        end_tic = self.snap.tic + budget
        for attempt in range(4):
            self.s.refresh_geometry()
            path = nav.find_path((self.snap.x, self.snap.y), goal)
            if path is None:
                if self._dist(*goal) <= arrive + 24:
                    return "completed", "arrived"
                return "failed", "no known path (blocked by a locked door, a switch or a ledge?)"
            status, reason = self._follow(path, end_tic - self.snap.tic, ctx, arrive=arrive, stop=stop)
            if status == "completed" or status == "interrupted":
                return status, reason
            if status == "timeout" or self.snap.tic >= end_tic:
                return "completed", f"still on the way ({self._dist(*goal):.0f} units left)"
            if status == "failed_hard":
                return "failed", reason
        return "failed", "could not get there (stuck)"

    def _follow(self, path: Path, budget: int, ctx: _Context, arrive: float,
                stop: Callable[[], bool] | None = None):
        """Pure-pursuit path follower with door handling and stuck recovery.

        Returns (status, reason) with status in completed | interrupted | failed |
        failed_hard | timeout.
        """
        nav = self.s.nav
        assert nav is not None
        pts = path.points
        self.s.last_path = list(pts)
        cum = [0.0]
        for p, q in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.dist(p, q))
        idx = 0
        hist: deque[tuple[float, float]] = deque(maxlen=14)
        unstick = 0
        door_tries: dict[int, int] = {}
        for _ in range(max(1, budget)):
            pos = (self.snap.x, self.snap.y)
            if stop is not None and stop():
                return "completed", "done"
            if math.dist(pos, pts[-1]) <= arrive:
                return "completed", "arrived"
            window = range(idx, min(len(pts), idx + 24))
            idx = min(window, key=lambda k: math.dist(pos, pts[k]))
            if math.dist(pos, pts[idx]) > 160:  # knocked off course / teleported
                return "failed", "pushed off the path"

            door_at = self._door_ahead(pts, idx, cum)
            if door_at is not None:
                sector = nav.cell_sector[nav.to_cell(*pts[door_at])]
                key = int(sector) if sector >= 0 else door_at
                door_tries[key] = door_tries.get(key, 0) + 1
                if door_tries[key] > 3:
                    sid = nav._closed_sector_near(*nav.to_cell(*pts[door_at]))
                    if sid >= 0:
                        nav.failed_doors.add(sid)
                    msgs = [e.text for e in self.s.tracker.events[-5:] if e.type == "message"]
                    return "failed", "a door would not open" + (f" ({msgs[-1]})" if msgs else "")
                self._open_door(pts[door_at], ctx)
                hist.clear()
                continue

            target = pts[-1]
            for k in range(idx, len(pts)):
                if cum[k] - cum[idx] >= 56.0:
                    target = pts[k]
                    break
            b = self._bearing(*target)
            buttons: dict[str, float] = {"TURN_LEFT_RIGHT_DELTA": -max(-25.0, min(25.0, b))}
            if abs(b) < 50:
                buttons.update(MOVE_FORWARD=1, SPEED=1)
            elif abs(b) < 100:
                buttons.update(MOVE_FORWARD=1)
            self.tic(**buttons)

            reason = ctx.interrupt_reason()
            if reason:
                return "interrupted", reason
            if self.s.tracker and self.s.tracker.events and self.s.tracker.events[-1].type == "teleport" \
                    and self.s.tracker.events[-1].tic == self.snap.tic:
                return "failed", "teleported"
            hist.append((self.snap.x, self.snap.y))
            if len(hist) == hist.maxlen and math.dist(hist[0], hist[-1]) < 10:
                unstick += 1
                hist.clear()
                if unstick == 1:
                    self.tic(USE=1)  # maybe a door we did not know about
                    for _ in range(8):
                        self.tic()
                elif unstick == 2:
                    side = "MOVE_LEFT" if (self.snap.tic // 7) % 2 else "MOVE_RIGHT"
                    for _ in range(8):
                        self.tic(**{side: 1, "SPEED": 1})
                elif unstick == 3:
                    for _ in range(8):
                        self.tic(MOVE_BACKWARD=1)
                    return "failed", "stuck, re-planning"
                else:
                    return "failed_hard", "stuck"
        return "timeout", "time budget used"

    def _door_ahead(self, pts: list[tuple[float, float]], idx: int, cum: list[float]) -> int | None:
        nav = self.s.nav
        assert nav is not None
        types = nav.cell_types()
        for k in range(idx, len(pts)):
            if cum[k] - cum[idx] > 72:
                return None
            r, c = nav.to_cell(*pts[k])
            if types[r, c] == CELL_DOOR:
                return k
        return None

    def _open_door(self, door_pt: tuple[float, float], ctx: _Context) -> None:
        nav = self.s.nav
        assert nav is not None
        for _ in range(20):  # get within reach, facing the door
            if self._dist(*door_pt) <= 44:
                break
            b = self._turn_towards(*door_pt, MOVE_FORWARD=1)
            if abs(b) > 60:
                break
        self._face(*door_pt, tolerance=5.0)
        self.tic(USE=1)
        r, c = nav.to_cell(*door_pt)
        for _ in range(40):
            self.tic()
            self.s.refresh_geometry()
            if nav.cell_types()[r, c] == CELL_FREE:
                return


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _dist_to_segment(px: float, py: float, f: Feature) -> float:
    dx, dy = f.x2 - f.x1, f.y2 - f.y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - f.x1) * dx + (py - f.y1) * dy) / L2))
    return math.hypot(px - (f.x1 + t * dx), py - (f.y1 + t * dy))
