"""Every conditional-edge function of the graph, in one place (LANGGRAPH_PLAN.md §5.4).

Each takes the state and returns the name of the next node (or a list of Send for fan-out).
An `error` in the state always ends the run.
"""
from __future__ import annotations

from typing import List, Union

from langgraph.graph import END
from langgraph.types import Send

from agent.state import DealState, section_ok

SECTIONS = ("our_business", "risk_factors")


def route_after_ingest(state: DealState) -> Union[str, List[Send]]:
    """One extract task per comparable (fan-out)."""
    if state.get("error"):
        return END
    comps = [d["doc_id"] for d in state.get("docs", []) if d["kind"] == "comparable"]
    return [Send("extract_comparable", {"deal_id": state["deal_id"], "doc_id": c}) for c in comps]


def needs_review(state: DealState) -> List[dict]:
    """Extraction flags that need a banker decision: not found, not via the TOC, or an end not verified."""
    return [f for f in state.get("extraction", []) if not section_ok(f)]


def usable_sections(state: DealState) -> List[dict]:
    return [f for f in state.get("extraction", []) if section_ok(f) and not f.get("dropped")]


def route_after_extraction(state: DealState) -> str:
    """TOC found and start+end verified for every section (or decided by the banker) -> context.
    Otherwise the banker reviews. Also stops if a section type has no usable comparable at all."""
    if state.get("error"):
        return END
    if needs_review(state):
        return "review_extraction"
    return "check_style_coverage"


def route_after_style_check(state: DealState) -> str:
    return END if state.get("error") else ("build_company_context" if not state.get("context_built")
                                           else "checkpoint_facts")


def route_after_context(state: DealState) -> str:
    return END if state.get("error") else "checkpoint_facts"


def route_after_facts(state: DealState) -> str:
    """Edits -> show the facts again; approved -> blueprint."""
    if state.get("error"):
        return END
    return "facts_approved" if state.get("facts_approved") else "checkpoint_facts"


def route_after_blueprint(state: DealState) -> str:
    """Blueprint passed the schema and has >= 1 subsection and >= 1 theme -> plan; otherwise the run errored."""
    return END if state.get("error") or not state.get("blueprint_ready") else "plan_work"


def route_after_plan(state: DealState) -> str:
    """Items without pages -> banker. Items over MAX_INPUT_TOKENS already stopped the run (error)."""
    if state.get("error"):
        return END
    return "choose_pages" if state.get("pages_problems") else "relevance_gate"


def route_after_relevance(state: DealState) -> Union[str, List[Send]]:
    """Every item's relevance >= RELEVANCE_MIN (or accepted by the banker) -> draft all items in parallel."""
    if state.get("error"):
        return END
    if state.get("pages_problems"):
        return "choose_pages"
    return [Send("part", {"deal_id": state["deal_id"], "run_id": state.get("run_id", ""), "item": it})
            for it in state.get("work_items", [])]


def route_after_choose(state: DealState) -> str:
    return END if state.get("error") else "plan_work"


def route_part(state: dict, *, max_fix_rounds: int, passed: bool) -> str:
    """Inside one part: passed -> finish; failed with rounds left -> fix; failed, rounds used -> finish (FAILED)."""
    if state.get("error"):
        return "finish_part"
    if passed:
        return "finish_part"
    return "fix_part" if state.get("rounds", 0) < max_fix_rounds else "finish_part"


def route_after_review(state: DealState) -> str:
    """Checkpoint 2: approved (nothing blocking) -> export; changes -> apply them; refused approval -> ask again."""
    if state.get("error"):
        return END
    if state.get("review_approved"):
        return "export"
    return "apply_review" if state.get("review_request") else "checkpoint_review"


def route_after_apply(state: DealState) -> str:
    return END if state.get("error") else "checkpoint_review"


def route_after_assemble(state: DealState) -> str:
    """Missing subsections / required risks and a gap round left -> plan the missing items only."""
    if state.get("error"):
        return END
    return "plan_work" if state.get("gap_plan") else "drafted"
