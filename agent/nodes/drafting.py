"""Part B: blueprint -> plan -> relevance gate -> (per work item, in parallel) draft/check/evaluate/fix -> assemble.

Rules (LANGGRAPH_PLAN.md §2, §5.4):
- the blueprint comes from the model under the strict schema; an unusable one stops the run;
- every banker-selected page is sent; an item over MAX_INPUT_TOKENS stops the run naming the item;
- an item with no pages, or with pages the judge finds irrelevant, goes back to the banker (choose_pages);
- a part is PASSED only with no high issue and faithfulness >= FAITHFULNESS_MIN; after MAX_FIX_ROUNDS fixes
  it is FAILED, kept and clearly marked, and blocks export;
- missing Our Business subsections or required risks get one gap round (MAX_GAP_ROUNDS), then block export.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from langgraph.types import Send, interrupt

from agent import pack_text as P
from agent import usage
from agent.deps import AgentDeps
from agent.llm.client import call_blueprint, call_text
from agent.llm.prompts import system_prompt
from agent.nodes.facts import apply_page_selection
from agent.state import ChoosePagesReply, DealState
from core import blueprint as bpmod
from core import context, store, validator
from core.blueprint import OBSubsection
from core.company_docs import find_unit
from core.replies import CITE_BLOCK_RX, CITE_RX, Draft, DraftItem, check_citations, save_draft, split_items

GROUP_ORDER = list(P.RF_NUMBER_RANGE)                     # Internal, External, Offer
GROUP_LABEL = {"Internal Risks": "Internal", "External Risks": "External", "Risks Relating to the Offer": "Offer"}
MAX_ISSUES_KEPT = 50


def run_dir(deal_id: str, run_id: str) -> Path:
    d = store.deal_dir(deal_id) / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- blueprint
def generate_blueprint(state: DealState, deps: AgentDeps) -> dict:
    dd = P.load_deal_data(state["deal_id"])
    bp, res = call_blueprint(deps.model("blueprint"), P.blueprint_pack(dd), settings=deps.settings, tel=deps.tel,
                             deal_id=state["deal_id"], source=f"agent:{state.get('run_id', '')}")
    bpmod.save(state["deal_id"], bp)
    _ledger(state, res)
    return {"blueprint_ready": True, "log": [
        f"Blueprint: {len(bp.our_business.subsections)} Our Business subsections, "
        f"{len(bp.risk_factors.themes)} risk themes ({res.tokens_in or res.tokens_in_estimate:,} tokens in)"]}


def _ledger(state: dict, res) -> None:
    usage.record(state["deal_id"], state.get("run_id", ""), item_id=res.item_id, call=res.call, model=res.model,
                 tokens_in=res.tokens_in, tokens_out=res.tokens_out, latency_s=res.latency_s,
                 cost_usd=res.cost_usd, tokens_estimate=res.tokens_in_estimate,
                 part=(state.get("item") or {}).get("item_id"))


def _judge(state: dict, deps: AgentDeps, item_id: str, call: str):
    return usage.judge_callback(state["deal_id"], state.get("run_id", ""), item_id, call, deps.settings, deps.tel)


# --------------------------------------------------------------------------- planning
def _brief_ob(sub: OBSubsection) -> str:
    parts = [f"Our Business subsection '{sub.title}' of an Indian IPO prospectus."]
    if sub.purpose:
        parts.append(sub.purpose)
    if sub.content_elements:
        parts.append("It covers: " + "; ".join(sub.content_elements) + ".")
    return " ".join(parts)


def _brief_rf(dd: P.DealData, group: str, only: Optional[List[str]]) -> str:
    trig = [t.title for t in dd.triggers if t.selected and P.RF_GROUP_OF_CATEGORY.get(t.category) == group
            and (only is None or t.key in only)]
    themes = [] if only is not None else [t.theme for t in dd.blueprint.risk_factors.themes
                                          if t.include and t.group == group]
    return f"Risk factors ({group}) of an Indian IPO prospectus: " + "; ".join(trig + themes) + "."


def pack_for(dd: P.DealData, item: dict) -> P.ItemPack:
    if item["section"] == "our_business":
        sub = next((s for s in dd.blueprint.our_business.subsections if s.id == item["ob_id"]), None)
        if sub is None:
            raise P.PackError(f"{item['ob_id']} is not in the blueprint")
        return P.ob_pack(dd, sub)
    return P.rf_group_pack(dd, item["group"], only_triggers=item.get("gap_triggers"),
                           first_number=item.get("first_number"))


def _statement_cites(dd: P.DealData) -> set:
    return {u.cite for u in P._statement_units(dd)}


def _plan_one(dd: P.DealData, item: dict, deps: AgentDeps, stmt: set) -> Tuple[Optional[dict], Optional[dict]]:
    """(item with pack facts, None) or (None, pages problem). ItemTooLarge propagates (stops the run)."""
    try:
        pk = pack_for(dd, item)
    except P.NoPagesSelected as e:
        return None, {**item, "reason": "no pages selected", "message": str(e)}
    n = P.check_budget(pk.item_id, system_prompt(pk.kind), pk.render(), deps.settings.openai_tokenizer,
                       deps.settings.max_input_tokens)
    return {**item, "item_id": pk.item_id, "input_tokens": n, "pages": pk.source_cites,
            "relevance_cites": [c for c in pk.source_cites if c not in stmt], "relevance": None}, None


def plan_work(state: DealState, deps: AgentDeps) -> dict:
    """One work item per included OB subsection and per RF group with something to write.
    In a gap round, only the missing items."""
    dd = P.load_deal_data(state["deal_id"])
    bp = dd.blueprint
    gap = state.get("gap_plan")
    wanted: List[dict] = []
    for sub in bp.our_business.subsections:
        if sub.include and (gap is None or sub.id in gap.get("ob", [])):
            wanted.append({"item_id": sub.id, "section": "our_business", "kind": "IPO-02", "ob_id": sub.id,
                           "title": f"{sub.id} {sub.title}", "map_key": sub.map_key, "brief": _brief_ob(sub)})
    logs = []
    for group in GROUP_ORDER:
        if gap is not None:
            keys = gap.get("rf", {}).get(group)
            if not keys:
                continue
            wanted.append({"item_id": P.group_item_id(group) + "-GAP", "section": "risk_factors", "kind": "IPO-03",
                           "group": group, "title": f"{group} (missing required risks)", "gap_triggers": keys,
                           "first_number": gap["first_number"][group], "brief": _brief_rf(dd, group, keys)})
            continue
        has_trig = any(t.selected and P.RF_GROUP_OF_CATEGORY.get(t.category) == group for t in dd.triggers)
        has_theme = any(t.include and t.group == group for t in bp.risk_factors.themes)
        if not (has_trig or has_theme):
            logs.append(f"{group}: nothing selected to write; group left out")
            continue
        wanted.append({"item_id": P.group_item_id(group), "section": "risk_factors", "kind": "IPO-03", "group": group,
                       "title": group, "brief": _brief_rf(dd, group, None)})
    if not wanted:
        raise ValueError("nothing to draft: no subsections or risk groups are included")
    stmt = _statement_cites(dd)
    items, problems = [], []
    for w in wanted:
        it, prob = _plan_one(dd, w, deps, stmt)
        (items if it else problems).append(it or prob)
    logs.append(f"Planned {len(items)} work item(s)" + (f"; {len(problems)} need pages" if problems else "")
                + (" (gap round)" if gap is not None else ""))
    return {"work_items": items, "pages_problems": problems, "log": logs}


# --------------------------------------------------------------------------- relevance gate
def relevance_gate(state: DealState, deps: AgentDeps) -> dict:
    """Ragas context relevance of each item's own pages (statement pages excluded) against its brief."""
    dd = P.load_deal_data(state["deal_id"])
    accepted = set(state.get("accepted_low") or [])
    items, low, logs = [], [], []
    for it in state.get("work_items", []):
        it = dict(it)
        if not it["relevance_cites"]:
            it["relevance_note"] = "not scored: no item-specific pages (offer risks use standard wording)"
            items.append(it)
            continue
        texts = [u.text for c in it["relevance_cites"] if (u := find_unit(dd.units, c))]
        score = deps.scorer.relevance(it["brief"], texts,
                                      on_usage=_judge(state, deps, it["item_id"], "eval-relevance"))
        it["relevance"] = round(score, 3)
        deps.tel.record_relevance(score, it["section"])
        if score < deps.settings.relevance_min and it["item_id"] not in accepted:
            low.append({**it, "reason": f"low relevance {score:.2f} (minimum {deps.settings.relevance_min:.2f})"})
        items.append(it)
    logs.append("Relevance: " + ", ".join(f"{i['item_id']} {i['relevance']}" for i in items if i["relevance"] is not None))
    return {"work_items": items, "pages_problems": low, "log": logs}


