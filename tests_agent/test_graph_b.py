"""Step 5: graph part B with a mock chat model and a stub scorer.

Blueprint -> plan -> relevance gate -> parts in parallel (draft/check/evaluate/fix) -> assemble.
Done when: a mock run produces OB + RF drafts; the fix loop stops at MAX_FIX_ROUNDS; FAILED parts block export.
"""
import copy
import json
import os
import re
import threading
from typing import Any, Callable, List, Optional

import httpx
from pydantic import ValidationError
import openai
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import graph as G
from agent import telemetry as T
from agent.deps import AgentDeps
from agent.evals import FaithResult
from core import blueprint as bpmod
from core import context, replies, section_extractor, store
from tests_agent.mock_llm import MockChatModel
from tests_agent.test_graph_a import UNITS, _pdf, fake_process, sec, settings
from tests_agent.test_llm import GOOD_BP

S = settings()

OB_REPLY = ("### OB01 Overview\nWe are an asset management company [D1 p.5]. "
            "Our profit for the year was ₹2,54,015.32 lakhs [D1 p.145].\n\n### GAPS\n- Number of branches")
RF_REPLY = ("### RF01 [Internal] We are involved in outstanding legal proceedings\n"
            "There are outstanding legal proceedings against us [D1 p.6]. An adverse outcome could affect us.")
RF_OTHER = ("### RF01 [Internal] We depend on our key personnel\n"
            "Our business depends on our branches and our people [D1 p.6].")
RF_GAP = ("### RF02 [Internal] We are involved in outstanding legal proceedings\n"
          "There are outstanding legal proceedings against us [D1 p.6].")


def blueprint_json(ob_titles=("Overview",)) -> str:
    bp = copy.deepcopy(GOOD_BP)
    bp["our_business"]["subsections"] = [
        {"id": f"OB{i:02d}", "title": t, "purpose": t, "target_words": 400, "content_elements": [], "tables": [],
         "example_ref": "C1 p.2"} for i, t in enumerate(ob_titles, 1)]
    bp["risk_factors"]["themes"] = [{"id": "RF01", "group": "Internal Risks", "theme": "Outstanding legal proceedings",
                                     "heading_pattern": "We are involved in ...", "quantification": "",
                                     "example_ref": "C1 p.2"}]
    return json.dumps(bp)


class RoutedModel(BaseChatModel):
    """Answers by work item (parts run in parallel, so a reply list would be in random order)."""
    answer: Callable[[str], Any]
    seen: List[str] = []
    model_name: str = "mock-model"

    @property
    def _llm_type(self) -> str:
        return "mock-routed"

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        text = messages[-1].content
        item = re.search(r"Work item:?\s*\|?\s*([A-Za-z0-9-]+)", text).group(1)
        with _lock:
            self.seen.append(item)
        r = self.answer(item)
        if isinstance(r, Exception):
            raise r
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=r, usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}))])


_lock = threading.Lock()


