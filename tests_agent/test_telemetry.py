"""Step 1: OpenTelemetry setup, content guard, metrics, and OTLP export to a Phoenix-like endpoint."""
import http.server
import threading

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent import telemetry as T
from agent.config import Settings

BASE = dict(
    openai_api_key="sk-test", openai_model_blueprint="gpt-6-luna", openai_model_draft="gpt-6-luna",
    openai_model_eval="gpt-6-luna", openai_tokenizer="o200k_base", openai_timeout_s=120,
    openai_temperature="0", max_fix_rounds=2, max_gap_rounds=1, max_input_tokens=200000,
    max_concurrency=2, faithfulness_min=0.9, relevance_min=0.5, otel_service_name="ipo-agent-test",
    deployment_environment="test", otel_metrics_exporter="none",
)


def settings(**kw):
    vals = {**BASE, "otel_enabled": True, "otel_exporter_otlp_endpoint": "http://localhost:6006",
            "otel_capture_content": False}
    vals.update(kw)
    return Settings(_env_file=None, **vals)


def make(capture=False, instrument=False):
    exp, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tel = T.setup_telemetry(settings(otel_capture_content=capture), span_exporter=exp, metric_reader=reader,
                            instrument=instrument, set_global=False, batch=False)
    return tel, exp, reader


def all_attr_values(spans):
    out = []
    for s in spans:
        out += [str(v) for v in (s.attributes or {}).values()]
        for ev in s.events:
            out += [str(v) for v in (ev.attributes or {}).values()]
    return out


# --- basics ----------------------------------------------------------------------------------

def test_disabled_sends_nothing_and_does_not_fail():
    tel = T.setup_telemetry(settings(otel_enabled=False, otel_exporter_otlp_endpoint=None), set_global=False)
    assert tel.enabled is False
    with tel.span("x", deal_id="d1") as sp:
        assert not sp.is_recording()
    tel.record_llm_call(call="draft", model="m", tokens_in=1, tokens_out=1, latency_s=0.1, cost_usd=0.0)


def test_span_carries_our_attributes_and_resource():
    tel, exp, _ = make()
    with tel.span("draft_part", deal_id="d1", run_id="r1", work_item_id="OB03", pages_sent=12):
        pass
    (s,) = exp.get_finished_spans()
    assert s.name == "draft_part"
    assert dict(s.attributes) == {"deal.id": "d1", "run.id": "r1", "work_item.id": "OB03", "pages.sent": 12}
    r = s.resource.attributes
    assert r["service.name"] == "ipo-agent-test"
    assert r["deployment.environment"] == "test"
    assert r["openinference.project.name"] == "ipo-agent-test"


def test_errors_are_recorded_and_reraised():
    tel, exp, reader = make()
    with pytest.raises(ValueError):
        with tel.node("generate_blueprint", deal_id="d1"):
            raise ValueError("schema failed")
    (s,) = exp.get_finished_spans()
    assert s.name == "node.generate_blueprint"
    assert s.status.status_code == StatusCode.ERROR
    assert any(ev.name == "exception" for ev in s.events)
    names = {m.name for rm in reader.get_metrics_data().resource_metrics
             for sm in rm.scope_metrics for m in sm.metrics}
    assert "ipo.node.duration" in names                  # recorded even when the node fails


def test_get_telemetry_before_setup_is_an_error(monkeypatch):
    monkeypatch.setattr(T, "_current", None)
    with pytest.raises(RuntimeError, match="not set up"):
        T.get_telemetry()


@pytest.mark.parametrize("endpoint,traces,metrics", [
    ("http://localhost:6006", "http://localhost:6006/v1/traces", "http://localhost:6006/v1/metrics"),
    ("http://localhost:6006/", "http://localhost:6006/v1/traces", "http://localhost:6006/v1/metrics"),
    ("https://app.phoenix.arize.com/s/demo/v1/traces", "https://app.phoenix.arize.com/s/demo/v1/traces",
     "https://app.phoenix.arize.com/s/demo/v1/metrics"),
])
def test_urls(endpoint, traces, metrics):
    s = settings(otel_exporter_otlp_endpoint=endpoint, phoenix_api_key="px")
    assert T.traces_url(s) == traces and T.metrics_url(s) == metrics