def _candidates(dd: P.DealData, item: dict) -> Dict[str, dict]:
    mapping = dd.mapping
    if item["section"] == "our_business":
        keys = [("our_business", item.get("map_key"))]
    else:
        keys = [("risk_themes", t.map_key) for t in dd.blueprint.risk_factors.themes
                if t.include and t.group == item["group"] and t.map_key]
    out = {}
    for kind, key in keys:
        if key and key in mapping.get(kind, {}):
            v = mapping[kind][key]
            out[f"{kind}:{key}"] = {"kind": kind, "key": key, "title": v.get("title", key),
                                    "selected": [h["cite"] for h in v["hits"] if h.get("selected")],
                                    "candidates": [{"cite": h["cite"], "score": h["score"]} for h in v["hits"]]}
    return out


def choose_pages(state: DealState, deps: AgentDeps) -> dict:
    """Interrupt: items without pages, or with low relevance. The banker picks pages (or decides)."""
    deal_id = state["deal_id"]
    dd = P.load_deal_data(deal_id)
    probs = state.get("pages_problems", [])
    payload = {"kind": "choose_pages", "notice": state.get("notice"),
               "items": [{"item_id": p["item_id"], "title": p["title"], "reason": p["reason"],
                          "relevance": p.get("relevance"), "pages": _candidates(dd, p)} for p in probs]}
    reply: ChoosePagesReply = interrupt(payload, response_schema=ChoosePagesReply)
    problems, logs = [], []
    if reply.page_selection:
        problems += apply_page_selection(deal_id, reply.page_selection)
        logs.append(f"Banker changed {len(reply.page_selection)} page selection(s)")
    low_ids = {p["item_id"] for p in probs if p["reason"].startswith("low relevance")}
    ok_anyway = [i for i in reply.proceed_anyway if i in low_ids]
    problems += [f"{i}: proceed_anyway is only for items with low relevance" for i in reply.proceed_anyway
                 if i not in low_ids]
    if ok_anyway:
        logs.append("Banker chose to draft despite low relevance: " + ", ".join(ok_anyway))
    if reply.exclude_subsections:
        bp = dd.blueprint
        ids = {s.id for s in bp.our_business.subsections}
        for sid in reply.exclude_subsections:
            if sid not in ids:
                problems.append(f"{sid} is not an Our Business subsection")
        for s in bp.our_business.subsections:
            if s.id in reply.exclude_subsections:
                s.include = False
        bpmod.save(deal_id, bp)
        logs.append("Banker left out: " + ", ".join(i for i in reply.exclude_subsections if i in ids))
    return {"accepted_low": ok_anyway, "log": logs,
            "notice": ("Not applied: " + "; ".join(problems)) if problems else None}


