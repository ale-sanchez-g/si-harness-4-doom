import json

import httpx
import pytest
from fake_ollama import FakeOllama
from test_llm_agent import FakeDoom

from doom_harness.actions import TurnView
from doom_harness.agent import Agent
from doom_harness.cli import main
from doom_harness.client import DoomClient
from doom_harness.config import HarnessConfig
from doom_harness.llm import LLMError, OllamaLLM
from doom_harness.recorder import RunRecorder
from doom_harness.report import markdown_table, summarize_run
from doom_harness.telemetry import TracedLLM, Tracer, read_spans


def by_name(spans: list[dict], name: str) -> list[dict]:
    return [s for s in spans if s["name"] == name]


def test_spans_nest_and_errors_are_recorded(tmp_path):
    tracer = Tracer(tmp_path)
    with tracer.span("outer", a=1) as outer:
        with tracer.span("inner") as inner:
            inner.set(b=2)
            inner.event("note", detail="x")
        with pytest.raises(RuntimeError):
            with tracer.span("boom"):
                raise RuntimeError("bad")
    tracer.close()
    spans = {s["name"]: s for s in read_spans(tmp_path / "traces.jsonl")}
    assert spans["inner"]["parent_id"] == spans["outer"]["span_id"] == spans["boom"]["parent_id"]
    assert spans["inner"]["trace_id"] == spans["outer"]["trace_id"]
    assert spans["outer"]["parent_id"] is None and spans["outer"]["attributes"] == {"a": 1}
    assert spans["inner"]["attributes"]["b"] == 2 and spans["inner"]["events"][0]["detail"] == "x"
    assert spans["boom"]["status"] == "error" and "bad" in spans["boom"]["error"]


def test_disabled_tracer_writes_nothing(tmp_path):
    tracer = Tracer(None)
    with tracer.span("x"):
        pass
    assert tracer.spans_written == 0 and not tracer.otel_enabled


def test_traced_llm_records_prompt_response_and_tokens(tmp_path, obs_enemy):
    fake = FakeOllama()
    tracer = Tracer(tmp_path)
    llm = TracedLLM(OllamaLLM("http://o", "granite4.2:3b", transport=fake.transport()), tracer)
    view = TurnView(obs_enemy)
    system = "You play DOOM. " * 100  # long system prompts are stored once, by reference
    for _ in range(2):
        llm.chat([{"role": "system", "content": system}, {"role": "user", "content": "TURN 1"}], view.schema())
    tracer.close()
    calls = by_name(read_spans(tmp_path / "traces.jsonl"), "llm.chat")
    assert len(calls) == 2
    a, p = calls[0]["attributes"], calls[0]["payload"]
    assert a["gen_ai.request.model"] == "granite4.2:3b" and a["gen_ai.system"] == "ollama"
    assert a["gen_ai.usage.input_tokens"] == 420 and a["gen_ai.usage.output_tokens"] == 28
    assert a["llm.token_count.total"] == 448 and a["llm.tokens_per_second"] == pytest.approx(20.0)
    assert a["gen_ai.request.temperature"] == 0.2 and a["harness.action"] == "attack"
    assert "attack" in a["harness.allowed_actions"]
    assert p["messages"][1] == {"role": "user", "content": "TURN 1"}
    assert p["messages"][0]["content_ref"].startswith("prompts/system_")
    assert (tmp_path / p["messages"][0]["content_ref"]).read_text() == system
    assert len(list((tmp_path / "prompts").iterdir())) == 1
    assert json.loads(p["response"])["action"] == "attack" and p["parsed"]["arg"] == "E1"
    assert p["schema"]["properties"]["action"]["enum"] == view.action_names


def test_traced_llm_marks_failed_calls(tmp_path):
    def broken(request):
        if request.url.path == "/api/chat":
            return httpx.Response(500, text="model crashed")
        return httpx.Response(200, json={"capabilities": []})
    tracer = Tracer(tmp_path)
    llm = TracedLLM(OllamaLLM("http://o", "m", transport=httpx.MockTransport(broken)), tracer)
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "x"}], {"properties": {}})
    tracer.close()
    [span] = read_spans(tmp_path / "traces.jsonl")
    assert span["status"] == "error" and "500" in span["error"]


def test_agent_run_is_fully_traced(tmp_path, obs_start, result_enemy):
    doom = FakeDoom(obs_start, result_enemy, episode_len=3)
    cfg = HarnessConfig(policy="llm", episodes=1, max_steps=10, runs_dir=str(tmp_path), scenario="freedoom2",
                        post_to_viewer=False)
    recorder = RunRecorder(tmp_path, "traced")
    tracer = Tracer(recorder.dir)
    llm = TracedLLM(OllamaLLM("http://o", cfg.model, transport=FakeOllama().transport()), tracer)
    agent = Agent(cfg, DoomClient("http://doom", transport=httpx.MockTransport(doom)), llm, recorder,
                  out=lambda *_: None, tracer=tracer)
    [episode] = agent.run()
    tracer.close()
    summary = recorder.finish({"model": cfg.model, "playbook": cfg.playbook})

    assert episode["llm_calls"] == 3 and episode["prompt_tokens"] == 3 * 420
    assert episode["completion_tokens"] == 3 * 28
    assert summary["prompt_tokens"] == 1260 and summary["llm_calls"] == 3
    spans = read_spans(recorder.dir / "traces.jsonl")
    [run], [ep] = by_name(spans, "harness.run"), by_name(spans, "episode")
    turns, calls, cmds = by_name(spans, "turn"), by_name(spans, "llm.chat"), by_name(spans, "game.command")
    assert len(turns) == len(calls) == len(cmds) == 3
    assert ep["parent_id"] == run["span_id"] and all(t["parent_id"] == ep["span_id"] for t in turns)
    assert {c["parent_id"] for c in calls} == {t["span_id"] for t in turns}
    assert {c["parent_id"] for c in cmds} == {t["span_id"] for t in turns}
    assert len({s["trace_id"] for s in spans}) == 1
    assert turns[0]["attributes"]["action"] == "explore"  # the start fixture has no enemy in view
    assert turns[-1]["attributes"]["action"] == "attack" and turns[-1]["attributes"]["prompt_tokens"] == 420
    assert cmds[-1]["attributes"]["status"] == "episode_over"
    assert ep["attributes"]["end_reason"] == "exit" and run["attributes"]["exits"] == 1

    row = summarize_run(recorder.dir)
    assert row["llm_calls"] == 3 and row["prompt_tokens"] == 1260 and row["avg_completion_tokens"] == 28
    assert row["successes"] == 1 and row["llm_errors"] == 0
    table = markdown_table([row])
    assert "granite4.2:3b" in table and "| 1/1 |" in table and "1260" in table


