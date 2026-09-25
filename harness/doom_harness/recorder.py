"""Run logs: one JSON line per turn plus episode and run summaries."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path


class RunRecorder:
    def __init__(self, runs_dir: str | Path, label: str):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")
        self.dir = Path(runs_dir) / f"{stamp}_{safe}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._steps = (self.dir / "steps.jsonl").open("a", encoding="utf-8")
        self.episodes: list[dict] = []

    def step(self, record: dict) -> None:
        self._steps.write(json.dumps(record, default=str) + "\n")
        self._steps.flush()

    def episode(self, summary: dict) -> None:
        self.episodes.append(summary)
        (self.dir / "episodes.json").write_text(json.dumps(self.episodes, indent=2, default=str))

    def finish(self, meta: dict) -> dict:
        eps = self.episodes
        summary = {
            **meta,
            "episodes": len(eps),
            "exits": sum(1 for e in eps if e.get("end_reason") == "exit"),
            # exit = level finished; completed = scenario objective reached (monster killed, armor found...)
            "successes": sum(1 for e in eps if e.get("end_reason") in ("exit", "completed")),
            "deaths": sum(1 for e in eps if e.get("end_reason") == "died"),
            "avg_kills": round(sum(e.get("kills", 0) for e in eps) / len(eps), 2) if eps else 0,
            "avg_turns": round(sum(e.get("turns", 0) for e in eps) / len(eps), 1) if eps else 0,
            "avg_llm_latency": round(sum(e.get("avg_llm_latency", 0) for e in eps) / len(eps), 2) if eps else 0,
            "fallback_rate": round(sum(e.get("fallbacks", 0) for e in eps) /
                                   max(1, sum(e.get("turns", 0) for e in eps)), 3),
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        self._steps.close()
        return summary