# --------------------------------------------------------------------------- one part (subgraph nodes)
def _render(items: List[Tuple[str, str, str, str]]) -> str:
    out = []
    for iid, grp, title, body in items:
        head = f"### {iid} " + (f"[{grp}] " if grp else "") + title
        out.append(f"{head}\n{body.strip()}")
    return "\n\n".join(out)


def _expected(item: dict) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    if item["section"] == "our_business":
        return item["ob_id"], None
    lo, hi = P.RF_NUMBER_RANGE[item["group"]]
    return None, (item.get("first_number") or lo, hi)


def _rf_num(iid: str) -> Optional[int]:
    m = re.fullmatch(r"RF(\d+)", iid)
    return int(m.group(1)) if m else None


def parse_part(item: dict, text: str) -> Tuple[List[Tuple[str, str, str, str]], List[str], List[dict]]:
    """Items of a reply that belong to this work item, the GAPS bullets, and structural issues."""
    parsed, gaps = split_items(text)
    want_id, rng = _expected(item)
    keep, issues = [], []
    for iid, grp, title, body in parsed:
        if want_id is not None:
            if iid == want_id:
                keep.append((iid, grp, title, body))
            else:
                issues.append({"item": iid, "kind": "structure", "severity": "low",
                               "message": f"Extra item {iid} in the reply was ignored"})
        else:
            n = _rf_num(iid)
            if n is None or not rng[0] <= n <= rng[1]:
                issues.append({"item": iid, "kind": "structure", "severity": "high",
                               "message": f"{iid} is outside RF{rng[0]:02d}-RF{rng[1]:02d} for {item['group']}"})
            keep.append((iid, grp or GROUP_LABEL[item["group"]], title, body))
    if not keep:
        issues.append({"item": want_id or item["item_id"], "kind": "structure", "severity": "high",
                       "message": f"The reply has no '### {want_id or 'RFnn'} ...' item"})
    return keep, gaps, issues


