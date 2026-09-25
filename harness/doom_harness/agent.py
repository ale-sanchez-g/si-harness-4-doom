"""The agent loop: observe -> decide -> act -> remember, one turn at a time."""

from __future__ import annotations

import logging
import time
from typing import Callable

from .actions import TurnView
from .client import DoomClient
from .config import HarnessConfig
from .llm import LLM
from .memory import Memory, StepRecord
from .policies import Decision, LLMPolicy, ScriptedPolicy
from .prompts import load_playbook, system_prompt
from .recorder import RunRecorder

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, cfg: HarnessConfig, client: DoomClient, llm: LLM | None = None,
                 recorder: RunRecorder | None = None, out: Callable[[str], None] = print):
        self.cfg = cfg
        self.client = client
        self.llm = llm
        self.recorder = recorder
        self.out = out
        self.playbook = load_playbook(cfg.playbook_path()) if cfg.policy == "llm" else None

    def make_policy(self, obs: dict):
        if self.cfg.policy == "scripted" or self.llm is None or self.playbook is None:
            return ScriptedPolicy()
        prompt = system_prompt(self.playbook, obs, self.cfg.reasoning)
        if self.recorder is not None and self.cfg.save_prompts:
            name = f"system_prompt_{obs['episode']['scenario']}.md"
            (self.recorder.dir / name).write_text(prompt, encoding="utf-8")
        return LLMPolicy(self.llm, prompt, reasoning=self.cfg.reasoning, history=self.cfg.history,
                         attack_seconds=self.cfg.attack_seconds, explore_seconds=self.cfg.explore_seconds,
                         reminder=self.playbook.reminder, facts=self.playbook.facts)

    # ------------------------------------------------------------------ run
    def run(self) -> list[dict]:
        results = []
        next_map = self.cfg.map
        for i in range(self.cfg.episodes):
            summary = self.play_episode(i + 1, next_map)
            results.append(summary)
            if self.recorder is not None:
                self.recorder.episode(summary)
            next_map = "next" if (self.cfg.campaign and summary["end_reason"] == "exit") else self.cfg.map
        return results

    def play_episode(self, index: int, map_name: str | None = None) -> dict:
        cfg = self.cfg
        obs = self.client.new_episode(scenario=cfg.scenario, map=map_name, skill=cfg.skill,
                                      seed=None if cfg.seed is None else cfg.seed + index - 1,
                                      timeout=cfg.episode_timeout or None)
        ep = obs["episode"]
        policy = self.make_policy(obs)
        memory = Memory()
        self.out(f"\n=== Episode {index}: {ep['scenario']} {ep['map']} (skill {ep['skill']}) "
                 f"policy={policy.name}{' model=' + self.llm.model if policy.name == 'llm' and self.llm else ''} ===")
        self.out(f"Goal: {ep['goal']}")
        latencies: list[float] = []
        fallbacks = 0
        turn = 0
        started = time.monotonic()
        result: dict = {"status": "", "reason": ""}
        for turn in range(1, cfg.max_steps + 1):
            if obs["episode"]["finished"]:
                turn -= 1
                break
            view = TurnView(obs, banned_ids=memory.banned_ids(turn),
                            allowed=self.playbook.actions if self.playbook else None,
                            blocked=memory.looping_actions())
            decision: Decision = policy.decide(view, memory, turn)
            if decision.source == "llm":
                latencies.append(decision.latency)
            elif decision.source == "fallback":
                fallbacks += 1
            cmd = decision.resolved.command
            t0 = time.monotonic()
            result = self.client.command(**cmd)
            exec_time = time.monotonic() - t0
            changes = result.get("changes", {})
            rec = StepRecord(
                turn=turn, action=decision.resolved.action, arg=decision.resolved.arg,
                status=result["status"], reason=result["reason"], thought=decision.thought,
                events=[e["text"] for e in result.get("events", []) if e["type"] in ("pickup", "kill", "key", "secret")],
                moved=changes.get("moved", 0), damage_taken=changes.get("damage_taken", 0),
                damage_dealt=changes.get("damage_dealt", 0),
                kills=changes.get("kills", 0), target_id=cmd.get("target_id"))
            memory.add(rec)
            memory.last_events = result.get("events", [])
            obs = result["observation"]
            self._report(rec, decision, obs)
            self._record(index, rec, decision, cmd, result, exec_time)
            if result["status"] == "episode_over":
                break
        p = obs["player"]
        summary = {
            "episode": index, "scenario": obs["episode"]["scenario"], "map": obs["episode"]["map"],
            "end_reason": obs["episode"]["end_reason"] or ("max_turns" if turn >= cfg.max_steps else "stopped"),
            "turns": turn, "game_time": obs["episode"]["time"], "wall_time": round(time.monotonic() - started, 1),
            "health": p["health"], "kills": p["kills"], "items": p["items"], "secrets": p["secrets"],
            "damage_dealt": p["damage_dealt"], "damage_taken": p["damage_taken"],
            "explored_percent": obs["episode"]["explored_percent"],
            "policy": policy.name, "model": self.llm.model if (self.llm and policy.name == "llm") else None,
            "avg_llm_latency": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "fallbacks": fallbacks,
        }
        self.out(f"=== Episode {index} over: {summary['end_reason']} after {turn} turns, "
                 f"{summary['kills']} kills, health {summary['health']}, "
                 f"explored {summary['explored_percent']}% ===")
        return summary

    # -------------------------------------------------------------- output
    def _report(self, rec: StepRecord, d: Decision, obs: dict) -> None:
        p = obs["player"]
        what = rec.action if rec.arg == "none" else f"{rec.action} {rec.arg}"
        src = d.source if d.source != "llm" else f"llm {d.latency:4.1f}s"
        self.out(f"T{rec.turn:03d} | {src:>9} | {what:<18} | {rec.status}: {rec.reason} "
                 f"| hp {p['health']} ar {p['armor']} kills {p['kills']}")
        if d.thought:
            self.out(f"       thought: {d.thought}")
        for note in d.resolved.notes + d.errors:
            self.out(f"       note: {note}")

    def _record(self, episode: int, rec: StepRecord, d: Decision, cmd: dict, result: dict,
                exec_time: float) -> None:
        obs = result["observation"]
        if self.recorder is not None:
            self.recorder.step({
                "episode": episode, "turn": rec.turn, "source": d.source, "thought": d.thought,
                "action": rec.action, "arg": rec.arg, "command": cmd, "status": rec.status,
                "reason": rec.reason, "events": result.get("events", []), "changes": result.get("changes"),
                "llm_latency": round(d.latency, 3), "exec_time": round(exec_time, 3),
                "prompt_tokens": d.prompt_tokens, "completion_tokens": d.completion_tokens,
                "tokens_per_second": round(d.tokens_per_second, 1), "raw": d.raw, "errors": d.errors,
                "notes": d.resolved.notes, "prompt": d.prompt if self.cfg.save_prompts else None,
                "player": obs["player"], "tic": obs["episode"]["tic"],
            })
        if self.cfg.post_to_viewer:
            self.client.post_agent_log({
                "step": rec.turn, "thought": d.thought, "action": rec.action,
                "args": {"arg": rec.arg} if rec.arg != "none" else {}, "result": rec.reason,
                "status": rec.status, "model": self.llm.model if (self.llm and d.source == "llm") else d.source,
                "latency": round(d.latency, 2) if d.source == "llm" else None,
            })
