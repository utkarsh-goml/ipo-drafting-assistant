"""Step 4: routing conditions and graph part A (ingest -> extract -> review -> context -> checkpoint 1)."""
import os
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from agent import graph as G
from agent import routing as R
from agent import telemetry as T
from agent.config import Settings
from agent.deps import AgentDeps
from agent.state import FactsReply, merge_extraction
from core import context, section_extractor, store
from core.company_docs import Unit
from core.financials import Figure, Indicators
from core.models import ExtractedSection, PageText
from core.risk_triggers import Evidence, Trigger


def settings(**kw):
    vals = dict(openai_api_key="sk-test", openai_model_blueprint="m", openai_model_draft="m", openai_model_eval="m",
                openai_tokenizer="o200k_base", openai_timeout_s=30, openai_temperature="0", max_fix_rounds=2,
                max_gap_rounds=1, max_input_tokens=200000, max_concurrency=2, faithfulness_min=0.9,
                relevance_min=0.5, otel_enabled=True, otel_exporter_otlp_endpoint="http://localhost:6006",
                otel_service_name="t", deployment_environment="test", otel_metrics_exporter="none",
                otel_capture_content=False)
    vals.update(kw)
    return Settings(_env_file=None, **vals)


S = settings()
STOP = ["generate_blueprint"]          # part A tests stop where part B begins


def deps(tel):
    return AgentDeps(settings=S, tel=tel, models={}, scorer=None)


# --------------------------------------------------------------------------- routing (one test per condition)
def flag(doc="C1", sec="our_business", **kw):
    return {"doc_id": doc, "section": sec, "accepted": True, "found": True, "method": "toc", **kw}


def test_route_after_ingest_fans_out_per_comparable():
    sends = R.route_after_ingest({"deal_id": "d", "docs": [{"doc_id": "D1", "kind": "company"},
                                                           {"doc_id": "C1", "kind": "comparable"},
                                                           {"doc_id": "C2", "kind": "comparable"}]})
    assert [(s.node, s.arg["doc_id"]) for s in sends] == [("extract_comparable", "C1"), ("extract_comparable", "C2")]
    assert R.route_after_ingest({"error": "x"}) == END


@pytest.mark.parametrize("flags,expected", [
    ([flag(), flag(sec="risk_factors")], "check_style_coverage"),
    ([flag(), flag(sec="risk_factors", accepted=False, method="heading_scan")], "review_extraction"),
    ([flag(), flag(sec="risk_factors", accepted=False, found=False)], "review_extraction"),
    ([flag(), flag(sec="risk_factors", accepted=False, banker_decision="confirm")], "check_style_coverage"),
])
def test_route_after_extraction(flags, expected):
    assert R.route_after_extraction({"extraction": flags}) == expected


def test_error_always_ends():
    for fn in (R.route_after_extraction, R.route_after_style_check, R.route_after_context, R.route_after_facts):
        assert fn({"error": "boom", "extraction": []}) == END


def test_route_after_facts_and_style_check():
    assert R.route_after_facts({"facts_approved": True}) == "facts_approved"
    assert R.route_after_facts({"facts_approved": False}) == "checkpoint_facts"
    assert R.route_after_style_check({}) == "build_company_context"
    assert R.route_after_style_check({"context_built": True}) == "checkpoint_facts"


def test_extraction_reducer_merges_by_doc_and_section():
    a = [flag(), flag(sec="risk_factors", accepted=False)]
    b = [flag(sec="risk_factors", accepted=False, banker_decision="confirm")]
    out = merge_extraction(a, b)
    assert len(out) == 2 and out[1]["banker_decision"] == "confirm"


def test_facts_reply_rules():
    with pytest.raises(ValidationError, match="approve without edits"):
        FactsReply(approved=True, trigger_selection={"x": False})
    with pytest.raises(ValidationError, match="send edits, or approve"):
        FactsReply(approved=False)


