"""OpenTelemetry for the agent: traces (and optionally metrics) sent by OTLP/HTTP to Phoenix.

One call at app start:

    from agent.config import get_settings
    from agent.telemetry import setup_telemetry
    tel = setup_telemetry(get_settings())

then in agent code:

    with tel.span("draft_part", deal_id="d1", work_item_id="OB03") as sp:
        ...
        sp.set_attribute("tokens.in", 1234)
    tel.record_llm_call(call="draft", model="gpt-6-luna", tokens_in=..., tokens_out=..., latency_s=..., cost_usd=...)

Rules (see LANGGRAPH_PLAN.md §8):
- `OTEL_ENABLED=false` means telemetry is switched off on purpose: spans and metrics go nowhere. It is not a
  fallback; when it is `true` and the endpoint is wrong, the check command reports the failure.
- Document and prompt text never leaves the process unless `OTEL_CAPTURE_CONTENT=true`. Two guards:
  the OpenInference instrumentors are told to hide inputs/outputs, and the exporter strips any content
  attribute that still gets through.
- Endpoint and keys come from settings; the same code sends to a local Phoenix or Phoenix Cloud.

Check a connection from the command line (sends one span and reports whether Phoenix accepted it):

    python -m agent.telemetry --check
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (BatchSpanProcessor, SimpleSpanProcessor, SpanExporter,
                                            SpanExportResult)
from opentelemetry.trace import Span, Status, StatusCode

from agent.config import Settings

log = logging.getLogger(__name__)

SCOPE = "ipo-agent"

# Span attributes that can carry document or prompt text (OpenInference semantic conventions).
# Matched as exact names or as prefixes followed by ".".
CONTENT_ATTRIBUTE_PREFIXES = (
    "input.value",
    "output.value",
    "llm.input_messages",
    "llm.output_messages",
    "llm.prompts",
    "llm.prompt_template",
    "llm.choices",
    "llm.invocation_parameters",   # model settings; can include tool definitions or canned text
    "retrieval.documents",
    "reranker.input_documents",
    "reranker.output_documents",
    "embedding.embeddings",
    "message.content",
    "tool.parameters",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
)


# LangGraph signals a banker pause (interrupt) or a jump with an exception; it is not an error.
CONTROL_FLOW_EXCEPTIONS = ("GraphInterrupt", "GraphBubbleUp", "ParentCommand", "NodeInterrupt")


def is_control_flow(exc: Any) -> bool:
    """True for LangGraph's pause/jump exceptions (an instance, a class, or an exception.type string)."""
    if isinstance(exc, str):
        return exc.rsplit(".", 1)[-1] in CONTROL_FLOW_EXCEPTIONS
    cls = exc if isinstance(exc, type) else type(exc)
    return any(c.__name__ in CONTROL_FLOW_EXCEPTIONS for c in cls.__mro__)


def is_content_attribute(key: str) -> bool:
    return any(key == p or key.startswith(p + ".") for p in CONTENT_ATTRIBUTE_PREFIXES)


