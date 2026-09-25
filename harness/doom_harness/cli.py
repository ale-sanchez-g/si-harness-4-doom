"""Command line interface: ``python -m doom_harness <play|check|prompt|bench>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .actions import TurnView
from .agent import Agent
from .evals import run_evals
from .client import DoomAPIError, DoomClient
from .config import HarnessConfig
from .llm import LLMError, OllamaLLM
from .memory import Memory
from .prompts import load_playbook, situation_report, system_prompt
from .recorder import RunRecorder


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--doom-url", help="Doom API server (env DOOM_URL)")
    p.add_argument("--ollama-host", help="Ollama server (env OLLAMA_HOST)")
    p.add_argument("--model", help="Ollama model, e.g. granite4.2:3b (env OLLAMA_MODEL)")
    p.add_argument("--scenario", help="freedoom2, freedoom1, basic, defend_the_center, ... (env HARNESS_SCENARIO)")
    p.add_argument("--map", help="map lump, e.g. MAP01 / E1M1 (env HARNESS_MAP)")
    p.add_argument("--skill", type=int, help="1-5 (env HARNESS_SKILL)")
    p.add_argument("--playbook", help="playbook name or path to a .md file (env HARNESS_PLAYBOOK)")
    p.add_argument("--policy", choices=["llm", "scripted"], help="who decides (env HARNESS_POLICY)")
    p.add_argument("--no-reasoning", action="store_true", help="skip the 'thought' field (faster)")
    p.add_argument("--temperature", type=float)
    p.add_argument("-v", "--verbose", action="store_true")


def _config(args: argparse.Namespace) -> HarnessConfig:
    cfg = HarnessConfig()
    mapping = {"doom_url": "doom_url", "ollama_host": "ollama_host", "model": "model", "scenario": "scenario",
               "map": "map", "skill": "skill", "playbook": "playbook", "policy": "policy",
               "temperature": "temperature", "episodes": "episodes", "max_steps": "max_steps",
               "seed": "seed", "timeout": "episode_timeout"}
    for arg, attr in mapping.items():
        value = getattr(args, arg, None)
        if value is not None:
            setattr(cfg, attr, value)
    if getattr(args, "no_reasoning", False):
        cfg.reasoning = False
    if getattr(args, "campaign", False):
        cfg.campaign = True
    return cfg


def _llm(cfg: HarnessConfig) -> OllamaLLM:
    return OllamaLLM(cfg.ollama_host, cfg.model, temperature=cfg.temperature, num_ctx=cfg.num_ctx,
                     num_predict=cfg.num_predict, timeout=cfg.llm_timeout, keep_alive=cfg.keep_alive,
                     think=cfg.think)


def cmd_play(args: argparse.Namespace) -> int:
    cfg = _config(args)
    client = DoomClient(cfg.doom_url)
    print(f"[harness] waiting for the Doom server at {cfg.doom_url} ...")
    client.wait_ready(timeout=180)
    llm = None
    if cfg.policy == "llm":
        llm = _llm(cfg)
        print(f"[harness] checking model {cfg.model} at {cfg.ollama_host} ...")
        llm.ensure_model(pull=cfg.auto_pull)
        llm.warm_up()
    recorder = RunRecorder(cfg.runs_dir, f"{cfg.scenario or 'default'}_{cfg.model if llm else 'scripted'}")
    print(f"[harness] logging this run to {recorder.dir}")
    agent = Agent(cfg, client, llm, recorder)
    try:
        results = agent.run()
    except KeyboardInterrupt:
        print("\n[harness] interrupted")
        results = recorder.episodes
    summary = recorder.finish({"model": cfg.model if llm else None, "policy": cfg.policy,
                               "scenario": cfg.scenario, "playbook": cfg.playbook})
    print("\n[harness] run summary:\n" + json.dumps(summary, indent=2))
    return 0 if results else 1


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ok = True
    client = DoomClient(cfg.doom_url)
    try:
        health = client.wait_ready(timeout=5)
        print(f"[ok]   Doom server {cfg.doom_url}: {health}")
        print("       scenarios:", ", ".join(s["name"] for s in client.scenarios()))
    except DoomAPIError as exc:
        ok = False
        print(f"[FAIL] Doom server: {exc}")
    llm = _llm(cfg)
    try:
        models = llm.list_models()
        print(f"[ok]   Ollama {cfg.ollama_host}: {len(models)} models installed")
        if llm.has_model():
            print(f"[ok]   model {cfg.model} is installed")
        else:
            print(f"[warn] model {cfg.model} is not installed yet (it is pulled automatically on 'play')")
    except LLMError as exc:
        ok = False
        print(f"[FAIL] Ollama: {exc}")
    if ok and llm.has_model():
        try:
            obs = client.observation()
        except DoomAPIError:
            obs = client.new_episode(scenario=cfg.scenario)
        playbook = load_playbook(cfg.playbook_path())
        view = TurnView(obs, allowed=playbook.actions)
        prompt = system_prompt(playbook, obs, cfg.reasoning)
        reply = llm.chat([{"role": "system", "content": prompt},
                          {"role": "user", "content": situation_report(view, Memory(), 1, reminder=playbook.reminder,
                                                                       facts=playbook.facts)}],
                         view.schema(cfg.reasoning))
        print(f"[ok]   test decision in {reply.latency:.1f}s ({reply.tokens_per_second:.0f} tok/s): {reply.data}")
    return 0 if ok else 1


def cmd_prompt(args: argparse.Namespace) -> int:
    """Print exactly what the model would see right now (great for tuning the playbook)."""
    cfg = _config(args)
    client = DoomClient(cfg.doom_url)
    obs = client.new_episode(scenario=cfg.scenario, map=cfg.map) if args.new else client.observation()
    playbook = load_playbook(cfg.playbook_path())
    view = TurnView(obs, allowed=playbook.actions)
    print("=" * 30, "SYSTEM PROMPT", "=" * 30)
    print(system_prompt(playbook, obs, cfg.reasoning))
    print("=" * 30, "SITUATION REPORT", "=" * 30)
    print(situation_report(view, Memory(), 1, reminder=playbook.reminder, facts=playbook.facts))
    print("=" * 30, "JSON SCHEMA", "=" * 30)
    print(json.dumps(view.schema(cfg.reasoning), indent=2))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Score the model/playbook on fixed situations (needs Ollama, not the game)."""
    cfg = _config(args)
    llm = _llm(cfg)
    llm.ensure_model(pull=cfg.auto_pull)
    playbook = load_playbook(cfg.playbook_path())
    print(f"[harness] evaluating {cfg.model} with playbook '{cfg.playbook}'")
    summary = run_evals(llm, playbook, cfg.reasoning, repeat=args.repeat)
    return 0 if summary["passed"] == summary["total"] else 1


