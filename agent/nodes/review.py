"""Checkpoint 2: the banker reviews the draft, changes it, and approves it; then export.

checkpoint_review (pause) --changes--> apply_review --> checkpoint_review ...
                          --approve--> export --> END       (approve is refused while anything blocks)

Every changed item is checked (validator) and scored (faithfulness against the pages it cites) on its own,
and gets its own status: PASSED or FAILED. The old text is always kept as a version.
"""
from __future__ import annotations

import copy
import csv
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from langgraph.types import interrupt

from agent import pack_text as P
from agent.deps import AgentDeps
from agent.llm.client import call_text
from agent.nodes import drafting as D
from agent.state import DealState, ReviewReply
from core import blueprint as bpmod
from core import context, store, validator, workspace
from core.replies import Draft, DraftItem, ItemVersion, check_citations, load_draft, save_draft, split_items

SECTIONS = ("our_business", "risk_factors")
GROUP_ORDER = ["Internal", "External", "Offer"]
GROUP_OF_LABEL = {"Internal": "Internal Risks", "External": "External Risks", "Offer": "Risks Relating to the Offer"}


# --------------------------------------------------------------------------- what the banker sees
def _required(deal_id: str, not_required: Dict[str, str]) -> List[Tuple[str, str]]:
    return [(t.key, t.title) for t in context.load_triggers(deal_id)
            if t.selected and t.mandatory and t.key not in not_required]


def review_payload(state: DealState) -> dict:
    deal_id = state["deal_id"]
    ist = state.get("item_status") or {}
    sections = {}
    for sec in SECTIONS:
        d = load_draft(deal_id, sec)
        items = []
        for it in (d.items if d else []):
            s = ist.get(sec, {}).get(it.id, {})
            items.append({"item_id": it.id, "title": it.title, "group": it.group, "text": it.text,
                          "status": s.get("status"), "faithfulness": s.get("faithfulness"), "high": s.get("high"),
                          "issues": s.get("issues", []), "unsupported": s.get("unsupported", []),
                          "source": it.source, "versions": len(it.versions), "citations": it.citations,
                          "unresolved": it.unresolved, "tbd": it.tbd})
        sections[sec] = {"items": items, "gaps": d.gaps if d else []}
    bp = bpmod.load(deal_id)
    titles = {s.id: s.title for s in bp.our_business.subsections} if bp else {}
    trig = {t.key: t.title for t in context.load_triggers(deal_id)}
    return {"kind": "checkpoint_review", "notice": state.get("notice"), "sections": sections,
            "missing_ob": [{"item_id": i, "title": titles.get(i, "")} for i in state.get("missing_ob", [])],
            "missing_required": [{"key": k, "title": trig.get(k, k)} for k in state.get("missing_required", [])],
            "not_required": state.get("not_required") or {}, "blockers": D.export_blockers(state)}


def checkpoint_review(state: DealState, deps: AgentDeps) -> dict:
    """Checkpoint 2 (interrupt)."""
    payload = review_payload(state)
    reply: ReviewReply = interrupt(payload, response_schema=ReviewReply)
    if reply.approve:
        if payload["blockers"]:
            return {"review_approved": False, "review_request": None,
                    "notice": "Approval refused: " + "; ".join(payload["blockers"]),
                    "log": [f"Checkpoint 2: approval refused ({len(payload['blockers'])} blocker(s))"]}
        return {"review_approved": True, "review_request": None, "notice": None,
                "log": ["Checkpoint 2: draft approved by the banker"]}
    return {"review_approved": False, "review_request": reply.model_dump(), "notice": None}


# --------------------------------------------------------------------------- applying changes
def _find(draft: Optional[Draft], item_id: str) -> Optional[DraftItem]:
    return next((i for i in (draft.items if draft else []) if i.id == item_id), None)


