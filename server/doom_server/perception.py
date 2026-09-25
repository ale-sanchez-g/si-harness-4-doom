"""Turn raw ViZDoom state into a structured observation an agent can reason about.

Conventions used everywhere in the API:
* distances are Doom map units (a player is 32 units wide, a door ~64-128);
* ``bearing`` is the angle to something relative to where the player faces, in
  degrees: 0 = straight ahead, positive = to the LEFT, negative = to the RIGHT;
* ``path_distance`` is the walking distance (None when it cannot be reached).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import knowledge as K
from .navigation import DistanceField

if TYPE_CHECKING:
    from .session import DoomSession

AMMO_MAX = {"bullets": 200, "shells": 50, "rockets": 50, "cells": 300}
DIRECTIONS = [("front", 0), ("front_left", 45), ("left", 90), ("back_left", 135),
              ("back", 180), ("back_right", -135), ("right", -90), ("front_right", -45)]


def norm_angle(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def bearing_to(px: float, py: float, angle: float, tx: float, ty: float) -> float:
    return norm_angle(math.degrees(math.atan2(ty - py, tx - px)) - angle)


def aim_tolerance(distance: float) -> float:
    """Half-angle (deg) within which a hitscan shot hits a ~20-unit-radius monster."""
    return max(1.5, math.degrees(math.atan2(14.0, max(distance, 1.0))))


def weapons_owned(v: dict[str, float]) -> list[dict]:
    out = []
    for slot, w in K.WEAPON_SLOTS.items():
        if v.get(f"WEAPON{slot}", 0) > 0:  # some scenarios take even the fist away
            ammo = None if w.ammo is None else int(v.get(f"AMMO{slot}", 0))
            out.append({"slot": slot, "name": w.name, "ammo": ammo,
                        "usable": w.ammo is None or (ammo or 0) > 0})
    return out


def player_status(session: "DoomSession") -> dict:
    snap, tr = session.snapshot, session.tracker
    assert snap is not None and tr is not None
    v = snap.vars
    slot = int(v["SELECTED_WEAPON"])
    weapon = K.WEAPON_SLOTS.get(slot)
    return {
        "x": round(snap.x, 1), "y": round(snap.y, 1), "angle": round(snap.angle, 1),
        "health": int(v["HEALTH"]), "armor": int(v["ARMOR"]),
        "weapon": weapon.name if weapon else f"slot{slot}", "weapon_slot": slot,
        "ammo": None if weapon is None or weapon.ammo is None else int(v["SELECTED_WEAPON_AMMO"]),
        "weapons": weapons_owned(v),
        "ammo_by_type": {"bullets": int(v["AMMO2"]), "shells": int(v["AMMO3"]),
                         "rockets": int(v["AMMO5"]), "cells": int(v["AMMO6"])},
        "keys": sorted(tr.keys),
        "kills": int(v["KILLCOUNT"]), "items": int(v["ITEMCOUNT"]), "secrets": int(v["SECRETCOUNT"]),
        "damage_dealt": int(v["DAMAGECOUNT"]), "damage_taken": int(v["DAMAGE_TAKEN"]),
        "attack_ready": bool(v["ATTACK_READY"]), "dead": bool(v["DEAD"]) or tr.end_reason == "died",
        "on_damaging_floor": bool(session.nav and session.nav.is_damaging(session.nav.sector_at(snap.x, snap.y))),
    }


def item_useful(name: str, kind: str, v: dict[str, float]) -> bool:
    """Is this item worth a detour right now? (+1 bonuses never are; they get
    collected anyway when walking over them.)"""
    health, armor = v["HEALTH"], v["ARMOR"]
    if name in ("HealthBonus", "ArmorBonus"):
        return False
    if name in ("Stimpack", "Medikit"):
        return health < 100
    if name == "GreenArmor":
        return armor < 100
    if name == "BlueArmor":
        return armor < 200
    if kind == "ammo":
        maxes = {"Clip": "bullets", "ClipBox": "bullets", "Shell": "shells", "ShellBox": "shells",
                 "RocketAmmo": "rockets", "RocketBox": "rockets", "Cell": "cells", "CellPack": "cells"}
        ammo_type = maxes.get(name)
        if ammo_type is None:
            return True
        current = {"bullets": v["AMMO2"], "shells": v["AMMO3"], "rockets": v["AMMO5"], "cells": v["AMMO6"]}
        return current[ammo_type] < AMMO_MAX[ammo_type] / 2
    return True


def observe(session: "DoomSession", with_paths: bool = True) -> dict:
    """Build the full observation dictionary for the current tic."""
    snap, nav, tr, sc = session.snapshot, session.nav, session.tracker, session.scenario
    assert snap is not None and nav is not None and tr is not None and sc is not None
    v = snap.vars
    px, py, ang = snap.x, snap.y, snap.angle
    pz = v["POSITION_Z"]
    field: DistanceField | None = nav.distance_field(px, py) if with_paths and not tr.finished else None

    def path_dist(x: float, y: float, radius_cells: int = 1) -> int | None:
        if field is None:
            return None
        d = field.at(x, y, radius_cells)
        return None if d is None else int(round(d))

    def locate(x: float, y: float) -> dict:
        return {"distance": int(round(math.hypot(x - px, y - py))),
                "bearing": round(bearing_to(px, py, ang, x, y), 1)}

    enemies, items, hazards, projectiles = [], [], [], []
    seen_ids: set[int] = set()
    monsters_xy = [(lab.object_position_x, lab.object_position_y) for lab in snap.labels
                   if lab.object_category == "Monster"]
    for lab in snap.labels:
        if lab.object_id in seen_ids:
            continue
        seen_ids.add(lab.object_id)
        cat, name = lab.object_category, lab.object_name
        ox, oy = lab.object_position_x, lab.object_position_y
        loc = locate(ox, oy)
        if cat == "Monster":
            info = K.monster_info(name)
            enemies.append({"id": lab.object_id, "name": name, **loc,
                            "aimed": abs(loc["bearing"]) <= aim_tolerance(loc["distance"]),
                            "threat": info.threat, "attack": info.attack, "note": info.note,
                            "height": int(round(lab.object_position_z - pz)),
                            "screen_width": lab.width})
        elif cat in K.ITEM_CATEGORIES:
            info = K.item_info(name, cat)
            items.append({"id": lab.object_id, "name": name, "kind": info.kind, "label": info.label,
                          **loc, "value": info.value, "useful": item_useful(name, info.kind, v),
                          "path_distance": path_dist(ox, oy)})
        elif cat == "Hazard":
            near = sum(1 for mx, my in monsters_xy if math.hypot(mx - ox, my - oy) < 128)
            hazards.append({"id": lab.object_id, "name": name, **loc, "enemies_nearby": near})
        elif cat == "Explosive" and name not in K.HARMLESS_EFFECTS:
            rx, ry = px - ox, py - oy
            vx, vy = lab.object_velocity_x, lab.object_velocity_y
            speed = math.hypot(vx, vy)
            incoming = False
            if speed > 0.1 and (vx * rx + vy * ry) > 0:
                miss = abs(vx * ry - vy * rx) / speed  # distance of closest approach
                incoming = miss < 40.0
            projectiles.append({"id": lab.object_id, "name": name, **loc, "incoming": incoming})
    enemies.sort(key=lambda e: e["distance"])
    items.sort(key=lambda i: (i["path_distance"] is None, i["path_distance"] or i["distance"]))
    projectiles.sort(key=lambda p: p["distance"])

    known_items = []
    visible_ids = {i["id"] for i in items}
    for oid, it in tr.seen_items.items():
        if oid in visible_ids or oid not in tr.items:
            continue
        info = K.item_info(it["name"])
        known_items.append({"id": oid, "name": it["name"], "kind": info.kind, "label": info.label,
                            **locate(it["x"], it["y"]), "value": info.value,
                            "useful": item_useful(it["name"], info.kind, v),
                            "path_distance": path_dist(it["x"], it["y"])})
    known_items = sorted([k for k in known_items if k["path_distance"] is not None],
                         key=lambda k: k["path_distance"])[:6]

    heard = []
    if session.settings.hearing_range > 0:
        visible_monsters = {e["id"] for e in enemies}
        for oid, (name, ox, oy) in tr.alive.items():
            if oid in visible_monsters:
                continue
            d = math.hypot(ox - px, oy - py)
            if d <= session.settings.hearing_range:
                heard.append({"id": oid, "name": name, **locate(ox, oy)})
        heard.sort(key=lambda h: h["distance"])

    walls = {}
    for label, rel in DIRECTIONS:
        d, what = nav.raycast(px, py, ang + rel, max_dist=1024.0)
        walls[label] = {"distance": int(round(d)), "blocked_by": what}

    full = session.settings.knowledge == "full"
    features = {"exit": None, "doors": [], "switches": []}
    exits = [f for f in nav.features_of("exit") if (f.seen or full) and not f.secret]
    exits = exits or [f for f in nav.features_of("exit") if f.seen or full]
    if exits:
        scored = [(path_dist(*f.approach, 2), f) for f in exits]
        scored.sort(key=lambda t: (t[0] is None, t[0] or 0))
        d, f = scored[0]
        features["exit"] = {**locate(f.x, f.y), "path_distance": d, "type": f.exit_type,
                            "secret": f.secret, "x": round(f.x), "y": round(f.y)}
    for f in nav.features_of("door"):
        if not (f.seen or full) or f.sector is None or not nav.is_closed_door(f.sector):
            continue
        loc = locate(f.x, f.y)
        if loc["distance"] > 1024:
            continue
        features["doors"].append({**loc, "key": f.key,
                                  "locked": bool(f.key) and f.key not in tr.keys,
                                  "x": round(f.x), "y": round(f.y)})
    features["doors"] = _dedupe(sorted(features["doors"], key=lambda d: d["distance"]))[:4]
    for f in nav.features_of("switch"):
        if (f.seen or full) and not f.used:
            d = path_dist(*f.approach, 2)
            if d is not None:
                features["switches"].append({**locate(f.x, f.y), "path_distance": d, "key": f.key,
                                             "x": round(f.x), "y": round(f.y)})
    features["switches"] = _dedupe(sorted(features["switches"], key=lambda s: s["path_distance"]))[:3]

    recent = [e.to_dict() for e in tr.events if e.tic >= snap.tic - 3 * K.TICRATE]
    return {
        "episode": {
            "id": tr.id, "scenario": sc.name, "title": sc.title, "map": session.map,
            "skill": session.skill, "tic": snap.tic, "time": round(snap.tic / K.TICRATE, 1),
            "finished": tr.finished, "end_reason": tr.end_reason, "goal": sc.goal,
            "campaign": sc.campaign, "commands": list(sc.commands), "tips": list(sc.tips),
            "explored_percent": round(nav.explored_fraction() * 100.0, 1),
        },
        "player": player_status(session),
        "enemies": enemies,
        "items": items,
        "known_items": known_items,
        "hazards": hazards,
        "projectiles": projectiles,
        "heard": heard,
        "walls": walls,
        "exit": features["exit"],
        "doors": features["doors"],
        "switches": features["switches"],
        "recent_events": recent[-12:],
    }


def _dedupe(entries: list[dict], radius: float = 96.0) -> list[dict]:
    """Doors/switches are often several short lines; keep one per spot."""
    out: list[dict] = []
    for e in entries:
        if all(math.hypot(e["x"] - o["x"], e["y"] - o["y"]) > radius for o in out):
            out.append(e)
    return out