# --------------------------------------------------------------------------- graph on a temp deal
UNITS = [Unit(doc_id="D1", unit_no=5, pdf_page=5, printed_page=5, lines=["We are an AMC."]),
         Unit(doc_id="D1", unit_no=6, pdf_page=6, printed_page=6, lines=["Our branches."]),
         Unit(doc_id="D1", unit_no=145, pdf_page=145, printed_page=145,
              lines=["Profit for the year 2,54,015.32 2,07,278.35"])]


def _pdf(path: Path, n=3):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i in range(n):
        c.drawString(72, 720, f"page {i + 1}")
        c.showPage()
    c.save()
    return str(path)


def fake_process(deal_id):
    ind = Indicators(basis="consolidated", unit="lakhs", years=["FY2025", "FY2024"], figures={
        "pat": Figure(key="pat", label="Profit for the year (PAT)", values=[254015.32, 207278.35],
                      cite="D1 p.145", source_line="Profit for the year")})
    trig = [Trigger(key="legal", title="Outstanding legal proceedings", category="Internal", mandatory=True,
                    reason="litigation", evidence=[Evidence(cite="D1 p.6", location="PDF page 6", snippet="x")])]
    mapping = {"sector_pack": None,
               "our_business": {"overview": {"title": "Overview", "hits": [
                   {"cite": "D1 p.5", "doc_id": "D1", "unit_no": 5, "score": 1.0, "selected": True},
                   {"cite": "D1 p.6", "doc_id": "D1", "unit_no": 6, "score": 0.2, "selected": False}]}},
               "risk_themes": {}}
    context.save_indicators(deal_id, ind)
    context.save_triggers(deal_id, trig)
    context.save_mapping(deal_id, mapping)
    return {"units": len(UNITS), "indicators": ind, "triggers": trig, "mapping": mapping}


def sec(doc, key, method="toc", sv=True, ev=True):
    return ExtractedSection(doc_id=doc, doc_name=f"{doc}.pdf", section_key=key, section_title=key.replace("_", " ").title(),
                            start_page=2, start_line=1, end_page=3, end_line=1, method=method, start_verified=sv,
                            end_verified=ev, confidence=0.9, pages=[PageText(page_no=2, lines=["x"])])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "deals")
    monkeypatch.setattr(context, "load_units", lambda d: UNITS)
    monkeypatch.setattr(context, "process", fake_process)
    found = {"C1": {"our_business": sec("C1", "our_business"), "risk_factors": sec("C1", "risk_factors")}}
    monkeypatch.setattr(section_extractor, "extract_sections",
                        lambda path, doc_id, doc_name: found[doc_id] if not isinstance(found[doc_id], Exception)
                        else (_ for _ in ()).throw(found[doc_id]))
    monkeypatch.setattr(section_extractor, "manual_section",
                        lambda path, doc_id, doc_name, section_key, start_page, end_page: sec(doc_id, section_key, "manual"))
    exp = InMemorySpanExporter()
    tel = T.setup_telemetry(S, span_exporter=exp, metric_reader=InMemoryMetricReader(), instrument=False,
                            set_global=False, batch=False)
    deal = store.create_deal("Test AMC", "Asset Management", "FY2025")
    docs = [{"path": _pdf(tmp_path / "ar.pdf"), "kind": "company", "doc_type": "ANNUAL_REPORT"},
            {"path": _pdf(tmp_path / "c1.pdf"), "kind": "comparable"}]
    g = G.build_graph(deps(tel), checkpointer=InMemorySaver(), interrupt_before=STOP)

    class Env:
        pass
    e = Env()
    e.deal, e.docs, e.graph, e.found, e.exp, e.tmp, e.tel = deal, docs, g, found, exp, tmp_path, tel
    return e