def _save_version(deal_id: str, section: str, item_id: str, title: str, text: str, source: str) -> bool:
    """New text for an item; the previous text becomes a version. False if nothing changed."""
    draft = load_draft(deal_id, section)
    item = _find(draft, item_id)
    title, text = title.strip(), text.strip()
    if title == item.title and text == item.text.strip():
        return False
    item.versions.append(ItemVersion(at=datetime.now().isoformat(timespec="seconds"),
                                     source=item.source or draft.task_id, title=item.title, text=item.text))
    item.versions = item.versions[-workspace.MAX_VERSIONS:]
    item.title, item.text, item.source = title, text, source
    for k, v in check_citations(item.text, context.load_units(deal_id), set()).items():
        setattr(item, k, v)
    save_draft(deal_id, draft)
    return True


def _new_ob_item(deal_id: str, item_id: str, title: Optional[str], text: str) -> None:
    """Write a missing Our Business subsection, placed in blueprint order."""
    bp = bpmod.load(deal_id)
    sub = next(s for s in bp.our_business.subsections if s.id == item_id)
    draft = load_draft(deal_id, "our_business")
    it = DraftItem(id=item_id, title=(title or sub.title).strip(), text=text.strip(), source="banker edit",
                   **check_citations(text, context.load_units(deal_id), set()))
    order = [s.id for s in bp.our_business.subsections]
    draft.items.append(it)
    draft.items.sort(key=lambda i: order.index(i.id) if i.id in order else len(order))
    draft.missing_ids = [i for i in draft.missing_ids if i != item_id]
    save_draft(deal_id, draft)


def _renumber_rf(deal_id: str, ist: Dict[str, dict]) -> Dict[str, dict]:
    """RF01, RF02, ... in group order (Internal, External, Offer), keeping order within a group."""
    draft = load_draft(deal_id, "risk_factors")
    items = sorted(draft.items, key=lambda i: GROUP_ORDER.index(i.group) if i.group in GROUP_ORDER else 9)
    new_status = {}
    for n, it in enumerate(items, 1):
        new_id = f"RF{n:02d}"
        if it.id in ist:
            new_status[new_id] = ist[it.id]
        it.id = new_id
    draft.items = items
    save_draft(deal_id, draft)
    return new_status


def score_item(state: DealState, deps: AgentDeps, section: str, item: DraftItem, source: str) -> dict:
    """Checks and faithfulness for one item on its own; PASSED only with no high issue and faithfulness >= minimum."""
    deal_id = state["deal_id"]
    units = context.load_units(deal_id)
    issues = [{"item": item.id, "kind": i.kind, "severity": i.severity, "message": i.message}
              for i in validator.check_item(section, item, units, context.load_indicators(deal_id),
                                            validator.comparable_sentences(deal_id, section))]
    contexts = D.cited_texts(units, item.text)
    f, unsupported = None, []
    if contexts:
        r = deps.scorer.faithfulness(item.title, item.text, contexts,
                                     on_usage=D._judge(state, deps, item.id, "eval-faithfulness"))
        f, unsupported = (None if r.score is None else round(r.score, 3)), r.unsupported
    elif section == "our_business":
        issues.append({"item": item.id, "kind": "citation", "severity": "high",
                       "message": "No company page is cited; every fact needs a [D… p.N] citation"})
    else:
        issues.append({"item": item.id, "kind": "faithfulness", "severity": "low",
                       "message": "faithfulness not checked: no company page is cited"})
    high = sum(1 for i in issues if i["severity"] == "high")
    passed = high == 0 and (f is None or f >= deps.settings.faithfulness_min)
    return {"status": "PASSED" if passed else "FAILED", "faithfulness": f, "high": high, "issues": issues,
            "unsupported": unsupported[:20], "source": source}


