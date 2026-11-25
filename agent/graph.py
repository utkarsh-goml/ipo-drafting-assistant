"""Builds the agent graph and runs it.

Part A (facts):
    START -> ingest_documents -> extract_comparable (one task per comparable, in parallel)
          -> check_extraction -> [review_extraction (banker) ...] -> check_style_coverage
          -> build_company_context -> checkpoint_facts (banker; edits loop back) -> facts_approved
Part B (drafting):
    -> generate_blueprint -> plan_work -> [choose_pages (banker) -> plan_work ...] -> relevance_gate
    -> part (one per work item, in parallel; subgraph draft -> check -> evaluate -> [fix -> check ...] -> finish)
    -> assemble_draft -> [plan_work for a gap round ...] -> drafted
    -> checkpoint_review (banker) -> [apply_review (revise / edit / add ...) -> checkpoint_review ...] -> export -> END

Runs are saved by a checkpointer: `deal_graph(deal_id, deps)` keeps them in the deal folder
(`data/deals/<deal>/runs.sqlite`), so a deal's files and its run state are kept and lost together.

    with deal_graph(deal_id, deps) as g:
        start(g, deal_id, run_id, settings, tel)          # runs until the first banker pause
        pending(g, run_id, settings)                     # {"kind": "checkpoint_facts", ...} or None
        resume(g, run_id, {"approved": True}, settings, tel)
"""
from __future__ import annotations

import contextlib
import functools
import sqlite3
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from agent import routing
from agent.config import Settings
from agent.deps import AgentDeps
from agent.nodes import drafting as D
from agent.nodes import facts as F
from agent.nodes import review as RV
from agent.nodes.common import guarded
from agent.state import DealState, PartOutput, PartState
from agent.telemetry import Telemetry
from core import store

RUNS_DB = "runs.sqlite"


def _check_extraction(state: DealState) -> dict:
    """Join point after the parallel extraction tasks (one routing decision for all of them)."""
    return {}


def _with(fn, deps: AgentDeps):
    return functools.wraps(fn)(functools.partial(fn, deps=deps))


def build_part_graph(deps: AgentDeps) -> Any:
    """Subgraph for one work item: draft -> check -> evaluate -> (fix -> check -> evaluate)* -> finish."""
    g = StateGraph(PartState, output_schema=PartOutput)
    for name, fn in (("draft_part", D.draft_part), ("check_part", D.check_part), ("evaluate_part", D.evaluate_part),
                     ("fix_part", D.fix_part), ("finish_part", D.finish_part)):
        g.add_node(name, guarded(name, _with(fn, deps), deps.tel))

    def after_evaluate(state: dict) -> str:
        return routing.route_part(state, max_fix_rounds=deps.settings.max_fix_rounds,
                                  passed=not state.get("error") and D.part_passed(state, deps))

    def ok_or_finish(nxt: str):
        def route(state: dict) -> str:
            return "finish_part" if state.get("error") else nxt
        route.__name__ = f"to_{nxt}"
        return route

    g.add_edge(START, "draft_part")
    g.add_conditional_edges("draft_part", ok_or_finish("check_part"), ["check_part", "finish_part"])
    g.add_conditional_edges("check_part", ok_or_finish("evaluate_part"), ["evaluate_part", "finish_part"])
    g.add_conditional_edges("evaluate_part", after_evaluate, ["fix_part", "finish_part"])
    g.add_conditional_edges("fix_part", ok_or_finish("check_part"), ["check_part", "finish_part"])
    g.add_edge("finish_part", END)
    return g.compile()


