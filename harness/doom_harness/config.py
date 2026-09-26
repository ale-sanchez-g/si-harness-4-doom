"""Harness configuration (environment variables, overridable from the CLI)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PLAYBOOK_DIR = Path(os.environ.get("HARNESS_PLAYBOOK_DIR", PACKAGE_DIR.parent / "playbooks"))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return str(_env(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


# Model size presets for side-by-side comparisons (HARNESS_PRESET / --preset).
# OLLAMA_MODEL and HARNESS_PLAYBOOK, when set, override the preset's choices.
PRESETS: dict[str, dict[str, str]] = {
    "xs": {"model": "granite4:350m-h", "playbook": "small",
           "about": "IBM Granite 4.0 H 350M: 340M params, ~2 s per decision on a 4-core CPU"},
    "s": {"model": "granite4:1b-h", "playbook": "small",
          "about": "IBM Granite 4.0 H 1B: 1.5B params, ~7 s per decision on a 4-core CPU"},
    "m": {"model": "granite4.2:3b", "playbook": "default",
          "about": "IBM Granite 4.2 3B: 3.7B params, ~15 s per decision on a 4-core CPU"},
}
DEFAULT_PRESET = "m"


def preset(name: str) -> dict[str, str]:
    try:
        return PRESETS[name.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown preset {name!r}: choose one of {', '.join(PRESETS)}") from None


@dataclass
class HarnessConfig:
    # Where things live
    doom_url: str = field(default_factory=lambda: _env("DOOM_URL", "http://localhost:8000"))
    ollama_host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://localhost:11434"))
    runs_dir: str = field(default_factory=lambda: _env("HARNESS_RUNS_DIR", "runs"))

    # The model: a size preset (xs | s | m), unless OLLAMA_MODEL names one explicitly
    preset: str = field(default_factory=lambda: _env("HARNESS_PRESET", DEFAULT_PRESET))
    model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", ""))
    temperature: float = field(default_factory=lambda: _env_float("HARNESS_TEMPERATURE", 0.2))
    num_ctx: int = field(default_factory=lambda: _env_int("HARNESS_NUM_CTX", 8192))
    num_predict: int = field(default_factory=lambda: _env_int("HARNESS_NUM_PREDICT", 200))
    llm_timeout: float = field(default_factory=lambda: _env_float("HARNESS_LLM_TIMEOUT", 180.0))
    keep_alive: str = field(default_factory=lambda: _env("HARNESS_KEEP_ALIVE", "30m"))
    auto_pull: bool = field(default_factory=lambda: _env_bool("HARNESS_AUTO_PULL", True))
    # Ask the model for a short "thought" before the action (chain-of-thought lite).
    reasoning: bool = field(default_factory=lambda: _env_bool("HARNESS_REASONING", True))
    # Ollama's `think` flag for reasoning models: unset = auto (off when the model
    # supports thinking, it is too slow for a game loop), true/false = force.
    think: bool | None = field(default_factory=lambda: None if _env("HARNESS_THINK") is None
                               else _env_bool("HARNESS_THINK", False))

    # The instructions (the "harness" around the LLM)
    playbook: str = field(default_factory=lambda: _env("HARNESS_PLAYBOOK", ""))
    history: int = field(default_factory=lambda: _env_int("HARNESS_HISTORY", 4))

    # The game
    policy: str = field(default_factory=lambda: _env("HARNESS_POLICY", "llm"))  # llm | scripted
    scenario: str | None = field(default_factory=lambda: _env("HARNESS_SCENARIO"))
    map: str | None = field(default_factory=lambda: _env("HARNESS_MAP"))
    skill: int | None = field(default_factory=lambda: int(_env("HARNESS_SKILL")) if _env("HARNESS_SKILL") else None)
    seed: int | None = None
    episodes: int = field(default_factory=lambda: _env_int("HARNESS_EPISODES", 1))
    max_steps: int = field(default_factory=lambda: _env_int("HARNESS_MAX_STEPS", 400))
    # Game-time limit per episode (seconds, 0 = scenario default)
    episode_timeout: float = field(default_factory=lambda: _env_float("HARNESS_EPISODE_TIMEOUT", 0))
    # After finishing a level, continue with the next map instead of restarting.
    campaign: bool = field(default_factory=lambda: _env_bool("HARNESS_CAMPAIGN", False))
    explore_seconds: float = field(default_factory=lambda: _env_float("HARNESS_EXPLORE_SECONDS", 5.0))
    attack_seconds: float = field(default_factory=lambda: _env_float("HARNESS_ATTACK_SECONDS", 2.0))
    post_to_viewer: bool = field(default_factory=lambda: _env_bool("HARNESS_POST_TO_VIEWER", True))
    save_prompts: bool = field(default_factory=lambda: _env_bool("HARNESS_SAVE_PROMPTS", True))
    # Local traces (runs/<run>/traces.jsonl). OpenTelemetry export is switched on by the
    # standard OTEL_EXPORTER_OTLP_ENDPOINT variable (see telemetry.py).
    trace: bool = field(default_factory=lambda: _env_bool("HARNESS_TRACE", True))

    def __post_init__(self) -> None:
        chosen = preset(self.preset)
        self.model = self.model or chosen["model"]
        self.playbook = self.playbook or chosen["playbook"]

    def apply_preset(self, name: str) -> None:
        """Switch to a size preset (model and playbook together)."""
        chosen = preset(name)
        self.preset, self.model, self.playbook = name.strip().lower(), chosen["model"], chosen["playbook"]

    def playbook_path(self) -> Path:
        p = Path(self.playbook)
        if p.suffix == ".md" and p.exists():
            return p
        candidate = DEFAULT_PLAYBOOK_DIR / f"{self.playbook}.md"
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"playbook '{self.playbook}' not found (looked in {DEFAULT_PLAYBOOK_DIR})")
