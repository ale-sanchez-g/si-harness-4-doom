"""Command line interface: ``python -m doom_harness <play|check|prompt|bench>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .actions import TurnView
from .agent import Agent
from .evals import run_evals
from .client import DoomAPIError, DoomClient
from .config import PRESETS, HarnessConfig, preset
from .llm import LLMError, OllamaLLM
from .memory import Memory
from .prompts import load_playbook, situation_report, system_prompt
from .recorder import RunRecorder
from .report import find_runs, markdown_table, summarize_run
from .telemetry import TracedLLM, Tracer


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--doom-url", help="Doom API server (env DOOM_URL)")
    p.add_argument("--ollama-host", help="Ollama server (env OLLAMA_HOST)")
    p.add_argument("--preset", choices=list(PRESETS), help="model size preset: xs, s or m (env HARNESS_PRESET)")
    p.add_argument("--model", help="Ollama model, e.g. granite4.2:3b (env OLLAMA_MODEL; overrides the preset)")
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
    if getattr(args, "preset", None):
        cfg.apply_preset(args.preset)
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


def _tracer(cfg: HarnessConfig, recorder: RunRecorder, llm: bool = True) -> Tracer:
    tracer = Tracer(recorder.dir if cfg.trace else None, enabled=cfg.trace,
                    resource={"harness.model": cfg.model if llm else None, "harness.playbook": cfg.playbook,
                              "harness.preset": cfg.preset, "harness.run": recorder.dir.name})
    if cfg.trace:
        print(f"[harness] traces: {recorder.dir / 'traces.jsonl'}"
              + (" (+ OpenTelemetry export)" if tracer.otel_enabled else ""))
    return tracer


def _play(cfg: HarnessConfig, label: str | None = None) -> dict:
    """Play cfg.episodes episodes and return the run summary (with its directory)."""
    client = DoomClient(cfg.doom_url)
    print(f"[harness] waiting for the Doom server at {cfg.doom_url} ...")
    client.wait_ready(timeout=180)
    llm = None
    if cfg.policy == "llm":
        llm = _llm(cfg)
        print(f"[harness] checking model {cfg.model} at {cfg.ollama_host} ...")
        llm.ensure_model(pull=cfg.auto_pull)
        llm.warm_up()
    recorder = RunRecorder(cfg.runs_dir, label or f"{cfg.scenario or 'default'}_{cfg.model if llm else 'scripted'}")
    print(f"[harness] logging this run to {recorder.dir}")
    tracer = _tracer(cfg, recorder, llm is not None)
    agent = Agent(cfg, client, TracedLLM(llm, tracer) if llm else None, recorder, tracer=tracer)
    try:
        agent.run()
    except KeyboardInterrupt:
        print("\n[harness] interrupted")
    finally:
        tracer.close()
    summary = recorder.finish({"model": cfg.model if llm else None, "policy": cfg.policy,
                               "scenario": cfg.scenario, "playbook": cfg.playbook if llm else None,
                               "preset": cfg.preset if llm else None})
    return {**summary, "run_dir": str(recorder.dir)}


def cmd_play(args: argparse.Namespace) -> int:
    summary = _play(_config(args))
    print("\n[harness] run summary:\n" + json.dumps(summary, indent=2))
    print(f"[harness] details: python -m doom_harness report {summary['run_dir']}")
    return 0 if summary["episodes"] else 1


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


def _eval(cfg: HarnessConfig, repeat: int) -> dict:
    llm = _llm(cfg)
    llm.ensure_model(pull=cfg.auto_pull)
    playbook = load_playbook(cfg.playbook_path())
    recorder = RunRecorder(cfg.runs_dir, f"eval_{cfg.model}_{Path(cfg.playbook).stem}")
    print(f"[harness] evaluating {cfg.model} with playbook '{cfg.playbook}' (logging to {recorder.dir})")
    tracer = _tracer(cfg, recorder)
    try:
        with tracer.span("harness.eval", model=cfg.model, playbook=cfg.playbook, repeat=repeat) as span:
            summary = run_evals(TracedLLM(llm, tracer), playbook, cfg.reasoning, repeat=repeat, tracer=tracer)
            span.set(passed=summary["passed"], total=summary["total"])
    finally:
        tracer.close()
    summary = recorder.finish_eval({**summary, "playbook": cfg.playbook, "preset": cfg.preset})
    return {**summary, "run_dir": str(recorder.dir)}


def cmd_eval(args: argparse.Namespace) -> int:
    """Score the model/playbook on fixed situations (needs Ollama, not the game)."""
    summary = _eval(_config(args), args.repeat)
    return 0 if summary["passed"] == summary["total"] else 1


def cmd_report(args: argparse.Namespace) -> int:
    """Outcome, LLM calls, tokens and latency of recorded runs, side by side."""
    cfg = HarnessConfig()
    runs = [Path(r) for r in args.runs] or find_runs(cfg.runs_dir, last=args.last)
    if not runs:
        print(f"[harness] no runs found in {cfg.runs_dir}")
        return 1
    print(markdown_table([summarize_run(r) for r in runs]))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Run the same test with several models (presets xs/s/m by default) and compare."""
    if args.models:
        variants = [("model", m.strip()) for m in args.models.split(",") if m.strip()]
    else:
        variants = [("preset", p.strip()) for p in args.presets.split(",") if p.strip()]
        for _, name in variants:
            preset(name)  # fail fast on a typo, before an hour of games
    rows = []
    for kind, name in variants:
        cfg = _config(args)
        if kind == "preset":
            cfg.apply_preset(name)
            if args.playbook:
                cfg.playbook = args.playbook
        else:
            cfg.model = name
        cfg.policy = "llm"
        if cfg.seed is None:
            cfg.seed = 1000  # same maps and monsters for every model
        print(f"\n[harness] ===== {kind} {name}: {cfg.model}, playbook '{cfg.playbook}' =====")
        result = _eval(cfg, args.repeat) if args.eval else _play(cfg)
        rows.append(summarize_run(result["run_dir"]))
    table = markdown_table(rows)
    out = Path(HarnessConfig().runs_dir) / f"compare_{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# Model comparison ({'eval' if args.eval else 'games'})\n\n{table}\n", encoding="utf-8")
    print("\n" + table + f"\n\n[harness] saved to {out}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    """Download models ahead of time (all presets by default)."""
    names = [n.strip() for n in args.presets.split(",") if n.strip()]
    models = [m.strip() for m in args.models.split(",")] if args.models else [preset(n)["model"] for n in names]
    for model in models:
        cfg = _config(args)
        cfg.model = model
        _llm(cfg).ensure_model(pull=True)
        print(f"[harness] {model} is ready")
    return 0


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
            tracer = Tracer(recorder.dir if cfg.trace else None, enabled=cfg.trace)
            agent = Agent(cfg, client, TracedLLM(llm, tracer) if llm else None, recorder,
                          out=(print if args.verbose else (lambda *_: None)), tracer=tracer)
            try:
                agent.run()
            finally:
                tracer.close()
            summary = recorder.finish({"model": cfg.model if llm else None, "policy": policy, "scenario": scenario,
                                       "playbook": cfg.playbook if llm else None})
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

    p = sub.add_parser("report", help="outcome, LLM calls, tokens and latency of recorded runs")
    p.add_argument("runs", nargs="*", help="run directories (default: the latest run)")
    p.add_argument("--last", type=int, default=1, help="with no directories: the last N runs")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("compare", help="run the same games (or evals) with several models and compare them")
    _add_common(p)
    p.add_argument("--presets", default="xs,s,m", help="model size presets to compare (default xs,s,m)")
    p.add_argument("--models", help="comma-separated Ollama models instead of presets")
    p.add_argument("--eval", action="store_true", help="compare on the fixed eval situations (no game needed)")
    p.add_argument("--repeat", type=int, default=2, help="with --eval: ask each situation N times")
    p.add_argument("--episodes", type=int)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--timeout", type=float)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("pull", help="download the models of the presets (or --models) into Ollama")
    _add_common(p)
    p.add_argument("--presets", default=",".join(PRESETS))
    p.add_argument("--models", help="comma-separated Ollama models instead of presets")
    p.set_defaults(func=cmd_pull)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(["play", *(argv or sys.argv[1:])])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (DoomAPIError, LLMError, FileNotFoundError, ValueError) as exc:
        print(f"[harness] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
