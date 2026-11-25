"""Part A nodes: ingest -> extract (per comparable) -> review_extraction -> context -> checkpoint 1.

Nothing here guesses. A section found by a fallback method, or with an unverified start or end,
waits for the banker. Checkpoint 1 shows every figure with its cited page and basis, the risk
triggers and the selected pages; the run goes on only after an explicit approval.
"""
from __future__ import annotations

from typing import Dict, List

from langgraph.types import interrupt

from agent import tools
from agent.routing import SECTIONS, needs_review, usable_sections
from agent.state import DealState, FactsReply, ReviewExtractionReply
from core import context, store
from core.packer import fmt_inr

SECTION_TITLES = {"our_business": "Our Business", "risk_factors": "Risk Factors"}


# --------------------------------------------------------------------------- ingest
def ingest_documents(state: DealState) -> dict:
    deal_id = state["deal_id"]
    logs = []
    for nd in state.get("add_documents") or []:
        d = tools.add_document(deal_id, nd["path"], nd["kind"], nd.get("doc_type"))
        logs.append(f"Added {d['doc_id']} {d['name']} ({d['page_count']} pages)")
    docs = tools.list_documents(deal_id)["documents"]
    missing = [d["doc_id"] for d in docs if not d["file_present"]]
    if missing:
        raise ValueError(f"stored file missing for {', '.join(missing)}; upload it again")
    if not any(d["kind"] == "company" for d in docs):
        raise ValueError("the deal has no company document (D1, ...)")
    if not any(d["kind"] == "comparable" for d in docs):
        raise ValueError("the deal has no comparable prospectus (C1, ...)")
    logs.append("Documents: " + ", ".join(f"{d['doc_id']} {d['name']}" for d in docs))
    return {"docs": docs, "status": "running", "add_documents": [], "log": logs}


# --------------------------------------------------------------------------- extraction
def extract_comparable(state: dict) -> dict:
    """Fan-out task: one comparable. State here is the Send payload {deal_id, doc_id}."""
    res = tools.extract_sections(state["deal_id"], state["doc_id"])
    flags = [dict(s, found=True) for s in res["sections"]]
    flags += [{"doc_id": state["doc_id"], "section": k, "title": SECTION_TITLES.get(k, k), "found": False,
               "accepted": False, "method": None, "pdf_pages": None} for k in res["not_found"]]
    lines = []
    for f in flags:
        if f["found"]:
            lines.append(f"{f['doc_id']} {f['title']}: PDF p{f['pdf_pages'][0]}-{f['pdf_pages'][1]} via {f['method']}"
                         + ("" if f["accepted"] else " - needs banker review"))
        else:
            lines.append(f"{f['doc_id']} {f['title']}: not found - needs banker review")
    return {"extraction": flags, "log": lines}


def _review_reason(f: dict) -> str:
    if not f.get("found"):
        return "not found in the document"
    why = []
    if f.get("method") != "toc":
        why.append(f"found by {f.get('method')} instead of the table of contents")
    if not f.get("start_verified"):
        why.append("start not confirmed by a bold capital heading")
    if not f.get("end_verified"):
        why.append("end not confirmed by the next section's heading")
    return "; ".join(why) or "not accepted"


def review_extraction(state: DealState) -> dict:
    """Interrupt: the banker decides each section that wasn't found cleanly."""
    pending = needs_review(state)
    value = {"kind": "review_extraction", "notice": state.get("notice"),
             "sections": [{**f, "reason": _review_reason(f)} for f in pending],
             "actions": {"confirm": "use the pages found", "set_range": "give PDF start and end pages",
                         "drop": "don't use this comparable section"}}
    reply: ReviewExtractionReply = interrupt(value, response_schema=ReviewExtractionReply)

    deal_id = state["deal_id"]
    by_key = {(f["doc_id"], f["section"]): f for f in pending}
    updates, logs, refused = [], [], []
    for d in reply.decisions:
        f = by_key.get((d.doc_id, d.section))
        if f is None:
            refused.append(f"{d.doc_id} {d.section} is not waiting for review")
            continue
        if d.action == "confirm":
            if not f.get("found"):
                refused.append(f"{d.doc_id} {d.section} was not found, so it can't be confirmed: set_range or drop")
                continue
            updates.append({**f, "banker_decision": "confirm"})
            logs.append(f"Banker confirmed {d.doc_id} {f['title']} PDF p{f['pdf_pages'][0]}-{f['pdf_pages'][1]}")
        elif d.action == "set_range":
            s = tools.set_section_range(deal_id, d.doc_id, d.section, d.start_page, d.end_page)
            updates.append({**s, "found": True, "banker_decision": "set_range"})
            logs.append(f"Banker set {d.doc_id} {s['title']} to PDF p{d.start_page}-{d.end_page}")
        else:
            (store.deal_dir(deal_id) / "extracted" / f"{d.doc_id}__{d.section}.json").unlink(missing_ok=True)
            updates.append({**f, "banker_decision": "drop", "dropped": True})
            logs.append(f"Banker dropped {d.doc_id} {f['title']}")
    return {"extraction": updates, "log": logs, "notice": ("Not applied: " + "; ".join(refused)) if refused else None}


def check_style_coverage(state: DealState) -> dict:
    """Each section type needs at least one usable comparable section (style source)."""
    usable = {f["section"] for f in usable_sections(state)}
    missing = [SECTION_TITLES[k] for k in SECTIONS if k not in usable]
    if missing:
        raise ValueError(f"no usable comparable section for {', '.join(missing)}: add a comparable or set a page range")
    return {"log": ["Comparable sections ready: " + ", ".join(
        f"{f['doc_id']} {f['title']}" for f in usable_sections(state))]}