def test_clean_run_pauses_at_checkpoint_1_then_approves(env):
    snap = G.start(env.graph, env.deal, "r1", S, env.tel, env.docs)
    assert snap["status"] == "waiting_banker" and snap["waiting_for"] == "checkpoint_facts"
    p = snap["pending"]
    assert p["basis"] == "consolidated" and p["unit"] == "lakhs"
    assert {"key": "pat", "label": "Profit for the year (PAT)", "values": [254015.32, 207278.35],
            "cite": "D1 p.145"} in p["figures"]
    assert p["pages"]["our_business"]["overview"]["selected"] == ["D1 p.5"]
    assert p["blockers"] == [] and p["reply_schema"]["title"] == "FactsReply"
    assert any("PAT 2,54,015.32 [D1 p.145]" in line for line in snap["log"])
    done = G.resume(env.graph, "r1", {"approved": True}, S, env.tel)
    assert done["status"] == "facts_approved" and done["pending"] is None
    names = {s.name for s in env.exp.get_finished_spans()}
    assert {"node.ingest_documents", "node.extract_comparable", "node.build_company_context",
            "node.checkpoint_facts", "node.facts_approved"} <= names
    assert all(s.attributes.get("deal.id") == env.deal for s in env.exp.get_finished_spans())
    with pytest.raises(ValueError, match="not waiting"):
        G.resume(env.graph, "r1", {"approved": True}, S, env.tel)
    with pytest.raises(ValueError, match="already exists"):
        G.start(env.graph, env.deal, "r1", S, env.tel)


def test_fallback_extraction_waits_for_banker(env):
    env.found["C1"]["risk_factors"] = sec("C1", "risk_factors", method="heading_scan", ev=False)
    snap = G.start(env.graph, env.deal, "r1", S, env.tel, env.docs)
    assert snap["waiting_for"] == "review_extraction"
    (item,) = snap["pending"]["sections"]
    assert item["section"] == "risk_factors" and "heading_scan instead of the table of contents" in item["reason"]
    assert "end not confirmed" in item["reason"]
    # an invalid reply changes nothing; the banker can answer again
    with pytest.raises(ValidationError, match="set_range needs start_page and end_page"):
        G.resume(env.graph, "r1", {"decisions": [{"doc_id": "C1", "section": "risk_factors", "action": "set_range"}]}, S, env.tel)
    assert G.pending(env.graph, "r1", S)["kind"] == "review_extraction"
    # a decision about a section that isn't under review is not applied, and the pause repeats
    snap = G.resume(env.graph, "r1", {"decisions": [{"doc_id": "C1", "section": "our_business", "action": "confirm"}]}, S, env.tel)
    assert snap["waiting_for"] == "review_extraction"
    assert "not waiting for review" in snap["pending"]["notice"]
    snap = G.resume(env.graph, "r1", {"decisions": [{"doc_id": "C1", "section": "risk_factors", "action": "set_range",
                                                     "start_page": 2, "end_page": 3}]}, S, env.tel)
    assert snap["waiting_for"] == "checkpoint_facts"
    rf = next(f for f in snap["extraction"] if f["section"] == "risk_factors")
    assert rf["method"] == "manual" and rf["banker_decision"] == "set_range"


def test_missing_section_cannot_be_confirmed_and_dropping_the_only_one_errors(env):
    env.found["C1"]["risk_factors"] = None
    snap = G.start(env.graph, env.deal, "r1", S, env.tel, env.docs)
    assert snap["pending"]["sections"][0]["reason"] == "not found in the document"
    snap = G.resume(env.graph, "r1", {"decisions": [{"doc_id": "C1", "section": "risk_factors", "action": "confirm"}]}, S, env.tel)
    assert "can't be confirmed" in snap["pending"]["notice"]
    snap = G.resume(env.graph, "r1", {"decisions": [{"doc_id": "C1", "section": "risk_factors", "action": "drop"}]}, S, env.tel)
    assert snap["status"] == "error" and "no usable comparable section for Risk Factors" in snap["error"]


