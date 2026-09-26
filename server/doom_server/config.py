"""Server settings, read from environment variables (see docker-compose.yml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("DOOM_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("DOOM_PORT", "8000")))
    scenario: str = field(default_factory=lambda: _env("DOOM_SCENARIO", "freedoom2"))
    map: str = field(default_factory=lambda: _env("DOOM_MAP", ""))
    skill: int = field(default_factory=lambda: int(_env("DOOM_SKILL", "3")))
    resolution: str = field(default_factory=lambda: _env("DOOM_RESOLUTION", "640x480"))
    # 35 = real-time playback (nice to watch), 0 = run commands as fast as possible.
    playback_fps: float = field(default_factory=lambda: float(_env("DOOM_PLAYBACK_FPS", "35")))
    # Game-time limit per episode in seconds (0 = scenario default).
    episode_timeout: float = field(default_factory=lambda: float(_env("DOOM_EPISODE_TIMEOUT", "0")))
    # fair: the exit/switches are revealed once seen. full: the whole map is known.
    knowledge: str = field(default_factory=lambda: _env("DOOM_KNOWLEDGE", "fair"))
    # Report monsters that are close but out of sight (like hearing them). 0 = off.
    hearing_range: float = field(default_factory=lambda: float(_env("DOOM_HEARING_RANGE", "0")))
    wad_dir: str = field(default_factory=lambda: _env("DOOM_WAD_DIR", "/wads"))
    autostart: bool = field(default_factory=lambda: _env_bool("DOOM_AUTOSTART", True))
    render_hud: bool = field(default_factory=lambda: _env_bool("DOOM_RENDER_HUD", True))
    jpeg_quality: int = field(default_factory=lambda: int(_env("DOOM_JPEG_QUALITY", "80")))