def cmd_bench(args: argparse.Namespace) -> int:
    base = _config(args)
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    policies = ["llm", "scripted"] if args.compare else [args.policy or base.policy]
    client = DoomClient(base.doom_url)
    client.wait_ready(timeout=180)
    rows = []
    for policy in policies:
        llm = None
        if policy == "llm":
            llm = _llm(base)
            llm.ensure_model(pull=base.auto_pull)
        for scenario in scenarios:
            cfg = _config(args)
            cfg.policy, cfg.scenario, cfg.episodes = policy, scenario, args.episodes
            cfg.seed = args.seed if args.seed is not None else 1000
            recorder = RunRecorder(cfg.runs_dir, f"bench_{scenario}_{cfg.model if llm else 'scripted'}")
            agent = Agent(cfg, client, llm, recorder, out=(print if args.verbose else (lambda *_: None)))
            agent.run()
            summary = recorder.finish({"model": cfg.model if llm else None, "policy": policy, "scenario": scenario})
            rows.append(summary)
            print(f"{policy:>8} | {scenario:<24} | success {summary['successes']}/{summary['episodes']} "
                  f"| deaths {summary['deaths']} | avg kills {summary['avg_kills']:>5} "
                  f"| avg turns {summary['avg_turns']:>5} | llm {summary['avg_llm_latency']}s "
                  f"| fallback {summary['fallback_rate']:.0%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doom_harness", description="LLM harness that plays Doom")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("play", help="play episodes")
    _add_common(p)
    p.add_argument("--episodes", type=int)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--timeout", type=float, help="game-time limit per episode (seconds)")
    p.add_argument("--campaign", action="store_true", help="continue to the next map after an exit")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("check", help="check the Doom server, Ollama and the model")
    _add_common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("prompt", help="print the prompt the model would see now")
    _add_common(p)
    p.add_argument("--new", action="store_true", help="start a fresh episode first")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("eval", help="score the model + playbook on fixed situations (no game needed)")
    _add_common(p)
    p.add_argument("--repeat", type=int, default=1, help="ask each situation N times")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("bench", help="compare policies/models over several scenarios")
    _add_common(p)
    p.add_argument("--scenarios", default="basic,defend_the_center,deadly_corridor,health_gathering,freedoom2")
    p.add_argument("--compare", action="store_true", help="run both the LLM and the scripted baseline")
    p.add_argument("--episodes", type=int, default=3)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--timeout", type=float)
    p.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(["play", *(argv or sys.argv[1:])])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (DoomAPIError, LLMError, FileNotFoundError) as exc:
        print(f"[harness] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
