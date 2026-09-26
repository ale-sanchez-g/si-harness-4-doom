"""The LLM's action space.

A small model cannot aim or steer. It *can* choose between a handful of clearly
named intentions, so the harness exposes high-level actions ("attack E1",
"pickup I2", "explore"...) and turns each into a server command. Every turn we
also build a JSON schema that only allows the actions/targets that make sense
right now, so constrained decoding makes invalid answers impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Ollama emits structured-output keys in alphabetical order, so the reasoning key
# must sort before "action" for the model to think *before* it picks an action.
# A capitalised "Thought" does that ("T" < "a") and stays first either way.
REASON_KEY = "Thought"
# Upper bound on the reasoning text (Ollama enforces maxLength while decoding), so a
# rambling small model can never run out of tokens before it writes the action.
THOUGHT_MAX_CHARS = 300

TURN_DIRS = ["left", "right", "around"]
MOVE_DIRS = ["forward", "backward", "left", "right"]
DODGE_DIRS = ["left", "right"]

SYNONYMS = {
    "shoot": "attack", "fire": "attack", "kill": "attack", "fight": "attack",
    "collect": "pickup", "grab": "pickup", "get": "pickup", "take": "pickup",
    "search": "explore", "walk": "explore", "go": "explore", "wander": "explore",
    "exit": "goto_exit", "leave": "goto_exit", "finish": "goto_exit",
    "open": "use", "activate": "use", "press": "use",
    "back": "retreat", "flee": "retreat", "run": "retreat",
    "strafe": "dodge", "sidestep": "dodge", "evade": "dodge",
    "look": "turn", "rotate": "turn", "step": "move",
    "weapon": "switch_weapon", "select_weapon": "switch_weapon", "change_weapon": "switch_weapon",
    "idle": "wait", "none": "wait", "noop": "wait",
}


@dataclass(frozen=True)
class Action:
    name: str
    usage: str
    description: str
    arg: str  # enemy | item | turn | move | dodge | weapon | none
    server_command: str
    # Affordance: the action is only offered when it can make sense this turn.
    needs: str | None = None  # enemy | threat | item | exit | weapon


ACTIONS: dict[str, Action] = {a.name: a for a in [
    Action("attack", "attack E#", "aim at enemy E# and shoot until it dies", "enemy", "attack", "enemy"),
    Action("pickup", "pickup I#", "walk to item I# and collect it", "item", "goto", "item"),
    Action("explore", "explore", "walk into unexplored areas; opens doors on the way", "none", "explore"),
    Action("goto_exit", "goto_exit", "walk to the level exit and finish the level", "none", "goto_exit", "exit"),
    Action("retreat", "retreat", "back away from enemies while facing them", "none", "retreat", "threat"),
    Action("dodge", "dodge left|right", "quick sidestep to avoid fireballs and bullets", "dodge", "dodge", "threat"),
    Action("turn", "turn left|right|around", "turn to look for enemies", "turn", "turn"),
    Action("move", "move forward|backward|left|right", "take a few steps", "move", "move"),
    Action("use", "use", "open the door or press the switch in front of you", "none", "use"),
    Action("switch_weapon", "switch_weapon <name>", "change to another weapon you own", "weapon",
           "select_weapon", "weapon"),
    Action("wait", "wait", "do nothing for a moment", "none", "wait"),
]}


@dataclass
class Target:
    tag: str
    id: int
    info: dict
    remembered: bool = False


@dataclass
class Resolved:
    action: str
    arg: str
    command: dict
    notes: list[str] = field(default_factory=list)


class TurnView:
    """Everything the policy may choose from on this turn."""

    def __init__(self, obs: dict, banned_ids: set[int] | None = None,
                 max_enemies: int = 5, max_items: int = 5, allowed: list[str] | None = None,
                 blocked: set[str] | None = None):
        self.obs = obs
        banned = banned_ids or set()
        enemies = [Target(f"E{i + 1}", e["id"], e) for i, e in enumerate(obs.get("enemies", [])[:max_enemies])]
        # Explosive barrels next to monsters are worth shooting too.
        barrels = [h for h in obs.get("hazards", []) if h.get("enemies_nearby", 0) > 0 and h["distance"] > 160]
        enemies += [Target(f"B{i + 1}", h["id"], {**h, "note": f"explosive barrel next to "
                                                               f"{h['enemies_nearby']} enemies"})
                    for i, h in enumerate(barrels[:2])]
        self.enemies = enemies

        items = [dict(i, remembered=False) for i in obs.get("items", [])
                 if i.get("useful") and i.get("path_distance") is not None and i["id"] not in banned]
        items += [dict(k, remembered=True) for k in obs.get("known_items", [])
                  if k.get("useful") and k.get("path_distance") is not None and k["id"] not in banned]
        items.sort(key=lambda i: i["path_distance"])
        self.items = [Target(f"I{n + 1}", i["id"], i, i["remembered"]) for n, i in enumerate(items[:max_items])]

        player = obs.get("player", {})
        self.weapons = [w["name"] for w in player.get("weapons", []) if w.get("usable")]
        commands = set(obs.get("episode", {}).get("commands", [a.server_command for a in ACTIONS.values()]))
        self.scenario_actions = [a for a in ACTIONS.values() if a.server_command in commands]
        if allowed_by_playbook := [a for a in self.scenario_actions if allowed is None or a.name in allowed]:
            self.scenario_actions = allowed_by_playbook  # the playbook's menu, if it leaves anything
        self.actions = [a for a in self.scenario_actions if self._available(a)]
        if not self.actions:  # never leave the model without a valid choice
            self.actions = [a for a in ACTIONS.values() if a.server_command in commands and self._available(a)]
        # Loop breaker: an action repeated without effect is off the menu for one turn.
        if blocked and (rest := [a for a in self.actions if a.name not in blocked]):
            self.actions = rest

    # ------------------------------------------------------------ availability
    def _available(self, action: Action) -> bool:
        if action.needs == "enemy":
            return bool(self.enemies) and bool(self.weapons)  # something must be able to fire
        if action.needs == "threat":  # nothing to flee from -> retreat/dodge would just waste the turn
            return bool(self.obs.get("enemies")) or any(p.get("incoming") for p in self.obs.get("projectiles", []))
        if action.needs == "item":
            return bool(self.items)
        if action.needs == "exit":
            ex = self.obs.get("exit")
            return bool(ex) and ex.get("path_distance") is not None
        if action.needs == "weapon":
            return len([w for w in self.weapons if w != "fist"]) > 1
        return True

    @property
    def action_names(self) -> list[str]:
        return [a.name for a in self.actions]

    def target(self, tag: str) -> Target | None:
        for t in self.enemies + self.items:
            if t.tag == tag:
                return t
        return None

    def arg_choices(self) -> list[str]:
        choices = ["none"]
        names = set(self.action_names)
        if "attack" in names:
            choices += [t.tag for t in self.enemies]
        if "pickup" in names:
            choices += [t.tag for t in self.items]
        dirs: list[str] = []
        if "turn" in names:
            dirs += TURN_DIRS
        if "move" in names:
            dirs += MOVE_DIRS
        if "dodge" in names:
            dirs += DODGE_DIRS
        choices += list(dict.fromkeys(dirs))
        if "switch_weapon" in names:
            choices += [w for w in self.weapons if w not in choices]
        return choices

    def schema(self, reasoning: bool = True) -> dict:
        props: dict = {}
        if reasoning:
            props[REASON_KEY] = {"type": "string", "maxLength": THOUGHT_MAX_CHARS}
        props["action"] = {"type": "string", "enum": self.action_names}
        props["arg"] = {"type": "string", "enum": self.arg_choices()}
        return {"type": "object", "properties": props, "required": list(props)}

    # --------------------------------------------------------------- resolving
    def resolve(self, decision: dict, attack_seconds: float = 2.0,
                explore_seconds: float = 5.0) -> Resolved | None:
        """Validate/repair a decision and translate it into a server command."""
        notes: list[str] = []
        name = str(decision.get("action", "")).strip().lower().replace(" ", "_")
        arg = str(decision.get("arg", "none") or "none").strip()
        if name not in ACTIONS:
            name = SYNONYMS.get(name, name)
        if name not in self.action_names:
            return None
        a = ACTIONS[name]
        if a.arg == "enemy":
            tags = {t.tag: t for t in self.enemies}
            if arg.upper() not in tags:
                notes.append(f"target '{arg}' invalid, using {self.enemies[0].tag}")
                arg = self.enemies[0].tag
            arg = arg.upper()
            return Resolved(name, arg, {"command": "attack", "target_id": tags[arg].id,
                                        "duration": attack_seconds}, notes)
        if a.arg == "item":
            tags = {t.tag: t for t in self.items}
            if arg.upper() not in tags:
                notes.append(f"item '{arg}' invalid, using {self.items[0].tag}")
                arg = self.items[0].tag
            arg = arg.upper()
            return Resolved(name, arg, {"command": "goto", "target_id": tags[arg].id, "duration": 12.0}, notes)
        if a.arg in ("turn", "move", "dodge"):
            valid = {"turn": TURN_DIRS, "move": MOVE_DIRS, "dodge": DODGE_DIRS}[a.arg]
            arg = arg.lower()
            if arg not in valid:
                default = {"turn": "around", "move": "forward", "dodge": "left"}[a.arg]
                notes.append(f"direction '{arg}' invalid, using {default}")
                arg = default
            cmd = {"command": a.server_command, "direction": arg}
            if name == "move":
                cmd["distance"] = 160.0
            return Resolved(name, arg, cmd, notes)
        if a.arg == "weapon":
            arg = arg.lower().replace(" ", "_")
            if arg not in self.weapons:
                best = next((w for w in ("plasma_rifle", "chaingun", "shotgun", "rocket_launcher", "pistol")
                             if w in self.weapons), self.weapons[0])
                notes.append(f"weapon '{arg}' not available, using {best}")
                arg = best
            return Resolved(name, arg, {"command": "select_weapon", "weapon": arg}, notes)
        cmd = {"command": a.server_command}
        if name == "explore":
            cmd["duration"] = explore_seconds
        elif name == "wait":
            cmd["duration"] = 0.5
        return Resolved(name, "none", cmd, notes)