class StubScorer:
    def __init__(self, relevance=None, faith=None):
        self.rel = relevance or {}
        self.faith = faith or {}
        self.calls = []

    def relevance(self, brief, contexts, *, on_usage=None):
        self.calls.append(("relevance", brief))
        return next((v for k, v in self.rel.items() if k in brief), 0.9)

    def faithfulness(self, brief, response, contexts, *, on_usage=None):
        self.calls.append(("faithfulness", response))
        f = next((v for k, v in self.faith.items() if k in response), 1.0)
        return FaithResult(score=f, statements=3, unsupported=[] if f >= 0.9 else ["The company has 9 offices - not on the page"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "deals")
    monkeypatch.setattr(context, "load_units", lambda d: UNITS)
    monkeypatch.setattr(context, "process", fake_process)
    found = {"C1": {"our_business": sec("C1", "our_business"), "risk_factors": sec("C1", "risk_factors")}}
    monkeypatch.setattr(section_extractor, "extract_sections", lambda path, doc_id, doc_name: found[doc_id])
    exp = InMemorySpanExporter()
    tel = T.setup_telemetry(S, span_exporter=exp, metric_reader=InMemoryMetricReader(), instrument=False,
                            set_global=False, batch=False)
    deal = store.create_deal("Test AMC", "Asset Management", "FY2025")
    docs = [{"path": _pdf(tmp_path / "ar.pdf"), "kind": "company", "doc_type": "ANNUAL_REPORT"},
            {"path": _pdf(tmp_path / "c1.pdf"), "kind": "comparable"}]

    class Env:
        def make(self, *, blueprint=None, draft=None, fix=None, scorer=None, settings_=S, stop=("checkpoint_review",)):
            self.bp_model = MockChatModel(replies=[blueprint or blueprint_json()], calls=[])
            answers = {"OB01": OB_REPLY, "RF-Internal": RF_REPLY}
            self.draft = RoutedModel(answer=draft or answers.get, seen=[])
            self.fix = RoutedModel(answer=fix or (lambda i: answers.get(i[4:]) if i.startswith("REV-") else None),
                                   seen=[])
            self.scorer = scorer or StubScorer()
            self.deps = AgentDeps(settings=settings_, tel=tel,
                                  models={"blueprint": self.bp_model, "draft": self.draft, "fix": self.fix},
                                  scorer=self.scorer)
            self.graph = G.build_graph(self.deps, checkpointer=InMemorySaver(), interrupt_before=stop)
            return self

        def to_drafting(self, run="r1"):
            snap = G.start(self.graph, deal, run, S, tel, docs)
            assert snap["waiting_for"] == "checkpoint_facts", snap
            return G.resume(self.graph, run, {"approved": True}, S, tel)

    e = Env()
    e.deal, e.tel, e.exp = deal, tel, exp
    return e


def rf_rev_answer(text):
    return lambda i: text if i.startswith("REV-RF") else None


# --------------------------------------------------------------------------- the happy path
def test_mock_run_produces_ob_and_rf_drafts(env):
    env.make()
    snap = env.to_drafting()
    assert snap["status"] == "drafted", snap["error"] or snap["log"]
    assert snap["export_blockers"] == [] and snap["pending"] is None
    assert {p["item_id"]: p["status"] for p in snap["parts"]} == {"OB01": "PASSED", "RF-Internal": "PASSED"}
    assert snap["item_status"]["our_business"]["OB01"]["status"] == "PASSED"
    assert snap["item_status"]["risk_factors"]["RF01"]["drafted_as"] == "RF01"
    ob = replies.load_draft(env.deal, "our_business")
    rf = replies.load_draft(env.deal, "risk_factors")
    assert [i.id for i in ob.items] == ["OB01"] and "₹2,54,015.32 lakhs [D1 p.145]" in ob.items[0].text
    assert ob.gaps == ["Number of branches"]
    assert [(i.id, i.group) for i in rf.items] == [("RF01", "Internal")]
    assert sorted(env.draft.seen) == ["OB01", "RF-Internal"] and env.fix.seen == []
    assert bpmod.load(env.deal).source.startswith("agent:")
    # what was sent: the selected page and the statement page, never a comparable as a source
    names = [s.name for s in env.exp.get_finished_spans()]
    for n in ("node.generate_blueprint", "node.plan_work", "node.relevance_gate", "node.draft_part",
              "node.check_part", "node.evaluate_part", "node.finish_part", "node.assemble_draft", "node.drafted"):
        assert n in names, n
    assert any("Draft ready for banker review" in line for line in snap["log"])


def test_ragas_usage_tracking_is_off():
    import agent.evals  # noqa: F401
    assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"


# --------------------------------------------------------------------------- fix loop
def test_fix_loop_stops_at_max_rounds_and_failed_part_blocks_export(env):
    env.make(scorer=StubScorer(faith={"profit for the year": 0.5}))
    snap = env.to_drafting()
    assert snap["status"] == "drafted"
    ob = next(p for p in snap["parts"] if p["item_id"] == "OB01")
    assert ob["status"] == "FAILED" and ob["rounds"] == S.max_fix_rounds == 2
    assert ob["unsupported"] == ["The company has 9 offices - not on the page"]
    assert env.fix.seen == ["REV-OB01", "REV-OB01"]                       # one revise call per round, then stop
    assert snap["export_blockers"] == ["OB01 FAILED the checks (0 high issue(s), faithfulness 0.5)"]
    parts_dir = store.deal_dir(env.deal) / "runs" / "r1" / "parts"
    assert sorted(f.name for f in parts_dir.iterdir() if f.name.startswith("OB01")) == [
        "OB01.md", "OB01.r0.md", "OB01.r1.md", "OB01.r2.md"]
    assert snap["item_status"]["our_business"]["OB01"]["status"] == "FAILED"    # kept and clearly marked


def test_fix_that_works_passes_after_one_round(env):
    fixed = "### OB01 Overview\nWe are an asset management company [D1 p.5]."
    env.make(scorer=StubScorer(faith={"profit for the year": 0.5}), fix=lambda i: fixed)
    snap = env.to_drafting()
    ob = next(p for p in snap["parts"] if p["item_id"] == "OB01")
    assert ob["status"] == "PASSED" and ob["rounds"] == 1 and snap["export_blockers"] == []


def test_comparable_cited_as_source_is_a_high_issue_and_gets_fixed(env):
    bad = "### OB01 Overview\nWe are an asset management company [C1 p.2]."
    env.make(draft=lambda i: bad if i == "OB01" else RF_REPLY,
             fix=lambda i: "### OB01 Overview\nWe are an asset management company [D1 p.5].")
    snap = env.to_drafting()
    assert env.fix.seen == ["REV-OB01"]
    assert next(p for p in snap["parts"] if p["item_id"] == "OB01")["status"] == "PASSED"


# --------------------------------------------------------------------------- errors: never a guess
def test_invalid_blueprint_stops_the_run_with_no_default(env):
    env.make(blueprint='{"our_business": {}}')
    snap = env.to_drafting()
    assert snap["status"] == "error" and snap["error"].startswith("generate_blueprint:")
    assert bpmod.load(env.deal) is None and env.draft.seen == []


def test_item_too_large_stops_the_run_naming_the_item(env):
    env.make(stop=["plan_work"])
    env.to_drafting()
    env.deps.settings = settings(max_input_tokens=50)            # blueprint already made; now the items are too big
    env.graph.invoke(None, G.run_config("r1", S))
    snap = G.snapshot(env.graph, "r1", S)
    assert snap["status"] == "error" and "OB01" in snap["error"] and "50" in snap["error"]
    assert env.draft.seen == []


def test_api_error_stops_the_run_and_no_other_model_is_tried(env):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    boom = openai.APIConnectionError(request=req)
    env.make(draft=lambda i: boom if i == "RF-Internal" else OB_REPLY)
    snap = env.to_drafting()
    assert snap["status"] == "error" and "draft_part" in snap["error"]
    assert replies.load_draft(env.deal, "risk_factors") is None                # nothing assembled
    assert env.fix.seen == [] and env.bp_model.calls and len(env.bp_model.calls) == 1


# --------------------------------------------------------------------------- banker: pages
def test_subsection_without_pages_goes_to_the_banker(env):
    env.make(blueprint=blueprint_json(("Overview", "Our Competitive Strengths")))
    snap = env.to_drafting()
    assert snap["waiting_for"] == "choose_pages"
    items = snap["pending"]["items"]
    assert [(i["item_id"], i["reason"]) for i in items] == [("OB02", "no pages selected")]
    assert snap["pending"]["reply_schema"]["title"] == "ChoosePagesReply"
    snap = G.resume(env.graph, "r1", {"exclude_subsections": ["OB02"]}, S, env.tel)
    assert snap["status"] == "drafted" and snap["export_blockers"] == []
    assert not any(s.include for s in bpmod.load(env.deal).our_business.subsections if s.id == "OB02")


def test_low_relevance_goes_to_the_banker_who_can_proceed(env):
    env.make(scorer=StubScorer(relevance={"Risk factors (Internal Risks)": 0.2}))
    snap = env.to_drafting()
    assert snap["waiting_for"] == "choose_pages"
    [it] = snap["pending"]["items"]
    assert it["item_id"] == "RF-Internal" and it["relevance"] == 0.2
    assert it["reason"] == "low relevance 0.20 (minimum 0.50)"
    with pytest.raises(ValidationError, match="proceed_anyway"):           # empty reply: rejected, nothing saved
        G.resume(env.graph, "r1", {}, S, env.tel)
    assert G.pending(env.graph, "r1", S)["kind"] == "choose_pages"
    snap = G.resume(env.graph, "r1", {"proceed_anyway": ["RF-Internal"]}, S, env.tel)
    assert snap["status"] == "drafted"
    rf = next(p for p in snap["parts"] if p["item_id"] == "RF-Internal")
    assert rf["relevance"] == 0.2 and rf["status"] == "PASSED"


# --------------------------------------------------------------------------- gap round
def test_missing_required_risk_gets_one_gap_round(env):
    env.make(draft=lambda i: {"OB01": OB_REPLY, "RF-Internal": RF_OTHER, "RF-Internal-GAP": RF_GAP}[i])
    snap = env.to_drafting()
    assert snap["status"] == "drafted", snap["error"]
    assert env.draft.seen.count("RF-Internal-GAP") == 1
    rf = replies.load_draft(env.deal, "risk_factors")
    assert [(i.id, i.title) for i in rf.items] == [("RF01", "We depend on our key personnel"),
                                                   ("RF02", "We are involved in outstanding legal proceedings")]
    assert snap["missing_required"] == [] and snap["export_blockers"] == []


def test_still_missing_after_gap_round_blocks_export(env):
    env.make(draft=lambda i: {"OB01": OB_REPLY, "RF-Internal": RF_OTHER,
                              "RF-Internal-GAP": RF_OTHER.replace("RF01", "RF02")}[i])
    snap = env.to_drafting()
    assert snap["status"] == "drafted"
    assert snap["missing_required"] == ["legal"]
    assert snap["export_blockers"] == ["Required risk 'legal' is not covered"]
