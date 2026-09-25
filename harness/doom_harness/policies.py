"""Decision makers: the LLM policy and a scripted baseline that follows the same playbook."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .actions import REASON_KEY, Resolved, TurnView
from .llm import LLM, LLMError, LLMReply
from .memory import Memory
from .prompts import situation_report

log = logging.getLogger(__name__)


@dataclass
class Decision:
    resolved: Resolved
    thought: str = ""
    source: str = "llm"  # llm | scripted | fallback
    prompt: str = ""
    raw: str = ""
    latency: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_second: float = 0.0
    errors: list[str] = field(default_factory=list)


class ScriptedPolicy:
    """Hand-written rules mirroring the default playbook. Useful as a baseline
    ("does the LLM beat 20 lines of if-statements?") and as the fallback when the
    model is unreachable or answers nonsense."""

    name = "scripted"

    def decide(self, view: TurnView, memory: Memory, turn: int = 0, **_) -> Decision:
        action, arg, why = self.choose(view, memory)
        resolved = view.resolve({"action": action, "arg": arg})
        if resolved is None:  # should not happen, but never crash the game loop
            resolved = view.resolve({"action": view.action_names[0], "arg": "none"})
        assert resolved is not None
        return Decision(resolved=resolved, thought=why, source="scripted")

    def choose(self, view: TurnView, memory: Memory) -> tuple[str, str, str]:
        obs = view.obs
        names = set(view.action_names)
        health = obs["player"]["health"]
        incoming = any(p.get("incoming") for p in obs.get("projectiles", []))
        health_items = [t for t in view.items if t.info.get("kind") == "health"]
        if incoming and "dodge" in names:
            return "dodge", "left" if obs["walls"]["left"]["distance"] >= obs["walls"]["right"]["distance"] \
                else "right", "A projectile is coming at me."
        if health <= 30 and health_items and "pickup" in names:
            return "pickup", health_items[0].tag, "Low health, grabbing health."
        if health <= 30 and view.enemies and "retreat" in names and view.enemies[0].info["distance"] < 250:
            return "retreat", "none", "Low health and an enemy is close."
        if view.enemies and "attack" in names:
            target = view.enemies[0]
            for t in view.enemies[1:]:
                if t.info.get("threat", 0) > target.info.get("threat", 0) + 1 and \
                        t.info["distance"] < target.info["distance"] * 1.5:
                    target = t
            return "attack", target.tag, f"Shooting the {target.info['name']}."
        if view.items and "pickup" in names:
            return "pickup", view.items[0].tag, f"Collecting the {view.items[0].info['label']}."
        if "goto_exit" in names:
            return "goto_exit", "none", "Area clear, heading for the exit."
        last = memory.records[-1] if memory.records else None
        if last and last.damage_taken > 0 and "turn" in names:
            return "turn", "around", "Hurt by something I cannot see."
        if memory.is_stuck() and "turn" in names:
            return "turn", "around", "Stuck, turning around."
        if "explore" in names and not (last and last.action == "explore" and "nothing left" in last.reason):
            return "explore", "none", "Searching for enemies and the exit."
        if "turn" in names:
            return "turn", "left", "Looking around."
        return view.action_names[0], "none", "Nothing better to do."


class LLMPolicy:
    name = "llm"

    def __init__(self, llm: LLM, system_prompt: str, reasoning: bool = True, history: int = 6,
                 attack_seconds: float = 2.0, explore_seconds: float = 5.0, retries: int = 1,
                 reminder: str = ""):
        self.llm = llm
        self.system_prompt = system_prompt
        self.reminder = reminder
        self.reasoning = reasoning
        self.history = history
        self.attack_seconds = attack_seconds
        self.explore_seconds = explore_seconds
        self.retries = retries
        self.fallback = ScriptedPolicy()

    def decide(self, view: TurnView, memory: Memory, turn: int = 0, **_) -> Decision:
        user = situation_report(view, memory, turn, self.history, self.reminder)
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user}]
        schema = view.schema(self.reasoning)
        errors: list[str] = []
        total_latency = 0.0
        reply: LLMReply | None = None
        for attempt in range(self.retries + 1):
            try:
                reply = self.llm.chat(messages, schema)
            except LLMError as exc:
                errors.append(str(exc))
                log.warning("LLM error (attempt %d): %s", attempt + 1, exc)
                continue
            total_latency += reply.latency
            resolved = view.resolve(reply.data, self.attack_seconds, self.explore_seconds)
            if resolved is not None:
                thought = reply.data.get(REASON_KEY) or reply.data.get("thought") or ""
                return Decision(resolved=resolved, thought=str(thought)[:300],
                                source="llm", prompt=user, raw=reply.text, latency=total_latency,
                                prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens,
                                tokens_per_second=reply.tokens_per_second, errors=errors)
            errors.append(f"invalid action {reply.data.get('action')!r}")
            messages = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": f"'{reply.data.get('action')}' is not possible now. "
                                            f"Choose one of: {', '.join(view.action_names)}."}]
        fb = self.fallback.decide(view, memory, turn)
        fb.source, fb.prompt, fb.errors, fb.latency = "fallback", user, errors, total_latency
        if reply is not None:
            fb.raw = reply.text
        return fb