def draft_part(state: dict, deps: AgentDeps) -> dict:
    item = state["item"]
    dd = P.load_deal_data(state["deal_id"])
    pk = pack_for(dd, item)
    res = call_text(deps.model("draft"), pk, call="draft", settings=deps.settings, tel=deps.tel,
                    deal_id=state["deal_id"])
    _ledger(state, res)
    keep, gaps, issues = parse_part(item, res.text)
    text = _render(keep) if keep else res.text
    _save_round(state, item, 0, text)
    return {"text": text, "gaps": gaps, "structure": issues, "rounds": 0, "pack_cites": pk.source_cites}


def _save_round(state: dict, item: dict, rnd: int, text: str) -> None:
    d = run_dir(state["deal_id"], state.get("run_id") or "run") / "parts"
    d.mkdir(exist_ok=True)
    (d / f"{item['item_id']}.r{rnd}.md").write_text(text, encoding="utf-8")


def check_part(state: dict, deps: AgentDeps) -> dict:
    item, deal_id = state["item"], state["deal_id"]
    units = context.load_units(deal_id)
    ind = context.load_indicators(deal_id)
    comp = validator.comparable_sentences(deal_id, item["section"])
    issues = list(state.get("structure") or [])
    pack = set(state.get("pack_cites") or [])
    for iid, grp, title, body in split_items(state["text"])[0]:
        di = DraftItem(id=iid, title=title, group=grp, text=body)
        for i in validator.check_item(item["section"], di, units, ind, comp):
            issues.append({"item": iid, "kind": i.kind, "severity": i.severity, "message": i.message})
        for c in check_citations(body, units, pack)["not_in_pack"]:
            issues.append({"item": iid, "kind": "citation", "severity": "low",
                           "message": f"[{c}] is a real page but was not given to the model"})
    high = sum(1 for i in issues if i["severity"] == "high")
    return {"issues": issues, "high": high}


def cited_texts(units, text: str) -> List[str]:
    seen, out = set(), []
    for block in CITE_BLOCK_RX.findall(text):
        for doc, kind, num in CITE_RX.findall(block):
            c = f"{doc} {kind}.{num}"
            if doc.startswith("D") and c not in seen:
                seen.add(c)
                u = find_unit(units, c)
                if u is not None:
                    out.append(f"[{u.cite}]\n{u.text}")
    return out


