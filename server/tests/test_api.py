"""API tests that drive the real ViZDoom engine (headless, unthrottled)."""

import pytest
from fastapi.testclient import TestClient

from doom_server.api import create_app
from doom_server.config import Settings


@pytest.fixture(scope="module")
def client():
    settings = Settings()
    settings.playback_fps = 0
    settings.autostart = False
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def cmd(client, **body):
    r = client.post("/api/command", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_health_and_catalogue(client):
    assert client.get("/api/health").json()["status"] == "ok"
    names = {s["name"] for s in client.get("/api/scenarios").json()}
    assert {"freedoom2", "freedoom1", "basic", "defend_the_center", "doom2"} <= names
    assert {c["name"] for c in client.get("/api/commands").json()} >= {"attack", "explore", "goto_exit"}
    assert client.get("/").status_code == 200


def test_errors(client):
    assert client.post("/api/episode", json={"scenario": "nope"}).status_code == 404
    r = client.post("/api/episode", json={"scenario": "doom2"})
    assert r.status_code == 400 and "doom2.wad" in r.json()["detail"]
    assert client.post("/api/command", json={"command": "fly"}).status_code == 422


def test_basic_scenario_kill(client):
    obs = client.post("/api/episode", json={"scenario": "basic", "seed": 1}).json()
    assert obs["episode"]["scenario"] == "basic" and not obs["episode"]["finished"]
    assert obs["enemies"], "the basic monster should be visible from the start"
    assert obs["episode"]["commands"] == ["attack", "move", "face", "wait"]
    res = cmd(client, command="explore")
    assert res["status"] == "failed" and "not allowed" in res["reason"]
    res = cmd(client, command="attack", target_id=obs["enemies"][0]["id"], duration=3)
    assert res["status"] in ("completed", "episode_over")
    assert "killed" in res["reason"] or res["status"] == "episode_over"
    assert res["changes"]["kills"] == 1
    assert any(e["type"] == "kill" for e in res["events"])
    # The scenario script ends the episode a few tics after the monster dies.
    res = cmd(client, command="wait", duration=2)
    assert res["status"] == "episode_over"
    assert res["observation"]["episode"]["end_reason"] == "completed"
    assert cmd(client, command="wait")["status"] == "episode_over"


def test_map01_explore_turn_and_step(client):
    obs = client.post("/api/episode", json={"scenario": "freedoom2", "map": "MAP01", "seed": 2}).json()
    p = obs["player"]
    assert (p["health"], p["weapon"], p["ammo"]) == (100, "pistol", 50)
    assert set(obs["walls"]) == {"front", "front_left", "left", "back_left", "back", "back_right", "right",
                                 "front_right"}
    angle = p["angle"]
    res = cmd(client, command="turn", degrees=90)
    assert res["status"] == "completed"
    assert abs(((res["observation"]["player"]["angle"] - angle - 90) + 180) % 360 - 180) < 1.0
    before = res["observation"]["episode"]["explored_percent"]
    res = cmd(client, command="explore", duration=4)
    assert res["status"] in ("completed", "interrupted")
    assert res["observation"]["episode"]["explored_percent"] > before
    assert res["changes"]["moved"] > 100

    r = client.post("/api/step", json={"buttons": {"MOVE_FORWARD": 1}, "tics": 5})
    assert r.status_code == 200 and r.json()["episode"]["tic"] > res["observation"]["episode"]["tic"]
    assert client.post("/api/step", json={"buttons": {"JUMP_HIGH": 1}}).status_code == 400

    frame = client.get("/api/frame.jpg")
    assert frame.status_code == 200 and frame.content[:2] == b"\xff\xd8"
    png = client.get("/api/map.png?size=256")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    status = client.get("/api/status").json()
    assert status["running"] and status["map"] == "MAP01"


def test_map01_scripted_run_reaches_exit(client):
    """A tiny rule-based loop must be able to finish MAP01 through the command API."""
    obs = client.post("/api/episode", json={"scenario": "freedoom2", "map": "MAP01", "seed": 7}).json()
    for _ in range(60):
        if obs["enemies"]:
            res = cmd(client, command="attack", target_id=obs["enemies"][0]["id"], duration=3)
        elif obs["exit"] and obs["exit"]["path_distance"] and obs["episode"]["explored_percent"] > 55:
            res = cmd(client, command="goto_exit")
        else:
            res = cmd(client, command="explore", duration=6)
        obs = res["observation"]
        if res["status"] == "episode_over":
            break
    assert obs["episode"]["end_reason"] == "exit", obs["episode"]
    assert obs["player"]["kills"] >= 3


def test_goto_item_and_use(client):
    obs = client.post("/api/episode", json={"scenario": "freedoom2", "map": "MAP01", "seed": 3}).json()
    for _ in range(6):
        if obs["enemies"]:
            obs = cmd(client, command="attack", duration=3)["observation"]
            continue
        wanted = [i for i in obs["items"] + obs["known_items"] if i["path_distance"] and i["useful"]]
        if wanted:
            res = cmd(client, command="goto", target_id=wanted[0]["id"], interrupt_on_enemy=False)
            assert res["status"] in ("completed", "failed", "interrupted")
            if res["status"] == "completed":
                assert "picked up" in res["reason"]
                assert any(e["type"] == "pickup" for e in res["events"])
                break
        obs = cmd(client, command="explore", duration=4)["observation"]
    res = cmd(client, command="use")
    assert res["status"] == "completed"


def test_agent_log_roundtrip(client):
    client.post("/api/episode", json={"scenario": "basic"})
    entry = client.post("/api/agent/log", json={"step": 1, "thought": "hi", "action": "attack"}).json()
    assert entry["seq"] >= 1
    later = client.get(f"/api/agent/log?after={entry['seq'] - 1}").json()
    assert later[-1]["thought"] == "hi"
    assert client.put("/api/settings", json={"playback_fps": 70}).json() == {"playback_fps": 70}
    client.put("/api/settings", json={"playback_fps": 0})