def test_headers_only_with_key():
    assert T.otlp_headers(settings()) == {}
    assert T.otlp_headers(settings(phoenix_api_key="px-1")) == {"authorization": "Bearer px-1"}


# --- metrics ---------------------------------------------------------------------------------

def _points(reader, name):
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return list(m.data.data_points)
    return []


def test_llm_call_metrics_and_span_attributes():
    tel, exp, reader = make()
    with tel.span("llm"):
        tel.record_llm_call(call="draft", model="gpt-6-luna", tokens_in=30000, tokens_out=2000,
                            latency_s=4.2, cost_usd=0.004)
    (s,) = exp.get_finished_spans()
    assert s.attributes["tokens.in"] == 30000 and s.attributes["cost.usd"] == 0.004
    tokens = {p.attributes["direction"]: p.value for p in _points(reader, "ipo.llm.tokens")}
    assert tokens == {"in": 30000, "out": 2000}
    assert _points(reader, "ipo.llm.cost_usd")[0].value == pytest.approx(0.004)
    assert _points(reader, "ipo.llm.latency")[0].count == 1


def test_part_and_ragas_metrics():
    tel, _, reader = make()
    tel.record_part(section="OB", status="PASSED", fix_rounds=1, faithfulness=0.95)
    tel.record_part(section="RF", status="FAILED", fix_rounds=2, faithfulness=0.6)
    tel.record_relevance(0.7, section="OB")
    tel.record_run_error("blueprint_invalid")
    status = {(p.attributes["section"], p.attributes["status"]): p.value
              for p in _points(reader, "ipo.part.status")}
    assert status == {("OB", "PASSED"): 1, ("RF", "FAILED"): 1}
    assert _points(reader, "ipo.ragas.faithfulness")
    assert _points(reader, "ipo.ragas.context_relevance")[0].count == 1
    assert _points(reader, "ipo.runs.errors")[0].attributes == {"reason": "blueprint_invalid"}


# --- content guard ---------------------------------------------------------------------------

SECRET = "SECRET-DOC-TEXT consolidated PAT 2,54,015.32"


def test_scrubber_drops_content_attributes_when_capture_off():
    tel, exp, _ = make(capture=False)
    with tel.span("llm") as sp:
        sp.set_attributes({"input.value": SECRET, "output.value": SECRET,
                           "llm.input_messages.0.message.content": SECRET,
                           "retrieval.documents.0.document.content": SECRET,
                           "llm.token_count.prompt": 10, "input.mime_type": "text/plain"})
        sp.add_event("chunk", {"output.value": SECRET, "n": 1})
    (s,) = exp.get_finished_spans()
    assert SECRET not in " ".join(all_attr_values([s]))
    assert s.attributes["llm.token_count.prompt"] == 10        # non-content attributes kept
    assert s.attributes["input.mime_type"] == "text/plain"
    assert s.events[0].attributes == {"n": 1}


def test_content_kept_when_capture_on():
    tel, exp, _ = make(capture=True)
    with tel.span("llm") as sp:
        sp.set_attribute("input.value", SECRET)
    assert exp.get_finished_spans()[0].attributes["input.value"] == SECRET


@pytest.mark.parametrize("key,content", [
    ("input.value", True), ("llm.output_messages.0.message.content", True), ("llm.prompts", True),
    ("input.mime_type", False), ("llm.token_count.total", False), ("deal.id", False),
])
def test_is_content_attribute(key, content):
    assert T.is_content_attribute(key) is content


@pytest.fixture
def instrumented():
    """LangChain/LangGraph/OpenAI auto-instrumentation on a fresh in-memory exporter, removed afterwards."""
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation.openai import OpenAIInstrumentor
    for inst in (LangChainInstrumentor(), OpenAIInstrumentor()):
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
    tel, exp, _ = make(capture=False, instrument=True)
    yield tel, exp
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    for inst in (LangChainInstrumentor(), OpenAIInstrumentor(), HTTPXClientInstrumentor(),
                 SQLite3Instrumentor(), LoggingInstrumentor()):
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()