def build_graph(deps: AgentDeps, *, checkpointer: Any, interrupt_before: Optional[Sequence[str]] = None) -> Any:
    tel = deps.tel
    g = StateGraph(DealState)
    for name, fn in (("ingest_documents", F.ingest_documents), ("extract_comparable", F.extract_comparable),
                     ("check_extraction", _check_extraction), ("review_extraction", F.review_extraction),
                     ("check_style_coverage", F.check_style_coverage),
                     ("build_company_context", F.build_company_context), ("checkpoint_facts", F.checkpoint_facts),
                     ("facts_approved", F.facts_approved)):
        g.add_node(name, guarded(name, fn, tel))
    for name, fn in (("generate_blueprint", D.generate_blueprint), ("plan_work", D.plan_work),
                     ("relevance_gate", D.relevance_gate), ("choose_pages", D.choose_pages),
                     ("assemble_draft", D.assemble_draft), ("drafted", D.drafted),
                     ("checkpoint_review", RV.checkpoint_review), ("apply_review", RV.apply_review),
                     ("export", RV.export)):
        g.add_node(name, guarded(name, _with(fn, deps), tel))
    g.add_node("part", build_part_graph(deps))

    g.add_edge(START, "ingest_documents")
    g.add_conditional_edges("ingest_documents", routing.route_after_ingest, ["extract_comparable", END])
    g.add_edge("extract_comparable", "check_extraction")
    g.add_conditional_edges("check_extraction", routing.route_after_extraction,
                            ["review_extraction", "check_style_coverage", END])
    g.add_conditional_edges("review_extraction", routing.route_after_extraction,
                            ["review_extraction", "check_style_coverage", END])
    g.add_conditional_edges("check_style_coverage", routing.route_after_style_check,
                            ["build_company_context", "checkpoint_facts", END])
    g.add_conditional_edges("build_company_context", routing.route_after_context, ["checkpoint_facts", END])
    g.add_conditional_edges("checkpoint_facts", routing.route_after_facts, ["checkpoint_facts", "facts_approved", END])
    g.add_edge("facts_approved", "generate_blueprint")
    g.add_conditional_edges("generate_blueprint", routing.route_after_blueprint, ["plan_work", END])
    g.add_conditional_edges("plan_work", routing.route_after_plan, ["choose_pages", "relevance_gate", END])
    g.add_conditional_edges("choose_pages", routing.route_after_choose, ["plan_work", END])
    g.add_conditional_edges("relevance_gate", routing.route_after_relevance, ["choose_pages", "part", END])
    g.add_edge("part", "assemble_draft")
    g.add_conditional_edges("assemble_draft", routing.route_after_assemble, ["plan_work", "drafted", END])
    g.add_edge("drafted", "checkpoint_review")
    g.add_conditional_edges("checkpoint_review", routing.route_after_review,
                            ["export", "apply_review", "checkpoint_review", END])
    g.add_conditional_edges("apply_review", routing.route_after_apply, ["checkpoint_review", END])
    g.add_edge("export", END)
    return g.compile(checkpointer=checkpointer, interrupt_before=list(interrupt_before or []))


