import copy
import json

import httpx
import pytest
from fake_ollama import FakeOllama

from doom_harness.actions import TurnView
from doom_harness.agent import Agent
from doom_harness.client import DoomClient
from doom_harness.config import HarnessConfig
from doom_harness.llm import LLMError, OllamaLLM, parse_json_reply
from doom_harness.memory import Memory
from doom_harness.policies import LLMPolicy, ScriptedPolicy
from doom_harness.recorder import RunRecorder


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("text", [
    '{"action": "explore", "arg": "none"}',
    '```json\n{"action": "explore", "arg": "none"}\n```',
    'Sure! Here is my move: {"action": "explore", "arg": "none"} good luck',
    '{"thought": "going to explore the big room because', '{"action": "explore", "arg": "none", "thought": "abc',
])
def test_parse_json_reply_variants(text):
    if "action" not in text:
        with pytest.raises(LLMError):
            parse_json_reply(text)
    else:
        assert parse_json_reply(text)["action"] == "explore"


# ------------------------------------------------------------------- ollama
def test_ollama_chat_sends_schema_and_parses(obs_enemy):
    fake = FakeOllama()
    llm = OllamaLLM("http://ollama:11434", "granite4.2:3b", transport=fake.transport())
    view = TurnView(obs_enemy)
    reply = llm.chat([{"role": "user", "content": "hi"}], view.schema())
    assert reply.data["action"] == "attack"
    sent = fake.chats[0]
    assert sent["format"] == view.schema()
    assert sent["stream"] is False and sent["options"]["temperature"] == 0.2
    assert reply.tokens_per_second == pytest.approx(20.0)
    assert sent["think"] is False  # granite4.2 thinks by default: too slow for a game loop


def test_think_flag_only_for_thinking_models(obs_enemy):
    view = TurnView(obs_enemy)
    plain = FakeOllama(capabilities=("completion",))
    OllamaLLM("http://o", "m", transport=plain.transport()).chat([{"role": "user", "content": "x"}], view.schema())
    assert "think" not in plain.chats[0]
    forced = FakeOllama()
    OllamaLLM("http://o", "m", think=True, transport=forced.transport()).chat(
        [{"role": "user", "content": "x"}], view.schema())
    assert forced.chats[0]["think"] is True


def test_ensure_model_pulls_when_missing():
    fake = FakeOllama(models=())
    llm = OllamaLLM("http://ollama:11434", "granite4.2:3b", transport=fake.transport())
    assert not llm.has_model()
    messages = []
    llm.ensure_model(pull=True, progress=messages.append)
    assert llm.has_model()
    assert any("success" in m for m in messages)
    with pytest.raises(LLMError):
        OllamaLLM("http://x", "nope:1b", transport=FakeOllama(models=()).transport()).ensure_model(pull=False)