def test_langgraph_nodes_and_llm_calls_become_spans_without_content(instrumented):
    from typing import TypedDict

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph import END, START, StateGraph

    tel, exp = instrumented
    assert set(tel.instrumented) == {"langchain", "openai", "httpx", "sqlite3", "logging"}
    llm = FakeListChatModel(responses=[f"### OB01 Overview {SECRET} [D1 p.12]"])

    class S(TypedDict, total=False):
        text: str

    def draft_part(state):
        return {"text": llm.invoke(f"Draft from: {SECRET}").content}

    def check_part(state):
        return {"text": state["text"]}

    g = StateGraph(S)
    g.add_node("draft_part", draft_part)
    g.add_node("check_part", check_part)
    g.add_edge(START, "draft_part")
    g.add_edge("draft_part", "check_part")
    g.add_edge("check_part", END)
    out = g.compile().invoke({"text": ""})
    assert SECRET in out["text"]

    spans = exp.get_finished_spans()
    names = {s.name for s in spans}
    assert {"draft_part", "check_part"} <= names                 # each node is a span
    assert any(s.attributes.get("openinference.span.kind") == "LLM" for s in spans)
    assert SECRET not in " ".join(all_attr_values(spans))        # no document text left the process


# --- OTLP over HTTP (stand-in for Phoenix) ----------------------------------------------------

class _Receiver(http.server.BaseHTTPRequestHandler):
    status = 200
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append((self.path, dict(self.headers), body))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def receiver():
    _Receiver.received, _Receiver.status = [], 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Receiver)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, _Receiver
    srv.shutdown()


def test_check_connection_sends_a_real_otlp_span(receiver):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    srv, R = receiver
    s = settings(otel_exporter_otlp_endpoint=f"http://127.0.0.1:{srv.server_port}", phoenix_api_key="px-9")
    msg = T.check_connection(s)
    assert msg.startswith("OK")
    path, headers, body = R.received[0]
    assert path == "/v1/traces"
    assert {k.lower(): v for k, v in headers.items()}["authorization"] == "Bearer px-9"
    req = ExportTraceServiceRequest.FromString(body)
    spans = [sp for rs in req.resource_spans for ss in rs.scope_spans for sp in ss.spans]
    assert [sp.name for sp in spans] == ["telemetry.check"]
    res = {a.key: a.value.string_value for a in req.resource_spans[0].resource.attributes}
    assert res["openinference.project.name"] == "ipo-agent-test"


def test_check_connection_reports_a_rejected_span(receiver):
    srv, R = receiver
    R.status = 401                                     # wrong key: not retried, reported as an error
    s = settings(otel_exporter_otlp_endpoint=f"http://127.0.0.1:{srv.server_port}")
    with pytest.raises(RuntimeError, match="NOT accepted"):
        T.check_connection(s)


def test_check_connection_refuses_when_disabled():
    with pytest.raises(RuntimeError, match="switched off"):
        T.check_connection(settings(otel_enabled=False, otel_exporter_otlp_endpoint=None))


def test_langgraph_pause_is_not_an_error_and_its_payload_is_not_exported():
    from langgraph.errors import GraphInterrupt
    tel, exp, _ = make(capture=False)
    with pytest.raises(GraphInterrupt):
        with tel.span("node.checkpoint_facts"):
            raise GraphInterrupt(({"figures": SECRET},))
    (s,) = exp.get_finished_spans()
    assert s.attributes["paused"] is True and s.status.status_code == StatusCode.UNSET and not s.events


def test_scrubber_strips_pause_payload_recorded_by_other_instrumentation():
    tel, exp, _ = make(capture=False)
    with tel.tracer.start_as_current_span("checkpoint_facts") as sp:      # e.g. a LangChain chain span
        sp.add_event("exception", {"exception.type": "langgraph.errors.GraphInterrupt",
                                   "exception.message": SECRET, "exception.stacktrace": SECRET})
        sp.set_status(StatusCode.ERROR, SECRET)
    (s,) = exp.get_finished_spans()
    assert s.events[0].attributes == {"exception.type": "langgraph.errors.GraphInterrupt"}
    assert s.status.status_code == StatusCode.UNSET and SECRET not in str(s.status.description)


def test_real_errors_keep_their_message():
    tel, exp, _ = make(capture=False)
    with pytest.raises(RuntimeError):
        with tel.span("x"):
            raise RuntimeError("PDF is damaged")
    (s,) = exp.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR and "PDF is damaged" in s.status.description
    assert s.events[0].attributes["exception.message"] == "PDF is damaged"
