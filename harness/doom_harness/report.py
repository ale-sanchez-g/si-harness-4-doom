"""Summaries of recorded runs: outcome, LLM calls, tokens and latency, side by side.

    python -m doom_harness report                 # the latest run
    python -m doom_harness report runs/A runs/B   # compare runs
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .telemetry import read_spans


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def llm_stats(run_dir: Path) -> dict:
    """Per-call LLM numbers from traces.jsonl (or steps.jsonl for runs without traces)."""
    calls: list[dict] = []
    model = None
    traces = run_dir / "traces.jsonl"
    if traces.exists():
        for s in read_spans(traces):
            if s.get("name") == "llm.chat":
                a = s.get("attributes", {})
                model = model or a.get("gen_ai.request.model")
                calls.append({"ok": s.get("status") == "ok",
                              "in": a.get("gen_ai.usage.input_tokens", 0) or 0,
                              "out": a.get("gen_ai.usage.output_tokens", 0) or 0,
                              "latency": (a.get("llm.latency_ms") or s.get("duration_ms", 0)) / 1000,
                              "tps": a.get("llm.tokens_per_second", 0) or 0,
                              "load_ms": a.get("ollama.load_duration_ms", 0) or 0})
    elif (run_dir / "steps.jsonl").exists():
        for line in (run_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines():
            step = json.loads(line)
            if step.get("source") in ("llm", "fallback") and step.get("llm_latency"):
                calls.append({"ok": step["source"] == "llm", "in": step.get("prompt_tokens", 0),
                              "out": step.get("completion_tokens", 0), "latency": step["llm_latency"],
                              "tps": step.get("tokens_per_second", 0), "load_ms": 0})
    lat = [c["latency"] for c in calls if c["ok"]]
    return {
        "traced_model": model,
        "llm_calls": len(calls), "llm_errors": sum(not c["ok"] for c in calls),
        "prompt_tokens": sum(c["in"] for c in calls), "completion_tokens": sum(c["out"] for c in calls),
        "avg_prompt_tokens": round(statistics.mean(c["in"] for c in calls)) if calls else 0,
        "avg_completion_tokens": round(statistics.mean(c["out"] for c in calls)) if calls else 0,
        "p50_latency": round(_pct(lat, 0.5), 2), "p95_latency": round(_pct(lat, 0.95), 2),
        "tokens_per_second": round(statistics.median(c["tps"] for c in calls if c["tps"]), 1)
        if any(c["tps"] for c in calls) else 0.0,
        "model_load_s": round(sum(c["load_ms"] for c in calls if c["load_ms"] > 1000) / 1000, 1),
    }


def summarize_run(run_dir: str | Path) -> dict:
    run_dir = Path(run_dir)
    summary: dict = {}
    if (run_dir / "summary.json").exists():
        summary = json.loads((run_dir / "summary.json").read_text())
    elif (run_dir / "episodes.json").exists():  # interrupted run: rebuild what we can
        eps = json.loads((run_dir / "episodes.json").read_text())
        summary = {"episodes": len(eps), "exits": sum(e.get("end_reason") == "exit" for e in eps),
                   "successes": sum(e.get("end_reason") in ("exit", "completed") for e in eps),
                   "deaths": sum(e.get("end_reason") == "died" for e in eps),
                   "model": eps[0].get("model") if eps else None}
    row = {"run": run_dir.name, "kind": summary.get("kind", "play"), "model": summary.get("model"),
           "playbook": summary.get("playbook"), "preset": summary.get("preset")}
    if row["kind"] == "eval":
        row.update(passed=summary.get("passed"), total=summary.get("total"),
                   accuracy=summary.get("accuracy"))
    else:
        for key in ("episodes", "successes", "exits", "deaths", "avg_turns", "avg_kills", "fallback_rate",
                    "scenario"):
            row[key] = summary.get(key)
    row.update(llm_stats(run_dir))
    row["model"] = row["model"] or row.pop("traced_model")
    row.pop("traced_model", None)
    return row


def find_runs(runs_dir: str | Path, last: int = 1) -> list[Path]:
    runs = sorted((p for p in Path(runs_dir).glob("*") if p.is_dir() and
                   ((p / "summary.json").exists() or (p / "traces.jsonl").exists())),
                  key=lambda p: p.stat().st_mtime)
    return runs[-last:] if last else runs


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:.0f}"
    if isinstance(v, int) and abs(v) >= 10000:
        return f"{v:,}"
    return str(v)


def markdown_table(rows: list[dict]) -> str:
    """One Markdown table per kind of run (games and evals have different outcomes)."""
    out = []
    plays = [r for r in rows if r["kind"] != "eval"]
    evals = [r for r in rows if r["kind"] == "eval"]
    llm_cols = [("LLM calls", "llm_calls"), ("errors", "llm_errors"), ("tokens in", "prompt_tokens"),
                ("tokens out", "completion_tokens"), ("in/call", "avg_prompt_tokens"),
                ("out/call", "avg_completion_tokens"), ("p50 s", "p50_latency"), ("p95 s", "p95_latency"),
                ("tok/s", "tokens_per_second")]
    if plays:
        cols = [("model", "model"), ("playbook", "playbook"), ("scenario", "scenario"),
                ("success", None), ("deaths", "deaths"), ("avg turns", "avg_turns"),
                ("avg kills", "avg_kills"), ("fallback", "fallback_rate"), *llm_cols]
        out.append(_table(plays, cols, lambda r: "-" if r.get("episodes") is None  # still running
                          else f"{r.get('successes') or 0}/{r['episodes']}"))
    if evals:
        cols = [("model", "model"), ("playbook", "playbook"), ("correct", None), *llm_cols]
        out.append(_table(evals, cols, lambda r: f"{r.get('passed') or 0}/{r.get('total') or 0}"))
    return "\n\n".join(out)


def _table(rows: list[dict], cols: list[tuple[str, str | None]], outcome) -> str:
    header = "| " + " | ".join(c for c, _ in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [header, sep]
    for r in rows:
        cells = [outcome(r) if key is None else _fmt(r.get(key)) for _, key in cols]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
