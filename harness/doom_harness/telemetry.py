"""Local AI observability: traces of every run, episode, turn, game command and LLM call.

Every span is written to ``<run dir>/traces.jsonl`` (one JSON object per line, always on,
no extra services). Each ``llm.chat`` span holds the full request (messages, JSON schema,
options), the raw and parsed response, token counts and Ollama's own timings. The system
prompt is identical on every call, so the file stores it once under ``prompts/`` and
refers to it by hash.

When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (and the ``otel`` extra is installed), the same
spans are also exported over OTLP/HTTP, using the OpenTelemetry GenAI and OpenInference
attribute names, so a local trace UI such as Arize Phoenix shows prompts, responses and
tokens per call (see docker-compose.yml, profile ``observability``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .llm import LLM, LLMError, LLMReply

log = logging.getLogger(__name__)

SERVICE_NAME = "doom-harness"


class Span:
    """One timed operation. ``attributes`` are small scalars (exported everywhere);
    ``payload`` holds big structured data such as messages and responses."""

    def __init__(self, name: str, trace_id: str, parent: "Span | None", kind: str, attributes: dict):
        self.name = name
        self.kind = kind
        self.trace_id = trace_id
        self.span_id = secrets.token_hex(8)
        self.parent_id = parent.span_id if parent else None
        self.start = time.time()
        self._t0 = time.monotonic()
        self.duration_ms = 0.0
        self.attributes: dict[str, Any] = dict(attributes)
        self.payload: dict[str, Any] = {}
        self.events: list[dict] = []
        self.status = "ok"
        self.error_message = ""
        self.otel: Any = None

    def set(self, **attributes: Any) -> "Span":
        self.attributes.update(attributes)
        return self

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append({"name": name, "time": _iso(time.time()), **attributes})

    def error(self, message: str) -> None:
        self.status, self.error_message = "error", message


class Tracer:
    """Nested spans on a stack (the harness is single-threaded)."""

    def __init__(self, directory: str | Path | None = None, enabled: bool = True,
                 otel: bool | None = None, resource: dict | None = None, otel_exporter: Any = None):
        self.dir = Path(directory) if directory else None
        self.enabled = enabled and self.dir is not None
        self._file = None
        self._saved_blobs: set[str] = set()
        self._stack: list[Span] = []
        self._provider = self._otel_tracer = None
        self.spans_written = 0
        if self.enabled:
            assert self.dir is not None
            self.dir.mkdir(parents=True, exist_ok=True)
            self._file = (self.dir / "traces.jsonl").open("a", encoding="utf-8")
        if otel is None:
            otel = otel_exporter is not None or bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                                                     or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
        if otel:
            self._setup_otel(resource or {}, otel_exporter)

    @property
    def otel_enabled(self) -> bool:
        return self._otel_tracer is not None

    # ------------------------------------------------------------ spans
    @contextmanager
    def span(self, name: str, kind: str = "internal", **attributes: Any) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else None
        trace_id = parent.trace_id if parent else secrets.token_hex(16)
        span = Span(name, trace_id, parent, kind, attributes)
        if self._otel_tracer is not None:
            span.otel = self._start_otel(span, parent)
        self._stack.append(span)
        try:
            yield span
        except BaseException as exc:
            if span.status == "ok":
                span.error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._stack.pop()
            span.duration_ms = (time.monotonic() - span._t0) * 1000
            self._write(span)
            if span.otel is not None:
                self._end_otel(span)

    @property
    def current(self) -> Span | None:
        return self._stack[-1] if self._stack else None

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._provider is not None:
            try:
                self._provider.shutdown()  # flushes pending spans
            except Exception as exc:  # never let telemetry break a run
                log.warning("could not flush OpenTelemetry spans: %s", exc)
            self._provider = self._otel_tracer = None

    # ------------------------------------------------------- local JSONL
    def _write(self, span: Span) -> None:
        if self._file is None:
            return
        record = {
            "trace_id": span.trace_id, "span_id": span.span_id, "parent_id": span.parent_id,
            "name": span.name, "kind": span.kind, "start": _iso(span.start),
            "duration_ms": round(span.duration_ms, 1), "status": span.status,
            "attributes": span.attributes,
        }
        if span.error_message:
            record["error"] = span.error_message
        if span.payload:
            record["payload"] = self._dedupe(span.payload)
        if span.events:
            record["events"] = span.events
        self._file.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._file.flush()
        self.spans_written += 1

    def _dedupe(self, payload: dict) -> dict:
        """Store each distinct system prompt once, as prompts/<hash>.md."""
        messages = payload.get("messages")
        if not messages or self.dir is None:
            return payload
        out = []
        for m in messages:
            content = m.get("content", "")
            if m.get("role") == "system" and len(content) > 500:
                digest = hashlib.sha256(content.encode()).hexdigest()[:12]
                ref = f"prompts/system_{digest}.md"
                if digest not in self._saved_blobs:
                    (self.dir / "prompts").mkdir(exist_ok=True)
                    (self.dir / ref).write_text(content, encoding="utf-8")
                    self._saved_blobs.add(digest)
                out.append({"role": "system", "content_ref": ref, "chars": len(content)})
            else:
                out.append(m)
        return {**payload, "messages": out}

    # ----------------------------------------------------- OpenTelemetry
    def _setup_otel(self, resource: dict, exporter: Any = None) -> None:
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
            if exporter is None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter()  # endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
        except ImportError:
            log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but OpenTelemetry is not installed "
                        "(pip install 'doom-harness[otel]'); traces stay local only")
            return
        service = os.environ.get("OTEL_SERVICE_NAME", SERVICE_NAME)
        attrs = {"service.name": service, "openinference.project.name": service,
                 **{k: _otel_value(v) for k, v in resource.items() if v is not None}}
        self._provider = TracerProvider(resource=Resource.create(attrs))
        processor = BatchSpanProcessor if type(exporter).__name__ == "OTLPSpanExporter" else SimpleSpanProcessor
        self._provider.add_span_processor(processor(exporter))
        self._otel_tracer = self._provider.get_tracer("doom_harness")

    def _start_otel(self, span: Span, parent: Span | None) -> Any:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind
        ctx = trace.set_span_in_context(parent.otel) if parent and parent.otel is not None else None
        kind = SpanKind.CLIENT if span.kind == "client" else SpanKind.INTERNAL
        return self._otel_tracer.start_span(span.name, context=ctx, kind=kind,
                                            start_time=int(span.start * 1e9))

    def _end_otel(self, span: Span) -> None:
        from opentelemetry.trace import Status, StatusCode
        otel = span.otel
        attrs = dict(span.attributes)
        attrs.update(_openinference(span))
        for key, value in attrs.items():
            value = _otel_value(value)
            if value is not None:
                otel.set_attribute(key, value)
        for ev in span.events:
            otel.add_event(ev["name"], {k: _otel_value(v) for k, v in ev.items()
                                        if k not in ("name", "time") and _otel_value(v) is not None})
        if span.status == "error":
            otel.set_status(Status(StatusCode.ERROR, span.error_message))
        else:
            otel.set_status(Status(StatusCode.OK))
        otel.end(end_time=int((span.start + span.duration_ms / 1000) * 1e9))


class TracedLLM:
    """Wraps any LLM so that every ``chat`` call becomes an ``llm.chat`` span."""

    def __init__(self, llm: LLM, tracer: Tracer):
        self.llm = llm
        self.tracer = tracer
        self.model = llm.model

    def __getattr__(self, name: str) -> Any:  # ensure_model, warm_up, list_models...
        return getattr(self.llm, name)

    def chat(self, messages: list[dict], schema: dict) -> LLMReply:
        options = dict(getattr(self.llm, "options", {}) or {})
        attrs = {
            "openinference.span.kind": "LLM",
            "gen_ai.operation.name": "chat", "gen_ai.system": "ollama", "gen_ai.provider.name": "ollama",
            "gen_ai.request.model": self.model, "llm.model_name": self.model, "llm.provider": "ollama",
            "gen_ai.request.temperature": options.get("temperature"),
            "gen_ai.request.max_tokens": options.get("num_predict"),
            "llm.invocation_parameters": json.dumps(options),
            "harness.allowed_actions": list(schema.get("properties", {}).get("action", {}).get("enum", [])),
        }
        with self.tracer.span("llm.chat", kind="client", **{k: v for k, v in attrs.items() if v is not None}) as span:
            span.payload = {"messages": messages, "schema": schema, "options": options}
            try:
                reply = self.llm.chat(messages, schema)
            except LLMError as exc:
                span.error(str(exc))
                raise
            raw = reply.raw or {}
            usage_in, usage_out = reply.prompt_tokens, reply.completion_tokens
            span.set(**{
                "gen_ai.usage.input_tokens": usage_in, "gen_ai.usage.output_tokens": usage_out,
                "llm.token_count.prompt": usage_in, "llm.token_count.completion": usage_out,
                "llm.token_count.total": usage_in + usage_out,
                "gen_ai.response.model": raw.get("model", self.model),
                "gen_ai.response.finish_reasons": [raw.get("done_reason") or "stop"],
                "llm.latency_ms": round(reply.latency * 1000, 1),
                "llm.tokens_per_second": round(reply.tokens_per_second, 1),
                **{f"ollama.{k}_ms": round(int(raw[k]) / 1e6, 1)
                   for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
                   if raw.get(k)},
                "harness.action": reply.data.get("action"), "harness.arg": reply.data.get("arg"),
            })
            span.payload.update(response=reply.text, parsed=reply.data)
            return reply


# ------------------------------------------------------------------ helpers
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _otel_value(value: Any) -> Any:
    """OpenTelemetry attributes must be scalars or homogeneous lists of scalars."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    if isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                for v in value):
        return list(value)
    return json.dumps(value, default=str)