def evaluate_part(state: dict, deps: AgentDeps) -> dict:
    """Ragas faithfulness of the text against the pages it cites."""
    item, deal_id = state["item"], state["deal_id"]
    contexts = cited_texts(context.load_units(deal_id), state["text"])
    if not contexts:
        out = {"faithfulness": None, "unsupported": [],
               "faithfulness_by_round": list(state.get("faithfulness_by_round") or []) + [None]}
        if item["section"] == "our_business":
            # every fact in Our Business needs a [D… p.N] citation
            return {**out, "high": state.get("high", 0) + 1, "issues": list(state.get("issues", [])) + [
                {"item": item.get("ob_id", item["item_id"]), "kind": "citation", "severity": "high",
                 "message": "No company page is cited; every fact needs a [D… p.N] citation"}]}
        # e.g. offer risks in standard wording. Uncited numbers are already high issues from the validator.
        note = "faithfulness not checked: no company page is cited"
        return {**out, "issues": list(state.get("issues", [])) + [
            {"item": item["item_id"], "kind": "faithfulness", "severity": "low", "message": note}]}
    r = deps.scorer.faithfulness(item["brief"], state["text"], contexts,
                                 on_usage=_judge(state, deps, item["item_id"], "eval-faithfulness"))
    f = None if r.score is None else round(r.score, 3)
    return {"faithfulness": f, "unsupported": r.unsupported,
            "faithfulness_by_round": list(state.get("faithfulness_by_round") or []) + [f]}


def part_passed(state: dict, deps: AgentDeps) -> bool:
    f = state.get("faithfulness")
    return state.get("high", 0) == 0 and (f is None or f >= deps.settings.faithfulness_min)


def _fix_instructions(state: dict, iid: str, deps: AgentDeps) -> Tuple[str, List[str]]:
    issues = [f"[{i['severity']}] {i['message']}" for i in state.get("issues", [])
              if i.get("item") in (iid, state["item"]["item_id"]) and i["severity"] == "high"]
    f = state.get("faithfulness")
    lines = ["Fix every problem listed below. Keep all correct, cited content and the heading line."]
    if f is not None and f < deps.settings.faithfulness_min:
        lines.append(f"A checker found claims not supported by the cited pages (faithfulness {f:.2f}, "
                     f"minimum {deps.settings.faithfulness_min:.2f}). Support each with the right page, or remove it, "
                     "or write [TBD: ...]:")
        lines += [f"- {u}" for u in state.get("unsupported", [])[:20]]
    return "\n".join(lines), issues


def fix_part(state: dict, deps: AgentDeps) -> dict:
    """IPO-04 rules, one call per item that needs it (OB: the subsection; RF: each failing risk)."""
    item, deal_id = state["item"], state["deal_id"]
    dd = P.load_deal_data(deal_id)
    parsed = split_items(state["text"])[0]
    faith_low = (state.get("faithfulness") is not None
                 and state["faithfulness"] < deps.settings.faithfulness_min)
    failing = {i["item"] for i in state.get("issues", []) if i["severity"] == "high"}
    new_items, notes = [], []
    if not parsed:                                     # no usable item: draft the whole item again
        res = call_text(deps.model("fix"), pack_for(dd, item), call="fix", settings=deps.settings, tel=deps.tel,
                        deal_id=deal_id)
        _ledger(state, res)
        keep, _, _ = parse_part(item, res.text)
        new_items = keep
    for iid, grp, title, body in parsed:
        if not (faith_low or iid in failing or item["item_id"] in failing):
            new_items.append((iid, grp, title, body))
            continue
        instr, issues = _fix_instructions(state, iid, deps)
        pk = P.revise_pack(dd, section=item["section"], item_id=iid, title=title,
                           group=grp if item["section"] == "risk_factors" else "", text=body, instructions=instr,
                           issues=issues, map_key=item.get("map_key"))
        res = call_text(deps.model("fix"), pk, call="fix", settings=deps.settings, tel=deps.tel, deal_id=deal_id)
        _ledger(state, res)
        got = [x for x in split_items(res.text)[0] if x[0] == iid]
        if got:
            g = got[0]
            new_items.append((iid, g[1] or grp, g[2] or title, g[3]))
        else:
            new_items.append((iid, grp, title, body))
            notes.append({"item": iid, "kind": "structure", "severity": "high",
                          "message": f"The fix reply had no '### {iid} ...' item; previous text kept"})
    rounds = state.get("rounds", 0) + 1
    text = _render(new_items) if new_items else state["text"]
    _save_round(state, item, rounds, text)
    structure = [i for i in (state.get("structure") or []) if i["kind"] == "structure" and i["severity"] == "low"]
    return {"text": text, "rounds": rounds, "structure": structure + notes}