# --------------------------------------------------------------------------- company context
def build_company_context(state: DealState) -> dict:
    res = tools.build_context(state["deal_id"])
    pat = res["figures"]["figures"].get("pat")
    line = (f"Company context: {res['units']} printed pages, {len(res['figures']['figures'])} figures "
            f"({res['figures']['basis'] or 'no basis'}, {res['figures']['unit'] or 'no unit'}), "
            f"{res['triggers']} risk triggers ({res['required_triggers']} required)")
    if pat:
        line += f"; PAT {fmt_inr(pat['values'][0])} [{pat['cite']}]"
    return {"context_built": True, "log": [line]}


def facts_payload(deal_id: str) -> dict:
    """What checkpoint 1 shows: figures with cites and basis, triggers, selected pages, blockers."""
    figs = tools.get_figures(deal_id)
    trig = tools.list_risk_triggers(deal_id)["triggers"]
    mapping = context.load_mapping(deal_id) or {}
    pages: Dict[str, Dict[str, dict]] = {}
    for kind in ("our_business", "risk_themes"):
        pages[kind] = {}
        for key, v in mapping.get(kind, {}).items():
            pages[kind][key] = {"title": v.get("title", key),
                                "selected": [h["cite"] for h in v["hits"] if h.get("selected")],
                                "candidates": [{"cite": h["cite"], "score": h["score"], "part": h.get("part", "")}
                                               for h in v["hits"]]}
    blockers: List[str] = []
    if not figs["figures"]:
        blockers.append("No financial figures were read: enter them before approving.")
    if not figs["basis"]:
        blockers.append("The basis of the figures (consolidated / standalone) is not known.")
    if not pages.get("our_business", {}).get("overview", {}).get("selected"):
        blockers.append("No pages are selected for Overview (needed to adapt the blueprint).")
    return {"kind": "checkpoint_facts", "basis": figs["basis"], "unit": figs["unit"], "years": figs["years"],
            "figures": [{"key": k, **v} for k, v in figs["figures"].items()], "triggers": trig,
            "pages": pages, "blockers": blockers}


def apply_page_selection(deal_id: str, choices) -> List[str]:
    """Select / deselect pages for subsections and risk themes. Returns problems; the rest is applied."""
    problems: List[str] = []
    mapping = context.load_mapping(deal_id) or {}
    for c in choices:
        entry = mapping.get(c.kind, {}).get(c.key)
        if entry is None:
            problems.append(f"unknown {c.kind} key {c.key!r}")
            continue
        hit = next((h for h in entry["hits"] if h["cite"] == c.cite), None)
        if hit is None:
            if not c.selected:
                problems.append(f"{c.cite} is not a page of {c.key}")
                continue
            u = next((u for u in context.load_units(deal_id) if u.cite == c.cite), None)
            if u is None:
                problems.append(f"no company page {c.cite}")
                continue
            entry["hits"].append({"cite": u.cite, "doc_id": u.doc_id, "unit_no": u.unit_no,
                                  "location": u.location, "part": u.part, "score": 0.0, "selected": True,
                                  "added_by": "banker"})
        else:
            hit["selected"] = c.selected
    context.save_mapping(deal_id, mapping)
    return problems


def _apply_edits(deal_id: str, reply: FactsReply) -> List[str]:
    """Apply banker edits. Returns problems (unknown keys/cites); everything else is applied."""
    problems: List[str] = []
    if reply.figure_edits:
        tools.update_figures(deal_id, [tools.FigureRow(**e.model_dump()) for e in reply.figure_edits])
    if reply.trigger_selection:
        ts = context.load_triggers(deal_id)
        known = {t.key for t in ts}
        problems += [f"unknown trigger {k!r}" for k in reply.trigger_selection if k not in known]
        for t in ts:
            if t.key in reply.trigger_selection:
                t.selected = reply.trigger_selection[t.key]
        context.save_triggers(deal_id, ts)
    if reply.page_selection:
        problems += apply_page_selection(deal_id, reply.page_selection)
    return problems


def checkpoint_facts(state: DealState) -> dict:
    """Checkpoint 1 (interrupt): approve the facts, or send edits and see them again."""
    deal_id = state["deal_id"]
    payload = facts_payload(deal_id)
    payload["notice"] = state.get("notice")
    reply: FactsReply = interrupt(payload, response_schema=FactsReply)
    if reply.approved:
        if payload["blockers"]:
            return {"facts_approved": False, "notice": "Approval refused: " + " ".join(payload["blockers"]),
                    "log": ["Checkpoint 1: approval refused (blockers)"]}
        return {"facts_approved": True, "notice": None, "log": ["Checkpoint 1: facts approved by the banker"]}
    problems = _apply_edits(deal_id, reply)
    n = len(reply.figure_edits) + len(reply.trigger_selection) + len(reply.page_selection)
    return {"facts_approved": False, "notice": ("Not applied: " + "; ".join(problems)) if problems else None,
            "log": [f"Checkpoint 1: {n} edit(s) applied" + (f", {len(problems)} not applied" if problems else "")]}


def facts_approved(state: DealState) -> dict:
    """End of part A; part B (blueprint, drafting) follows."""
    return {"status": "facts_approved", "log": ["Facts approved; generating the blueprint."]}


__all__ = ["ingest_documents", "extract_comparable", "review_extraction", "check_style_coverage",
           "build_company_context", "checkpoint_facts", "facts_approved", "facts_payload", "apply_page_selection"]