def _revise(state: DealState, deps: AgentDeps, section: str, item: DraftItem, instructions: str,
            status: dict) -> Optional[Tuple[str, str]]:
    """Rewrite one item with the model (IPO-04 rules). Returns (title, text), or None if the reply has no item."""
    dd = P.load_deal_data(state["deal_id"])
    map_key = None
    if section == "our_business" and dd.blueprint:
        map_key = next((s.map_key for s in dd.blueprint.our_business.subsections if s.id == item.id), None)
    issues = [f"[{i['severity']}] {i['message']}" for i in status.get("issues", []) if i["severity"] == "high"]
    issues += [f"Not supported by the cited pages: {u}" for u in status.get("unsupported", [])]
    pk = P.revise_pack(dd, section=section, item_id=item.id, title=item.title,
                       group=item.group if section == "risk_factors" else "", text=item.text,
                       instructions=instructions, issues=issues, map_key=map_key)
    res = call_text(deps.model("fix"), pk, call="revise", settings=deps.settings, tel=deps.tel,
                    deal_id=state["deal_id"])
    D._ledger(state, res)
    got = [x for x in split_items(res.text)[0] if x[0] == item.id]
    return None if not got else (got[0][2] or item.title, got[0][3])


def apply_review(state: DealState, deps: AgentDeps) -> dict:
    deal_id = state["deal_id"]
    req = ReviewReply.model_validate(state["review_request"])
    ist = copy.deepcopy(state.get("item_status") or {"our_business": {}, "risk_factors": {}})
    missing_ob = list(state.get("missing_ob", []))
    not_required = dict(state.get("not_required") or {})
    problems: List[str] = []
    logs: List[str] = []
    touched: Dict[Tuple[str, str], str] = {}            # (section, item id) -> source label

    for r in req.restore:
        it = _find(load_draft(deal_id, r.section), r.item_id)
        if it is None or r.index >= len(it.versions):
            problems.append(f"{r.item_id}: no such item or version {r.index}")
            continue
        workspace.restore_version(deal_id, r.section, r.item_id, r.index)
        touched[(r.section, r.item_id)] = f"restored version {r.index}"
    for e in req.edit:
        it = _find(load_draft(deal_id, e.section), e.item_id)
        if it is not None:
            if _save_version(deal_id, e.section, e.item_id, e.title or it.title, e.text, "banker edit"):
                touched[(e.section, e.item_id)] = "banker edit"
            else:
                problems.append(f"{e.item_id}: the edit is the same as the current text")
        elif e.section == "our_business" and e.item_id in missing_ob:
            _new_ob_item(deal_id, e.item_id, e.title, e.text)
            missing_ob.remove(e.item_id)
            touched[(e.section, e.item_id)] = "banker edit"
        else:
            problems.append(f"{e.item_id}: no such item in {e.section}")
    for r in req.revise:
        it = _find(load_draft(deal_id, r.section), r.item_id)
        if it is None:
            problems.append(f"{r.item_id}: no such item in {r.section}")
            continue
        got = _revise(state, deps, r.section, it, r.instructions, ist.get(r.section, {}).get(r.item_id, {}))
        if got is None:
            problems.append(f"{r.item_id}: the model's reply had no '### {r.item_id} ...' item; text unchanged")
            continue
        _save_version(deal_id, r.section, r.item_id, got[0], got[1], "revised by the model")
        touched[(r.section, r.item_id)] = "revised by the model"
    for r in req.recheck:
        if _find(load_draft(deal_id, r.section), r.item_id) is None:
            problems.append(f"{r.item_id}: no such item in {r.section}")
        else:
            touched.setdefault((r.section, r.item_id), "rechecked")
    added = []
    for n, a in enumerate(req.add_risk, 1):
        draft = load_draft(deal_id, "risk_factors")
        tmp = f"NEW{n}"
        draft.items.append(DraftItem(id=tmp, title=a.title.strip(), group=a.group, text=a.text.strip(),
                                     source="banker edit", **check_citations(a.text, context.load_units(deal_id), set())))
        save_draft(deal_id, draft)
        touched[("risk_factors", tmp)] = "banker edit"
        added.append(f"{a.group}: {a.title}")
    if req.leave_out:
        bp = bpmod.load(deal_id)
        for sid in req.leave_out:
            if sid not in missing_ob:
                problems.append(f"{sid}: only a missing subsection can be left out here")
                continue
            next(s for s in bp.our_business.subsections if s.id == sid).include = False
            missing_ob.remove(sid)
            logs.append(f"Banker left out {sid}")
        bpmod.save(deal_id, bp)
    req_keys = {k for k, _ in _required(deal_id, {})}
    for nr in req.not_required:
        if nr.key not in req_keys:
            problems.append(f"'{nr.key}' is not a required risk")
            continue
        not_required[nr.key] = nr.reason
        logs.append(f"Banker marked required risk '{nr.key}' as not applicable: {nr.reason}")

    for (sec, iid), source in touched.items():
        it = _find(load_draft(deal_id, sec), iid)
        s = score_item(state, deps, sec, it, source)
        ist.setdefault(sec, {})[iid] = s
        logs.append(f"{iid}: {source}; {s['status']} (faithfulness {s['faithfulness']}, {s['high']} high issue(s))")
    if added:
        ist["risk_factors"] = _renumber_rf(deal_id, ist.get("risk_factors", {}))
        logs.append("Banker added risk(s): " + "; ".join(added) + "; risks renumbered")

    rf = load_draft(deal_id, "risk_factors")
    rf_items = rf.items if rf else []
    unc = validator.uncovered_required([i.title for i in rf_items], [i.text for i in rf_items],
                                       _required(deal_id, not_required))
    return {"item_status": ist, "missing_ob": missing_ob, "missing_required": [i.excerpt for i in unc],
            "not_required": not_required, "review_request": None,
            "notice": ("Not applied: " + "; ".join(problems)) if problems else None, "log": logs}