def finish_part(state: dict, deps: AgentDeps) -> dict:
    item = state["item"]
    if state.get("error"):
        return {}
    passed = part_passed(state, deps)
    status = "PASSED" if passed else "FAILED"
    d = run_dir(state["deal_id"], state.get("run_id") or "run") / "parts"
    d.mkdir(exist_ok=True)
    f = d / f"{item['item_id']}.md"
    f.write_text(state["text"], encoding="utf-8")
    issues = state.get("issues", [])
    deps.tel.record_part(section=item["section"], status=status, fix_rounds=state.get("rounds", 0),
                         faithfulness=state.get("faithfulness"))
    summary = {"item_id": item["item_id"], "section": item["section"], "title": item["title"],
               "group": item.get("group"), "status": status, "rounds": state.get("rounds", 0),
               "faithfulness": state.get("faithfulness"), "relevance": item.get("relevance"),
               "faithfulness_by_round": state.get("faithfulness_by_round") or [],
               "high": sum(1 for i in issues if i["severity"] == "high"),
               "low": sum(1 for i in issues if i["severity"] != "high"),
               "issues": issues[:MAX_ISSUES_KEPT], "unsupported": (state.get("unsupported") or [])[:20],
               "gaps": state.get("gaps", []), "pages": item.get("pages", []),
               "file": f.relative_to(store.deal_dir(state["deal_id"])).as_posix()}   # relative: survives a restore
    line = (f"{item['item_id']}: {status} after {summary['rounds']} fix round(s)"
            f" (faithfulness {summary['faithfulness']}, {summary['high']} high issue(s))")
    return {"parts": [summary], "log": [line]}


# --------------------------------------------------------------------------- assemble
def _item_status(p: dict, ids: List[str]) -> dict:
    """One draft item's status: its part's result, with the issues that name this item (or the whole part)."""
    issues = [i for i in p.get("issues", []) if i.get("item") in ids]
    return {"status": p["status"], "part": p["item_id"], "faithfulness": p["faithfulness"],
            "high": sum(1 for i in issues if i["severity"] == "high"), "issues": issues,
            "unsupported": p.get("unsupported", []), "source": "agent draft"}


def _part_items(deal_id: str, part: dict) -> List[Tuple[str, str, str, str]]:
    return split_items((store.deal_dir(deal_id) / part["file"]).read_text(encoding="utf-8"))[0]


