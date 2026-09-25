import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))  # fake_ollama
sys.path.insert(0, str(HERE.parent))  # doom_harness


def _load(name: str) -> dict:
    return json.loads((HERE / "fixtures" / name).read_text())


@pytest.fixture
def obs_start() -> dict:
    return _load("obs_map01_start.json")


@pytest.fixture
def obs_enemy() -> dict:
    return _load("result_explore_enemy.json")["observation"]


@pytest.fixture
def result_enemy() -> dict:
    return _load("result_explore_enemy.json")


@pytest.fixture
def obs_basic() -> dict:
    return _load("obs_basic.json")


@pytest.fixture
def make_obs(obs_start):
    """Build a variant of a real observation: make_obs(enemies=[...], player={...})."""
    def factory(**overrides):
        obs = copy.deepcopy(obs_start)
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(obs.get(key), dict):
                obs[key].update(value)
            else:
                obs[key] = value
        return obs
    return factory


def enemy(id_=100, name="DoomImp", distance=300, bearing=10.0, **kw) -> dict:
    return {"id": id_, "name": name, "distance": distance, "bearing": bearing, "aimed": False,
            "threat": kw.pop("threat", 2), "attack": "projectile", "note": kw.pop("note", "throws fireballs"),
            "height": 0, "screen_width": 40, **kw}


def item(id_=200, name="Medikit", kind="health", label="medikit (+25 health)", distance=200,
         bearing=-20.0, path_distance=220, useful=True, value=5) -> dict:
    return {"id": id_, "name": name, "kind": kind, "label": label, "distance": distance, "bearing": bearing,
            "value": value, "useful": useful, "path_distance": path_distance}