def test_checkpoint_edits_loop_then_approve(env):
    G.start(env.graph, env.deal, "r1", S, env.tel, env.docs)
    snap = G.resume(env.graph, "r1", {"approved": False,
                                      "figure_edits": [{"key": "pat", "label": "Profit for the year (PAT)",
                                                        "current": 254015.32, "previous": 207000.0}],
                                      "trigger_selection": {"legal": False, "nope": True},
                                      "page_selection": [{"kind": "our_business", "key": "overview",
                                                          "cite": "D1 p.6", "selected": True}]}, S, env.tel)
    p = snap["pending"]
    assert snap["waiting_for"] == "checkpoint_facts"
    assert next(f for f in p["figures"] if f["key"] == "pat")["values"] == [254015.32, 207000.0]
    assert next(f for f in p["figures"] if f["key"] == "pat")["cite"] == "D1 p.145"     # edited value keeps its cite
    assert p["triggers"][0]["selected"] is False
    assert p["pages"]["our_business"]["overview"]["selected"] == ["D1 p.5", "D1 p.6"]
    assert "unknown trigger 'nope'" in p["notice"]
    with pytest.raises(ValidationError, match="approve without edits"):
        G.resume(env.graph, "r1", {"approved": True, "trigger_selection": {"legal": True}}, S, env.tel)
    assert G.resume(env.graph, "r1", {"approved": True}, S, env.tel)["status"] == "facts_approved"


def test_approval_refused_while_blocked(env):
    G.start(env.graph, env.deal, "r1", S, env.tel, env.docs)
    snap = G.resume(env.graph, "r1", {"approved": False, "page_selection": [
        {"kind": "our_business", "key": "overview", "cite": "D1 p.5", "selected": False}]}, S, env.tel)
    assert "No pages are selected for Overview" in snap["pending"]["blockers"][0]
    snap = G.resume(env.graph, "r1", {"approved": True}, S, env.tel)
    assert snap["waiting_for"] == "checkpoint_facts" and snap["pending"]["notice"].startswith("Approval refused")


def test_no_comparable_is_an_error(env):
    snap = G.start(env.graph, env.deal, "r1", S, env.tel, env.docs[:1])
    assert snap["status"] == "error" and "no comparable" in snap["error"] and snap["pending"] is None


def test_failure_in_one_parallel_extraction_stops_the_run(env, tmp_path):
    env.found["C2"] = RuntimeError("PDF is damaged")
    docs = env.docs + [{"path": _pdf(tmp_path / "c2.pdf"), "kind": "comparable"}]
    snap = G.start(env.graph, env.deal, "r1", S, env.tel, docs)
    assert snap["status"] == "error" and snap["error"] == "extract_comparable: RuntimeError: PDF is damaged"
    assert snap["pending"] is None
    err = [s for s in env.exp.get_finished_spans() if s.status.status_code.name == "ERROR"]
    assert [s.name for s in err] == ["node.extract_comparable"]


def test_run_state_survives_in_the_deal_folder(env):
    tel = T.setup_telemetry(settings(otel_enabled=False, otel_exporter_otlp_endpoint=None), set_global=False)
    with G.deal_graph(env.deal, deps(tel), STOP) as g:
        assert G.start(g, env.deal, "r1", S, tel, env.docs)["waiting_for"] == "checkpoint_facts"
    assert (store.deal_dir(env.deal) / "runs.sqlite").exists()
    with G.deal_graph(env.deal, deps(tel), STOP) as g:     # e.g. after a Streamlit rerun or an app restart
        assert G.pending(g, "r1", S)["kind"] == "checkpoint_facts"
        assert G.resume(g, "r1", {"approved": True}, S, tel)["status"] == "facts_approved"
    with pytest.raises(ValueError, match="Unknown deal"):
        with G.deal_graph("nope", deps(tel)):
            pass


# --------------------------------------------------------------------------- real documents
SAMPLE_AR = os.environ.get("IPO_SAMPLE_AR")
SAMPLE_DRHP = os.environ.get("IPO_SAMPLE_DRHP")