def assemble_draft(state: DealState, deps: AgentDeps) -> dict:
    if state.get("error"):                  # a part failed to run (e.g. API error): nothing is assembled
        return {}
    deal_id, run_id = state["deal_id"], state.get("run_id", "")
    dd = P.load_deal_data(deal_id)
    units = dd.units
    parts = {p["item_id"]: p for p in state.get("parts", [])}
    item_status: Dict[str, Dict[str, dict]] = {"our_business": {}, "risk_factors": {}}
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")

    # Our Business, in blueprint order
    ob = Draft(section="our_business", task_id=run_id, created=now)
    missing_ob = []
    for sub in dd.blueprint.our_business.subsections:
        if not sub.include:
            continue
        p = parts.get(sub.id)
        got = [x for x in _part_items(deal_id, p) if x[0] == sub.id] if p else []
        if not got:
            missing_ob.append(sub.id)
            continue
        iid, grp, title, body = got[0]
        ob.items.append(DraftItem(id=iid, title=title, text=body, source=run_id,
                                  **check_citations(body, units, set(p["pages"]))))
        item_status["our_business"][iid] = _item_status(p, [iid, p["item_id"]])
        ob.gaps += p.get("gaps", [])
    ob.missing_ids = missing_ob

    # Risk Factors: groups in order, gap-round risks after their group, numbered RF01, RF02, ...
    rf = Draft(section="risk_factors", task_id=run_id, created=now)
    n = 0
    max_in_group: Dict[str, int] = {}
    for group in GROUP_ORDER:
        for pid in (P.group_item_id(group), P.group_item_id(group) + "-GAP"):
            p = parts.get(pid)
            if not p:
                continue
            for iid, grp, title, body in sorted(_part_items(deal_id, p), key=lambda x: _rf_num(x[0]) or 999):
                num = _rf_num(iid)
                if num is not None:
                    max_in_group[group] = max(max_in_group.get(group, 0), num)
                n += 1
                new_id = f"RF{n:02d}"
                rf.items.append(DraftItem(id=new_id, title=title, group=grp or GROUP_LABEL[group], text=body,
                                          source=f"{run_id} {iid}", **check_citations(body, units, set(p["pages"]))))
                item_status["risk_factors"][new_id] = {**_item_status(p, [iid, pid]), "drafted_as": iid}
            rf.gaps += p.get("gaps", [])

    required = [(t.key, t.title) for t in dd.triggers if t.selected and t.mandatory]
    unc = validator.uncovered_required([i.title for i in rf.items], [i.text for i in rf.items], required)
    missing_req = [i.excerpt for i in unc]
    save_draft(deal_id, ob)
    save_draft(deal_id, rf)
    logs = [f"Assembled Our Business ({len(ob.items)} subsections) and Risk Factors ({len(rf.items)} risks)"]
    out = {"item_status": item_status, "missing_ob": missing_ob, "missing_required": missing_req, "gap_plan": None}

    rounds = state.get("gap_rounds", 0)
    if (missing_ob or missing_req) and rounds < deps.settings.max_gap_rounds:
        by_group: Dict[str, List[str]] = {}
        for key in missing_req:
            t = next(t for t in dd.triggers if t.key == key)
            by_group.setdefault(P.RF_GROUP_OF_CATEGORY[t.category], []).append(key)
        first = {}
        for g in by_group:
            lo, hi = P.RF_NUMBER_RANGE[g]
            first[g] = max_in_group.get(g, lo - 1) + 1
            if first[g] > hi:
                raise ValueError(f"{g}: no RF numbers left for a gap round")
        out["gap_plan"] = {"ob": missing_ob, "rf": by_group, "first_number": first}
        out["gap_rounds"] = rounds + 1
        logs.append("Gap round: " + ", ".join(missing_ob + [f"required risk '{k}'" for k in missing_req]))
    elif missing_ob or missing_req:
        logs.append("Still missing after the gap round (blocks export): "
                    + ", ".join(missing_ob + [f"required risk '{k}'" for k in missing_req]))
    out["log"] = logs
    return out


def drafted(state: DealState, deps: AgentDeps) -> dict:
    blockers = export_blockers(state)
    return {"status": "drafted", "log": ["Draft ready for banker review"
                                         + (f"; export blocked: {len(blockers)} reason(s)" if blockers else "")]}


def export_blockers(state: DealState) -> List[str]:
    """Why the draft can't be exported yet (empty = it can): FAILED items, missing subsections, required risks."""
    out = []
    for sec in ("our_business", "risk_factors"):
        for iid, s in (state.get("item_status") or {}).get(sec, {}).items():
            if s["status"] == "FAILED":
                out.append(f"{iid} FAILED the checks ({s.get('high', 0)} high issue(s), "
                           f"faithfulness {s.get('faithfulness')})")
    out += [f"Our Business subsection {i} is missing" for i in state.get("missing_ob", [])]
    out += [f"Required risk '{k}' is not covered" for k in state.get("missing_required", [])]
    return out