def test_opentelemetry_export_uses_llm_conventions(tmp_path, obs_enemy):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    tracer = Tracer(tmp_path, otel_exporter=exporter, resource={"harness.model": "granite4.2:3b"})
    assert tracer.otel_enabled
    llm = TracedLLM(OllamaLLM("http://o", "granite4.2:3b", transport=FakeOllama().transport()), tracer)
    with tracer.span("turn", turn=1):
        llm.chat([{"role": "system", "content": "rules"}, {"role": "user", "content": "TURN 1"}],
                 TurnView(obs_enemy).schema())
    tracer.close()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    chat, turn = spans["llm.chat"], spans["turn"]
    assert chat.parent.span_id == turn.context.span_id
    a = chat.attributes
    assert a["openinference.span.kind"] == "LLM" and a["llm.model_name"] == "granite4.2:3b"
    assert a["llm.input_messages.0.message.role"] == "system"
    assert a["llm.input_messages.0.message.content"] == "rules"  # full prompt in the trace UI
    assert a["llm.input_messages.1.message.content"] == "TURN 1"
    assert '"attack"' in a["llm.output_messages.0.message.content"]
    assert a["gen_ai.usage.input_tokens"] == 420 and a["llm.token_count.completion"] == 28
    assert turn.attributes["openinference.span.kind"] == "CHAIN" and chat.status.is_ok
    assert chat.resource.attributes["service.name"] == "doom-harness"
    assert chat.resource.attributes["openinference.project.name"] == "doom-harness"


def test_presets_pick_model_and_playbook(monkeypatch):
    for var in ("OLLAMA_MODEL", "HARNESS_PLAYBOOK", "HARNESS_PRESET"):
        monkeypatch.delenv(var, raising=False)
    cfg = HarnessConfig()
    assert (cfg.preset, cfg.model, cfg.playbook) == ("m", "granite4.2:3b", "default")
    monkeypatch.setenv("HARNESS_PRESET", "xs")
    cfg = HarnessConfig()
    assert (cfg.model, cfg.playbook) == ("granite4:350m-h", "small")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")  # an explicit model wins over the preset
    cfg = HarnessConfig()
    assert (cfg.model, cfg.playbook) == ("qwen3:4b", "small")
    cfg.apply_preset("S")
    assert (cfg.preset, cfg.model, cfg.playbook) == ("s", "granite4:1b-h", "small")
    with pytest.raises(ValueError):
        cfg.apply_preset("xl")
    for name in ("xs", "s", "m"):
        cfg.apply_preset(name)
        assert cfg.playbook_path().exists()


def test_report_command_prints_latest_run(tmp_path, monkeypatch, capsys, obs_start, result_enemy):
    monkeypatch.setenv("HARNESS_RUNS_DIR", str(tmp_path))
    assert main(["report"]) == 1  # nothing recorded yet
    recorder = RunRecorder(tmp_path, "r")
    tracer = Tracer(recorder.dir)
    cfg = HarnessConfig(policy="llm", episodes=1, max_steps=5, runs_dir=str(tmp_path), post_to_viewer=False)
    Agent(cfg, DoomClient("http://doom", transport=httpx.MockTransport(FakeDoom(obs_start, result_enemy, 2))),
          TracedLLM(OllamaLLM("http://o", cfg.model, transport=FakeOllama().transport()), tracer), recorder,
          out=lambda *_: None, tracer=tracer).run()
    tracer.close()
    recorder.finish({"model": cfg.model, "playbook": cfg.playbook})
    capsys.readouterr()
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    assert "| model |" in out and "granite4.2:3b" in out and "840" in out


def test_report_handles_a_run_still_in_progress(tmp_path, obs_enemy):
    tracer = Tracer(tmp_path / "running")
    llm = TracedLLM(OllamaLLM("http://o", "granite4:1b-h", transport=FakeOllama().transport()), tracer)
    llm.chat([{"role": "user", "content": "TURN 1"}], TurnView(obs_enemy).schema())
    tracer.close()  # no summary.json yet: the run is still going
    row = summarize_run(tmp_path / "running")
    assert row["model"] == "granite4:1b-h" and row["llm_calls"] == 1 and row["episodes"] is None
    assert "| granite4:1b-h | - | - | - |" in markdown_table([row])
