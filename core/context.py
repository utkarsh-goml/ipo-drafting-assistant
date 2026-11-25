"""Company context for a deal: units, financial indicators, risk triggers, page mapping.

Saved under data/deals/<deal>/context/ so the banker's edits (figures, trigger selection,
page selection per subsection) survive and feed the packs in Phase 3.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import store
from .company_docs import Unit, read_units
from .financials import Figure, Indicators, compute_derived, extract_indicators
from .retrieval import map_subsections
from .risk_triggers import Trigger, financial_triggers, find_triggers

FIN_DOC_TYPES = {"ANNUAL_REPORT", "FINANCIALS"}
DEFAULT_MIN_SCORE = 0.35   # hits below this relative score are not pre-selected


def _dir(deal_id: str) -> Path:
    d = store.deal_dir(deal_id) / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_units(deal_id: str) -> List[Unit]:
    """All company-document units of the deal, in upload order."""
    units: List[Unit] = []
    for doc in store.documents(deal_id, "company"):
        units.extend(read_units(doc.path, doc.doc_id, doc.doc_type or "OTHER"))
    return units


def unit_lookup(units: List[Unit]) -> Dict[Tuple[str, int], Unit]:
    return {(u.doc_id, u.unit_no): u for u in units}


def process(deal_id: str) -> dict:
    """(Re)build indicators, triggers and mapping from the company documents."""
    meta = store.load_meta(deal_id)
    docs = store.documents(deal_id, "company")
    units = load_units(deal_id)
    fin_ids = {d.doc_id for d in docs if (d.doc_type or "") in FIN_DOC_TYPES}
    fin_units = [u for u in units if u.doc_id in fin_ids] or units

    ind = extract_indicators(fin_units)
    triggers = find_triggers(units, ind)
    mapping = map_subsections(units, meta.get("sector", ""))
    for kind in ("our_business", "risk_themes"):
        for sub in mapping[kind].values():
            for h in sub["hits"]:
                h["selected"] = h["score"] >= DEFAULT_MIN_SCORE
    save_indicators(deal_id, ind)
    save_triggers(deal_id, triggers)
    save_mapping(deal_id, mapping)
    return {"units": len(units), "indicators": ind, "triggers": triggers, "mapping": mapping}


# --------------------------------------------------------------------------- persistence
def save_indicators(deal_id: str, ind: Indicators) -> None:
    (_dir(deal_id) / "indicators.json").write_text(ind.model_dump_json(indent=1), encoding="utf-8")


def load_indicators(deal_id: str) -> Optional[Indicators]:
    f = _dir(deal_id) / "indicators.json"
    return Indicators.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


def save_triggers(deal_id: str, triggers: List[Trigger]) -> None:
    (_dir(deal_id) / "triggers.json").write_text(
        json.dumps([t.model_dump() for t in triggers], indent=1), encoding="utf-8")


def load_triggers(deal_id: str) -> List[Trigger]:
    f = _dir(deal_id) / "triggers.json"
    return [Trigger(**t) for t in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []


def save_mapping(deal_id: str, mapping: dict) -> None:
    (_dir(deal_id) / "mapping.json").write_text(json.dumps(mapping, indent=1), encoding="utf-8")


def load_mapping(deal_id: str) -> Optional[dict]:
    f = _dir(deal_id) / "mapping.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def update_figures(deal_id: str, ind: Indicators, rows: List[dict]) -> Indicators:
    """Apply banker edits [{key, label, current, previous, cite}] and refresh financial triggers."""
    for r in rows:
        key = r["key"]
        vals = [r.get("current"), r.get("previous")]
        vals = [None if v is None or v != v else float(v) for v in vals]  # NaN -> None
        if all(v is None for v in vals):
            ind.figures.pop(key, None)
            continue
        old = ind.figures.get(key)
        if old and old.values == vals:
            continue
        ind.figures[key] = Figure(key=key, label=r["label"], values=vals,
                                  cite=(old.cite if old else "manual"),
                                  source_line=("edited by user" if old else "entered by user"))
    compute_derived(ind)
    save_indicators(deal_id, ind)
    # replace financial triggers, keep textual ones (and their selection)
    keep = [t for t in load_triggers(deal_id)
            if not t.key.startswith(("negative_ocf", "loss_", "revenue_ops_decline", "pat_decline", "high_leverage"))]
    save_triggers(deal_id, financial_triggers(ind) + keep)
    return ind