@contextlib.contextmanager
def deal_graph(deal_id: str, deps: AgentDeps, interrupt_before: Optional[Sequence[str]] = None) -> Iterator[Any]:
    """The compiled graph with its run state stored in the deal folder."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    d = store.deal_dir(deal_id)
    if not (d / "meta.json").exists():
        raise ValueError(f"Unknown deal {deal_id!r}.")
    conn = sqlite3.connect(str(d / RUNS_DB), check_same_thread=False)
    try:
        yield build_graph(deps, checkpointer=SqliteSaver(conn), interrupt_before=interrupt_before)
    finally:
        conn.close()


def run_config(run_id: str, settings: Settings) -> Dict[str, Any]:
    return {"configurable": {"thread_id": run_id}, "max_concurrency": settings.max_concurrency,
            "run_name": f"ipo-agent {run_id}", "metadata": {"run_id": run_id}}


OnStep = Optional[Callable[[str], None]]


def _drive(graph: Any, inp: Any, config: Dict[str, Any], on_step: OnStep) -> None:
    """Run until the next pause or the end. `on_step(node)` is called after each step (live progress in the UI)."""
    if on_step is None:
        graph.invoke(inp, config)
        return
    for chunk in graph.stream(inp, config, stream_mode="updates", subgraphs=True):
        _, update = chunk
        for node in update:
            if not node.startswith("__"):
                on_step(node)


def start(graph: Any, deal_id: str, run_id: str, settings: Settings, tel: Telemetry,
          add_documents: Optional[List[dict]] = None, on_step: OnStep = None) -> Dict[str, Any]:
    """Start a run; returns when the run pauses for the banker, errors, or finishes.
    One parent span per banker action, so every step of it is one trace."""
    if graph.get_state(run_config(run_id, settings)).values:
        raise ValueError(f"Run {run_id!r} already exists; resume it or use a new run id.")
    inp = {"deal_id": deal_id, "run_id": run_id, "add_documents": add_documents or [], "log": []}
    with tel.span("action.start", deal_id=deal_id, run_id=run_id) as sp:
        _drive(graph, inp, run_config(run_id, settings), on_step)
        snap = snapshot(graph, run_id, settings)
        _mark(sp, snap)
    return snap


def can_continue(graph: Any, run_id: str, settings: Settings) -> bool:
    """True when a run stopped between steps (e.g. the page was closed mid-run): no pause, no error, steps left."""
    st = graph.get_state(run_config(run_id, settings))
    return bool(st.values) and bool(st.next) and not st.interrupts and not st.values.get("error")


def continue_run(graph: Any, run_id: str, settings: Settings, tel: Telemetry, on_step: OnStep = None) -> Dict[str, Any]:
    """Carry on from the last saved step of a run that was cut off (not a banker pause)."""
    if not can_continue(graph, run_id, settings):
        raise ValueError(f"Run {run_id!r} has nothing to continue (it is paused, finished or failed).")
    deal_id = graph.get_state(run_config(run_id, settings)).values.get("deal_id")
    with tel.span("action.continue", deal_id=deal_id, run_id=run_id) as sp:
        _drive(graph, None, run_config(run_id, settings), on_step)
        snap = snapshot(graph, run_id, settings)
        _mark(sp, snap)
    return snap


def list_runs(graph: Any) -> List[str]:
    """Run ids saved for this deal, newest first."""
    seen: Dict[str, str] = {}
    for c in graph.checkpointer.list(None):
        tid = c.config["configurable"]["thread_id"]
        ts = c.checkpoint.get("ts", "")
        if ts > seen.get(tid, ""):
            seen[tid] = ts
    return sorted(seen, key=lambda t: seen[t], reverse=True)


def resume(graph: Any, run_id: str, reply: Dict[str, Any], settings: Settings, tel: Telemetry,
           on_step: OnStep = None) -> Dict[str, Any]:
    """Answer the current pause. An invalid reply raises pydantic.ValidationError and changes nothing."""
    waiting = pending(graph, run_id, settings)
    if waiting is None:
        raise ValueError(f"Run {run_id!r} is not waiting for the banker.")
    st = graph.get_state(run_config(run_id, settings))
    deal_id = st.values.get("deal_id")
    # Addressed by interrupt id: a bare dict reply such as {} would otherwise be read as an (empty) map of
    # interrupt ids, and the pause would be asked again instead of the reply being rejected.
    answer = {st.interrupts[0].id: reply}
    with tel.span("action.resume", deal_id=deal_id, run_id=run_id, answering=waiting["kind"]) as sp:
        _drive(graph, Command(resume=answer), run_config(run_id, settings), on_step)
        snap = snapshot(graph, run_id, settings)
        _mark(sp, snap)
    return snap


def _mark(sp: Any, snap: Dict[str, Any]) -> None:
    sp.set_attribute("run.status", snap["status"])
    if snap["waiting_for"]:
        sp.set_attribute("waiting.for", snap["waiting_for"])


def pending(graph: Any, run_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """The pause the run is waiting on: the interrupt value, plus the JSON schema of the reply."""
    st = graph.get_state(run_config(run_id, settings))
    if not st.interrupts:
        return None
    it = st.interrupts[0]
    return {**it.value, "reply_schema": it.response_schema}


def snapshot(graph: Any, run_id: str, settings: Settings) -> Dict[str, Any]:
    st = graph.get_state(run_config(run_id, settings))
    v = st.values
    waiting = pending(graph, run_id, settings)
    status = v.get("status") or "running"
    return {"run_id": run_id, "status": "waiting_banker" if waiting else status, "error": v.get("error"),
            "waiting_for": waiting["kind"] if waiting else None, "pending": waiting, "log": v.get("log", []),
            "extraction": v.get("extraction", []), "facts_approved": bool(v.get("facts_approved")),
            "parts": v.get("parts", []), "item_status": v.get("item_status", {}),
            "missing_ob": v.get("missing_ob", []), "missing_required": v.get("missing_required", []),
            "export_blockers": D.export_blockers(v) if v.get("status") in ("drafted", "done") else [],
            "not_required": v.get("not_required") or {}, "exports": v.get("exports") or {},
            "next": list(st.next), "can_continue": can_continue(graph, run_id, settings)}