class ContentScrubbingExporter(SpanExporter):
    """Second guard: drops content attributes from every span (and span event) before export."""

    def __init__(self, inner: SpanExporter):
        self.inner = inner

    @staticmethod
    def _clean(attrs: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        return {k: v for k, v in (attrs or {}).items() if not is_content_attribute(k)}

    def _scrub(self, s: ReadableSpan) -> ReadableSpan:
        events, paused = [], False
        for ev in s.events:
            attrs = self._clean(ev.attributes)
            if ev.name == "exception" and is_control_flow(str(attrs.get("exception.type", ""))):
                # a LangGraph pause carries the banker payload (document figures) in its message
                attrs = {"exception.type": attrs["exception.type"]}
                paused = True
            events.append(type(ev)(ev.name, attrs, ev.timestamp))
        status = Status(StatusCode.UNSET) if paused and s.status.status_code == StatusCode.ERROR else s.status
        return ReadableSpan(
            name=s.name, context=s.context, parent=s.parent, resource=s.resource,
            attributes=self._clean(s.attributes), events=events, links=s.links, kind=s.kind,
            status=status, start_time=s.start_time, end_time=s.end_time,
            instrumentation_scope=s.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([self._scrub(s) for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def attr_name(keyword: str) -> str:
    head, sep, tail = keyword.rpartition("_")
    return f"{head}.{tail}" if sep else keyword


def otlp_headers(settings: Settings) -> Dict[str, str]:
    """Auth header for Phoenix Cloud (or a self-run Phoenix with auth on). Empty when no key is set."""
    key = settings.phoenix_api_key.get_secret_value().strip() if settings.phoenix_api_key else ""
    return {"authorization": f"Bearer {key}"} if key else {}


def traces_url(settings: Settings) -> str:
    base = (settings.otel_exporter_otlp_endpoint or "").strip().rstrip("/")
    return base if base.endswith("/v1/traces") else base + "/v1/traces"


def metrics_url(settings: Settings) -> str:
    base = (settings.otel_exporter_otlp_endpoint or "").strip().rstrip("/")
    if base.endswith("/v1/traces"):
        base = base[: -len("/v1/traces")]
    return base + "/v1/metrics"


def make_span_exporter(settings: Settings) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter(endpoint=traces_url(settings), headers=otlp_headers(settings), timeout=10)


def make_resource(settings: Settings) -> Resource:
    return Resource.create({
        "service.name": settings.otel_service_name,
        "deployment.environment": settings.deployment_environment,
        "openinference.project.name": settings.otel_service_name,   # Phoenix groups traces by this
    })


# ---------------------------------------------------------------------------------------------
# custom metrics (LANGGRAPH_PLAN.md §8.1)

@dataclass
class Instruments:
    run_duration: Any
    node_duration: Any
    llm_latency: Any
    llm_tokens: Any
    llm_cost: Any
    part_status: Any
    fix_rounds: Any
    ragas_faithfulness: Any
    ragas_context_relevance: Any
    run_errors: Any
    interrupts_waiting: Any


def _instruments(meter: metrics.Meter) -> Instruments:
    return Instruments(
        run_duration=meter.create_histogram("ipo.run.duration", unit="s",
                                            description="End-to-end run time, excluding banker wait"),
        node_duration=meter.create_histogram("ipo.node.duration", unit="s", description="Time per graph node"),
        llm_latency=meter.create_histogram("ipo.llm.latency", unit="s", description="OpenAI response time"),
        llm_tokens=meter.create_counter("ipo.llm.tokens", unit="{token}", description="Tokens used"),
        llm_cost=meter.create_counter("ipo.llm.cost_usd", unit="USD", description="LLM spend"),
        part_status=meter.create_counter("ipo.part.status", description="PASSED / FAILED parts"),
        fix_rounds=meter.create_histogram("ipo.fix.rounds", description="Fix rounds used per part"),
        ragas_faithfulness=meter.create_histogram("ipo.ragas.faithfulness", description="Ragas faithfulness"),
        ragas_context_relevance=meter.create_histogram("ipo.ragas.context_relevance",
                                                       description="Ragas context relevance"),
        run_errors=meter.create_counter("ipo.runs.errors", description="Run failures by cause"),
        interrupts_waiting=meter.create_up_down_counter("ipo.interrupts.waiting",
                                                        description="Runs waiting on a banker"),
    )


# ---------------------------------------------------------------------------------------------

@dataclass
class Telemetry:
    enabled: bool
    capture_content: bool
    tracer: trace.Tracer
    meter: metrics.Meter
    instruments: Instruments
    tracer_provider: Optional[TracerProvider] = None
    meter_provider: Optional[MeterProvider] = None
    instrumented: List[str] = field(default_factory=list)

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        """A span with our attributes. The last `_` in a keyword becomes `.`
        (deal_id -> deal.id, work_item_id -> work_item.id, tokens_in -> tokens.in).
        Exceptions are recorded on the span, the status set to ERROR, and re-raised.
        A LangGraph pause (interrupt) is not an error: the span is only marked `paused`."""
        attrs = {attr_name(k): v for k, v in attributes.items() if v is not None}
        with self.tracer.start_as_current_span(name, attributes=attrs, record_exception=False,
                                               set_status_on_exception=False) as sp:
            try:
                yield sp
            except BaseException as e:
                if is_control_flow(e):
                    sp.set_attribute("paused", True)
                else:
                    sp.record_exception(e)
                    sp.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"))
                raise

    @contextlib.contextmanager
    def node(self, node: str, **attributes: Any) -> Iterator[Span]:
        """Span + duration metric for one graph node."""
        t0 = time.perf_counter()
        try:
            with self.span(f"node.{node}", node=node, **attributes) as sp:
                yield sp
        finally:
            self.instruments.node_duration.record(time.perf_counter() - t0, {"node": node})

    def record_llm_call(self, *, call: str, model: str, tokens_in: int, tokens_out: int,
                        latency_s: float, cost_usd: Optional[float]) -> None:
        """cost_usd=None means the price is not known: no cost is recorded (never a guessed one)."""
        attrs = {"call": call, "model": model}
        self.instruments.llm_latency.record(latency_s, attrs)
        self.instruments.llm_tokens.add(tokens_in, {**attrs, "direction": "in"})
        self.instruments.llm_tokens.add(tokens_out, {**attrs, "direction": "out"})
        if cost_usd is not None:
            self.instruments.llm_cost.add(cost_usd, attrs)
        sp = trace.get_current_span()
        if sp.is_recording():
            sp.set_attributes({"llm.call": call, "llm.model": model, "tokens.in": tokens_in,
                               "tokens.out": tokens_out})
            if cost_usd is not None:
                sp.set_attribute("cost.usd", cost_usd)

    def record_part(self, *, section: str, status: str, fix_rounds: int,
                    faithfulness: Optional[float] = None) -> None:
        self.instruments.part_status.add(1, {"section": section, "status": status})
        self.instruments.fix_rounds.record(fix_rounds)
        if faithfulness is not None:
            self.instruments.ragas_faithfulness.record(faithfulness, {"section": section})

    def record_relevance(self, score: float, section: str) -> None:
        self.instruments.ragas_context_relevance.record(score, {"section": section})

    def record_run_error(self, reason: str) -> None:
        self.instruments.run_errors.add(1, {"reason": reason})

    def record_run_duration(self, seconds: float) -> None:
        self.instruments.run_duration.record(seconds)

    def force_flush(self, timeout_millis: int = 10000) -> bool:
        ok = True
        if self.tracer_provider is not None:
            ok = self.tracer_provider.force_flush(timeout_millis) and ok
        if self.meter_provider is not None:
            ok = self.meter_provider.force_flush(timeout_millis) and ok
        return ok

    def shutdown(self) -> None:
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()


def _disabled() -> Telemetry:
    tracer = trace.NoOpTracer()
    meter = metrics.NoOpMeter(SCOPE)
    return Telemetry(enabled=False, capture_content=False, tracer=tracer, meter=meter,
                     instruments=_instruments(meter))


def _instrument_libraries(tp: TracerProvider, capture_content: bool) -> List[str]:
    """Auto-instrument LangChain/LangGraph, OpenAI, httpx, sqlite3 and logging (trace id in log lines)."""
    from openinference.instrumentation import TraceConfig
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor

    hide = not capture_content
    cfg = TraceConfig(hide_inputs=hide, hide_outputs=hide, hide_input_messages=hide,
                      hide_output_messages=hide, hide_input_text=hide, hide_output_text=hide,
                      hide_input_images=hide, hide_prompts=hide, hide_choices=hide,
                      hide_retrieval_documents=hide, hide_embeddings_text=hide, hide_llm_tools=hide,
                      hide_llm_invocation_parameters=hide)
    done = []
    for name, inst, kwargs in (
        ("langchain", LangChainInstrumentor(), {"config": cfg}),
        ("openai", OpenAIInstrumentor(), {"config": cfg}),
        ("httpx", HTTPXClientInstrumentor(), {}),
        ("sqlite3", SQLite3Instrumentor(), {}),
        ("logging", LoggingInstrumentor(), {"set_logging_format": False}),
    ):
        if not inst.is_instrumented_by_opentelemetry:
            inst.instrument(tracer_provider=tp, **kwargs)
        done.append(name)
    return done


_lock = threading.Lock()
_current: Optional[Telemetry] = None


def setup_telemetry(settings: Settings, *, span_exporter: Optional[SpanExporter] = None,
                    metric_reader: Optional[MetricReader] = None, instrument: bool = True,
                    set_global: bool = True, batch: bool = True) -> Telemetry:
    """Build telemetry from settings. With `set_global` (the app), it is done once per process
    and later calls return the same object. Tests pass their own exporter/reader and set_global=False."""
    global _current
    with _lock:
        if set_global and _current is not None:
            return _current
        if not settings.otel_enabled:
            tel = _disabled()
        else:
            resource = make_resource(settings)
            tp = TracerProvider(resource=resource)
            exporter = span_exporter if span_exporter is not None else make_span_exporter(settings)
            if not settings.otel_capture_content:
                exporter = ContentScrubbingExporter(exporter)
            tp.add_span_processor(BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter))

            readers: List[MetricReader] = []
            if metric_reader is not None:
                readers.append(metric_reader)
            elif settings.otel_metrics_exporter == "otlp":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                readers.append(PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=metrics_url(settings), headers=otlp_headers(settings), timeout=10)))
            mp = MeterProvider(resource=resource, metric_readers=readers)

            if set_global:
                trace.set_tracer_provider(tp)
                metrics.set_meter_provider(mp)
            meter = mp.get_meter(SCOPE)
            tel = Telemetry(enabled=True, capture_content=settings.otel_capture_content,
                            tracer=tp.get_tracer(SCOPE), meter=meter, instruments=_instruments(meter),
                            tracer_provider=tp, meter_provider=mp)
            if instrument:
                tel.instrumented = _instrument_libraries(tp, settings.otel_capture_content)
        if set_global:
            _current = tel
        return tel


def get_telemetry() -> Telemetry:
    """The telemetry set up for this process. Errors if setup_telemetry was never called."""
    if _current is None:
        raise RuntimeError("Telemetry is not set up: call agent.telemetry.setup_telemetry(settings) at start-up.")
    return _current


# ---------------------------------------------------------------------------------------------
# connection check

class _ResultRecorder(SpanExporter):
    def __init__(self, inner: SpanExporter):
        self.inner = inner
        self.results: List[SpanExportResult] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        r = self.inner.export(spans)
        self.results.append(r)
        return r

    def shutdown(self) -> None:
        self.inner.shutdown()


def check_connection(settings: Settings, *, span_exporter: Optional[SpanExporter] = None) -> str:
    """Send one span named `telemetry.check` and return a message. Raises RuntimeError if the
    endpoint did not accept it (wrong URL, wrong key, Phoenix not running)."""
    if not settings.otel_enabled:
        raise RuntimeError("OTEL_ENABLED=false: telemetry is switched off, nothing to check.")
    recorder = _ResultRecorder(span_exporter if span_exporter is not None else make_span_exporter(settings))
    tel = setup_telemetry(settings, span_exporter=recorder, metric_reader=None, instrument=False,
                          set_global=False, batch=False)
    try:
        with tel.span("telemetry.check", deployment_environment=settings.deployment_environment) as sp:
            sp.set_attribute("check.time", time.strftime("%Y-%m-%dT%H:%M:%S"))
    finally:
        tel.shutdown()
    if recorder.results and all(r == SpanExportResult.SUCCESS for r in recorder.results):
        return (f"OK: span 'telemetry.check' accepted by {traces_url(settings)} "
                f"(project '{settings.otel_service_name}').")
    raise RuntimeError(f"Span NOT accepted by {traces_url(settings)}. Check that Phoenix is running, "
                       "the endpoint is right, and PHOENIX_API_KEY is set for Phoenix Cloud.")


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    from agent.config import ConfigError, load_settings

    ap = argparse.ArgumentParser(prog="python -m agent.telemetry")
    ap.add_argument("--check", action="store_true", help="send one test span and report the result")
    args = ap.parse_args(argv)
    if not args.check:
        ap.print_help()
        return 2
    logging.basicConfig(level=logging.WARNING)
    try:
        print(check_connection(load_settings()))
        return 0
    except (ConfigError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