# ----------------------------------------------------------------- policies
def test_llm_policy_uses_model_answer(obs_enemy):
    fake = FakeOllama()
    policy = LLMPolicy(OllamaLLM("http://o", "m", transport=fake.transport()), "SYSTEM", reminder="RULES!")
    d = policy.decide(TurnView(obs_enemy), Memory(), turn=1)
    assert d.source == "llm" and d.resolved.action == "attack" and d.thought
    assert fake.chats[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    user = fake.chats[0]["messages"][1]["content"]
    assert "ENEMIES IN VIEW" in user and "RULES!" in user
    assert list(fake.chats[0]["format"]["properties"])[0] == "Thought"


def test_llm_policy_retries_then_falls_back(obs_enemy):
    answers = iter(['{"Thought": "x", "action": "dance", "arg": "none"}',
                    '{"Thought": "x", "action": "attack", "arg": "E1"}'])
    fake = FakeOllama(responder=lambda body: next(answers))
    policy = LLMPolicy(OllamaLLM("http://o", "m", transport=fake.transport()), "S", retries=1)
    d = policy.decide(TurnView(obs_enemy), Memory(), 1)
    assert d.source == "llm" and d.resolved.action == "attack" and d.errors
    assert "not possible now" in fake.chats[1]["messages"][-1]["content"]

    broken = FakeOllama(responder=lambda body: "I refuse to answer in JSON")
    policy = LLMPolicy(OllamaLLM("http://o", "m", transport=broken.transport()), "S", retries=1)
    d = policy.decide(TurnView(obs_enemy), Memory(), 1)
    assert d.source == "fallback" and d.resolved.action == "attack" and len(d.errors) == 2


def test_scripted_policy_priorities(make_obs, obs_start):
    from conftest import enemy, item
    p = ScriptedPolicy()
    assert p.decide(TurnView(make_obs(enemies=[enemy()])), Memory()).resolved.action == "attack"
    danger = make_obs(enemies=[enemy()], projectiles=[{"id": 1, "name": "DoomImpBall", "distance": 90,
                                                       "bearing": 0.0, "incoming": True}])
    assert p.decide(TurnView(danger), Memory()).resolved.action == "dodge"
    hurt = make_obs(enemies=[], items=[item()], player={**obs_start["player"], "health": 20})
    assert p.decide(TurnView(hurt), Memory()).resolved.command["command"] == "goto"
    calm = make_obs(enemies=[], items=[], known_items=[])
    assert p.decide(TurnView(calm), Memory()).resolved.action == "explore"


# --------------------------------------------------------------- agent loop
class FakeDoom:
    """Scripted Doom API: every command returns the enemy fixture; the episode ends after N commands."""

    def __init__(self, start: dict, result: dict, episode_len: int = 4):
        self.start, self.result, self.episode_len = start, result, episode_len
        self.commands: list[dict] = []
        self.logs: list[dict] = []
        self.episodes: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/episode":
            self.episodes.append(body)
            self.commands.clear()
            return httpx.Response(200, json=self.start)
        if path == "/api/command":
            self.commands.append(body)
            res = copy.deepcopy(self.result)
            if len(self.commands) >= self.episode_len:
                res["status"], res["reason"] = "episode_over", "exit"
                res["observation"]["episode"].update(finished=True, end_reason="exit")
            return httpx.Response(200, json=res)
        if path == "/api/agent/log":
            self.logs.append(body)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"detail": path})


def test_agent_plays_episode_with_llm(tmp_path, obs_start, result_enemy):
    doom = FakeDoom(obs_start, result_enemy, episode_len=4)
    fake = FakeOllama()
    cfg = HarnessConfig(policy="llm", episodes=2, max_steps=10, runs_dir=str(tmp_path), campaign=True,
                        scenario="freedoom2", seed=5)
    client = DoomClient("http://doom", transport=httpx.MockTransport(doom))
    recorder = RunRecorder(tmp_path, "test")
    out: list[str] = []
    agent = Agent(cfg, client, OllamaLLM("http://o", "granite4.2:3b", transport=fake.transport()),
                  recorder, out=out.append)
    results = agent.run()
    summary = recorder.finish({"model": "granite4.2:3b"})

    assert [r["end_reason"] for r in results] == ["exit", "exit"]
    assert results[0]["turns"] == 4
    assert doom.episodes[0]["scenario"] == "freedoom2" and doom.episodes[0]["seed"] == 5
    assert doom.episodes[1]["map"] == "next"  # campaign mode continues to the next map
    assert doom.commands[-1]["command"] == "attack"  # the fixture shows a zombie
    assert len(doom.logs) == 8 and doom.logs[0]["model"] == "granite4.2:3b"
    steps = (recorder.dir / "steps.jsonl").read_text().strip().splitlines()
    assert len(steps) == 8 and json.loads(steps[0])["source"] == "llm"
    assert (recorder.dir / "system_prompt_freedoom2.md").exists()
    assert summary["exits"] == 2 and summary["fallback_rate"] == 0
    assert any("thought:" in line for line in out)


def test_agent_scripted_policy_needs_no_llm(tmp_path, obs_start, result_enemy):
    doom = FakeDoom(obs_start, result_enemy, episode_len=2)
    cfg = HarnessConfig(policy="scripted", episodes=1, max_steps=5, runs_dir=str(tmp_path), post_to_viewer=False)
    agent = Agent(cfg, DoomClient("http://doom", transport=httpx.MockTransport(doom)), None, None,
                  out=lambda *_: None)
    [result] = agent.run()
    assert result["policy"] == "scripted" and result["turns"] == 2
    assert doom.logs == []  # viewer posting disabled