@pytest.mark.skipif(not (SAMPLE_AR and SAMPLE_DRHP and Path(SAMPLE_AR).exists() and Path(SAMPLE_DRHP).exists()),
                    reason="IPO_SAMPLE_AR and IPO_SAMPLE_DRHP not set")
def test_part_a_on_real_documents(tmp_path, monkeypatch):
    """SBI Funds AR 2024-25 + ICICI Pru AMC DRHP: pauses at checkpoint 1 with consolidated PAT and its page."""
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    tel = T.setup_telemetry(settings(otel_enabled=False, otel_exporter_otlp_endpoint=None), set_global=False)
    deal = store.create_deal("SBI Funds Test", "Asset Management", "FY2025")
    docs = [{"path": SAMPLE_AR, "kind": "company", "doc_type": "ANNUAL_REPORT"},
            {"path": SAMPLE_DRHP, "kind": "comparable"}]
    with G.deal_graph(deal, deps(tel), STOP) as g:
        snap = G.start(g, deal, "real-1", S, tel, docs)
        assert snap["status"] == "waiting_banker" and snap["waiting_for"] == "checkpoint_facts", snap
        p = snap["pending"]
        pat = next(f for f in p["figures"] if f["key"] == "pat")
        assert p["basis"] == "consolidated" and p["unit"] == "lakhs"
        assert pat["values"][0] == 254015.32 and pat["cite"] == "D1 p.145"
        assert {(f["section"], tuple(f["pdf_pages"])) for f in snap["extraction"]} == {
            ("our_business", (160, 184)), ("risk_factors", (35, 65))}
        assert p["blockers"] == []
        assert G.resume(g, "real-1", {"approved": True}, S, tel)["status"] == "facts_approved"


# --------------------------------------------------------------------------- traces of a real pause
@pytest.fixture
def instrumented_graph(env):
    """Same deal, but a graph whose telemetry has LangChain/OpenAI/sqlite auto-instrumentation switched on."""
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    insts = (LangChainInstrumentor(), OpenAIInstrumentor(), HTTPXClientInstrumentor(), SQLite3Instrumentor(),
             LoggingInstrumentor())
    for i in insts:
        if i.is_instrumented_by_opentelemetry:
            i.uninstrument()
    exp = InMemorySpanExporter()
    tel = T.setup_telemetry(S, span_exporter=exp, metric_reader=InMemoryMetricReader(), instrument=True,
                            set_global=False, batch=False)
    yield G.build_graph(deps(tel), checkpointer=InMemorySaver(), interrupt_before=STOP), tel, exp
    for i in insts:
        if i.is_instrumented_by_opentelemetry:
            i.uninstrument()


def _values(spans):
    out = []
    for s in spans:
        out += [str(v) for v in (s.attributes or {}).values()] + [str(s.status.description or "")]
        for ev in s.events:
            out += [str(v) for v in (ev.attributes or {}).values()]
    return " ".join(out)


def test_pause_is_one_trace_with_no_document_content(env, instrumented_graph):
    g, tel, exp = instrumented_graph
    G.start(g, env.deal, "r1", S, tel, env.docs)
    spans = exp.get_finished_spans()
    root = next(s for s in spans if s.name == "action.start")
    assert root.parent is None and root.attributes["waiting.for"] == "checkpoint_facts"
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}        # every step nested in one trace
    assert any(s.name == "checkpoint_facts" for s in spans)                      # LangGraph node span (auto)
    assert next(s for s in spans if s.name == "node.checkpoint_facts").attributes["paused"] is True
    assert not [s.name for s in spans if s.status.status_code.name == "ERROR"]   # a pause is not an error
    text = _values(spans)
    for secret in ("254015.32", "2,54,015.32", "Outstanding legal proceedings", "Profit for the year"):
        assert secret not in text, secret
    exp.clear()
    G.resume(g, "r1", {"approved": True}, S, tel)
    spans = exp.get_finished_spans()
    assert len({s.context.trace_id for s in spans}) == 1
    assert next(s for s in spans if s.name == "action.resume").attributes["run.status"] == "facts_approved"