# OpenInference span kinds, so trace UIs show a readable tree instead of "unknown".
_KINDS = {"game.command": "TOOL", "llm.chat": "LLM"}


def _openinference(span: Span) -> dict:
    """Map a span to the OpenInference attributes trace UIs read (kind, input, output,
    and for LLM calls the full message list)."""
    a = span.attributes
    attrs: dict[str, Any] = {"openinference.span.kind": a.get("openinference.span.kind")
                             or _KINDS.get(span.name, "CHAIN")}
    if span.name == "turn" and a.get("action"):
        arg = "" if a.get("arg") in (None, "none") else f" {a['arg']}"
        attrs["input.value"] = a.get("thought") or ""
        attrs["output.value"] = f"{a['action']}{arg} -> {a.get('status')}: {a.get('reason')}"
    elif span.name == "game.command":
        attrs["input.value"] = json.dumps({k[len("command."):]: v for k, v in a.items()
                                           if k.startswith("command.")})
        attrs["input.mime_type"] = "application/json"
        attrs["output.value"] = f"{a.get('status')}: {a.get('reason')}"
    elif span.name in ("episode", "harness.run", "harness.eval", "eval.case"):
        attrs["output.value"] = json.dumps({k: v for k, v in a.items() if v is not None}, default=str)
        attrs["output.mime_type"] = "application/json"
    p = span.payload
    if not p or "messages" not in p:
        return attrs
    for i, m in enumerate(p["messages"]):
        attrs[f"llm.input_messages.{i}.message.role"] = m.get("role", "")
        attrs[f"llm.input_messages.{i}.message.content"] = m.get("content", "")
    attrs["input.value"] = json.dumps({"messages": p["messages"]}, ensure_ascii=False)
    attrs["input.mime_type"] = "application/json"
    if "response" in p:
        attrs["llm.output_messages.0.message.role"] = "assistant"
        attrs["llm.output_messages.0.message.content"] = p["response"]
        attrs["output.value"] = p["response"]
        attrs["output.mime_type"] = "application/json"
    return attrs


# ------------------------------------------------------------ reading back
def read_spans(path: str | Path) -> list[dict]:
    """Load a traces.jsonl file (skipping a torn last line after a crash)."""
    spans = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            try:
                spans.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return spans