# --------------------------------------------------------------------------- export
def export(state: DealState, deps: AgentDeps) -> dict:
    deal_id, run_id = state["deal_id"], state.get("run_id") or "run"
    blockers = D.export_blockers(state)
    if blockers:                       # approval already checks this; never export a blocked draft
        raise ValueError("export refused: " + "; ".join(blockers))
    out = store.deal_dir(deal_id) / "exports" / run_id
    out.mkdir(parents=True, exist_ok=True)
    note = f"AI-assisted draft (agent run {run_id}, drafting model {deps.settings.openai_model_draft})"
    rel = out.relative_to(store.deal_dir(deal_id)).as_posix()      # paths relative to the deal folder
    files = {}
    for sec in SECTIONS:
        (out / f"{sec}.docx").write_bytes(workspace.section_docx(deal_id, sec, note=note))
        (out / f"{sec}.md").write_text(workspace.section_markdown(deal_id, sec, note=note), encoding="utf-8")
        files[f"{sec}.docx"] = f"{rel}/{sec}.docx"
        files[f"{sec}.md"] = f"{rel}/{sec}.md"
    ist = state.get("item_status") or {}
    with (out / "checks.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "item_id", "title", "status", "faithfulness", "high_issues", "source"])
        for sec in SECTIONS:
            d = load_draft(deal_id, sec)
            for it in (d.items if d else []):
                s = ist.get(sec, {}).get(it.id, {})
                w.writerow([sec, it.id, it.title, s.get("status"), s.get("faithfulness"), s.get("high"),
                            it.source])
        for k, reason in (state.get("not_required") or {}).items():
            w.writerow(["risk_factors", "", f"required risk '{k}' marked not applicable: {reason}", "", "", "",
                        "banker"])
    files["checks.csv"] = f"{rel}/checks.csv"
    return {"exports": files, "status": "done", "log": [f"Exported Word, Markdown and checks to {rel}/"]}


__all__ = ["checkpoint_review", "apply_review", "export", "review_payload", "score_item"]
