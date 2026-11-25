"""Tools: thin wrappers over `core/` that return short JSON-ready summaries (never whole documents).

Graph nodes call `core/` directly; these tools are for LangGraph Studio debugging and a later chat
helper (LANGGRAPH_PLAN.md §9). Each function is plain Python (easy to test) and is also exposed as a
LangChain tool in `TOOLS`.

Errors are raised as `ToolError` with a plain message (unknown deal, document, page or item). A tool
never substitutes another document, page or value for one it can't find.

Ragas scoring (`score_item`) is added with `agent/evals.py` in build step 5.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from core import blueprint as bpmod
from core import context, pdf_reader, replies, section_extractor, store, validator, workspace
from core.blueprint import Blueprint
from core.company_docs import find_unit
from core.retrieval import UnitIndex

SectionKey = Literal["our_business", "risk_factors"]
CompanyDocType = Literal["ANNUAL_REPORT", "FINANCIALS", "INVESTOR_PPT", "MGMT_QA", "OTHER"]
SNIPPET = 240


class ToolError(ValueError):
    pass


# --------------------------------------------------------------------------- helpers
def _deal(deal_id: str) -> dict:
    if not (store.deal_dir(deal_id) / "meta.json").exists():
        raise ToolError(f"Unknown deal {deal_id!r}.")
    return store.load_meta(deal_id)


def _doc(deal_id: str, doc_id: str):
    _deal(deal_id)
    d = next((d for d in store.documents(deal_id) if d.doc_id == doc_id), None)
    if d is None:
        raise ToolError(f"Deal {deal_id!r} has no document {doc_id!r}.")
    if not Path(d.path).exists():
        raise ToolError(f"{doc_id}: the stored file is missing ({d.path}).")
    return d


def _draft(deal_id: str, section: str):
    _deal(deal_id)
    d = replies.load_draft(deal_id, section)
    if d is None:
        raise ToolError(f"Deal {deal_id!r} has no {section} draft yet.")
    return d


def _item(deal_id: str, section: str, item_id: str):
    draft = _draft(deal_id, section)
    try:
        return draft, workspace.get_item(draft, item_id)
    except KeyError:
        raise ToolError(f"The {section} draft has no item {item_id!r}.") from None


def _snip(text: str, n: int = SNIPPET) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def section_summary(sec) -> Dict[str, Any]:
    """One extracted section, with the agent's acceptance rule: found via the TOC AND start and end
    verified against bold capital headings. Anything else needs the banker."""
    accepted = sec.method == "toc" and sec.start_verified and sec.end_verified
    return {"doc_id": sec.doc_id, "section": sec.section_key, "title": sec.section_title,
            "pdf_pages": [sec.start_page, sec.end_page], "page_count": sec.page_count, "method": sec.method,
            "start_verified": sec.start_verified, "end_verified": sec.end_verified,
            "confidence": round(sec.confidence, 2), "subheadings": len(sec.subheadings),
            "accepted": accepted, "notes": sec.notes}


def _item_summary(item) -> Dict[str, Any]:
    return {"id": item.id, "title": item.title, "group": item.group, "words": len(item.text.split()),
            "citations": item.citations, "unresolved": item.unresolved, "comparable_cites": item.comparable_cites,
            "tbd": item.tbd, "source": item.source, "versions": len(item.versions)}


# --------------------------------------------------------------------------- deals and documents
def create_deal(name: str, sector: str, fy: str) -> Dict[str, Any]:
    """Create a new deal. Returns its id."""
    if not name.strip():
        raise ToolError("A deal needs a name.")
    deal_id = store.create_deal(name.strip(), sector.strip(), fy.strip())
    return {"deal_id": deal_id, "name": name.strip(), "sector": sector.strip(), "fy": fy.strip()}


def add_document(deal_id: str, file_path: str, kind: Literal["company", "comparable"],
                 doc_type: Optional[CompanyDocType] = None) -> Dict[str, Any]:
    """Add a PDF from disk to a deal: company documents get D1, D2 …; comparables C1, C2 …"""
    _deal(deal_id)
    p = Path(file_path)
    if not p.is_file() or p.suffix.lower() != ".pdf":
        raise ToolError(f"Not a PDF file: {file_path}")
    if kind == "company" and doc_type is None:
        raise ToolError("A company document needs a doc_type (ANNUAL_REPORT, FINANCIALS, ...).")
    if kind == "comparable" and doc_type is not None:
        raise ToolError("Comparables take no doc_type.")
    pages = pdf_reader.page_count(p)
    d = store.add_document(deal_id, kind=kind, file_name=p.name, data=p.read_bytes(), doc_type=doc_type,
                           page_count=pages)
    return {"doc_id": d.doc_id, "kind": d.kind, "name": d.name, "doc_type": d.doc_type, "page_count": d.page_count}


def list_documents(deal_id: str) -> Dict[str, Any]:
    """Documents of a deal and the sections extracted so far."""
    meta = _deal(deal_id)
    ext = store.load_extractions(deal_id)
    docs = []
    for d in store.documents(deal_id):
        docs.append({"doc_id": d.doc_id, "kind": d.kind, "name": d.name, "doc_type": d.doc_type,
                     "page_count": d.page_count, "file_present": Path(d.path).exists(),
                     "sections": sorted(k for (di, k) in ext if di == d.doc_id)})
    return {"deal_id": deal_id, "name": meta["name"], "sector": meta.get("sector", ""), "fy": meta.get("fy", ""),
            "documents": docs}


# --------------------------------------------------------------------------- section extraction
def extract_sections(deal_id: str, doc_id: str) -> Dict[str, Any]:
    """Find Our Business and Risk Factors in one comparable (TOC first) and save what was found."""
    d = _doc(deal_id, doc_id)
    if d.kind != "comparable":
        raise ToolError(f"{doc_id} is a company document; sections are extracted from comparables.")
    found = section_extractor.extract_sections(d.path, doc_id=d.doc_id, doc_name=d.name)
    out, missing = [], []
    for key, sec in found.items():
        if sec is None:
            missing.append(key)
            continue
        store.save_extraction(deal_id, sec)
        out.append(section_summary(sec))
    return {"doc_id": doc_id, "sections": out, "not_found": missing,
            "needs_banker": bool(missing) or not all(s["accepted"] for s in out)}


def set_section_range(deal_id: str, doc_id: str, section: SectionKey, start_page: int,
                      end_page: int) -> Dict[str, Any]:
    """Banker correction: set a section's PDF page range by hand (headings are re-checked)."""
    d = _doc(deal_id, doc_id)
    if d.kind != "comparable":
        raise ToolError(f"{doc_id} is a company document.")
    if not 1 <= start_page <= end_page <= d.page_count:
        raise ToolError(f"Pages must satisfy 1 <= start ({start_page}) <= end ({end_page}) <= {d.page_count}.")
    sec = section_extractor.manual_section(d.path, doc_id=d.doc_id, doc_name=d.name, section_key=section,
                                           start_page=start_page, end_page=end_page)
    store.save_extraction(deal_id, sec)
    return section_summary(sec)


# --------------------------------------------------------------------------- company context
def _figures(ind) -> Dict[str, Any]:
    if ind is None:
        return {"basis": None, "unit": None, "years": [], "figures": {}}
    return {"basis": ind.basis, "unit": ind.unit, "years": ind.years,
            "figures": {k: {"label": f.label, "values": f.values, "cite": f.cite} for k, f in ind.figures.items()},
            "notes": ind.notes}


def build_context(deal_id: str) -> Dict[str, Any]:
    """(Re)build printed-page units, financial figures, risk triggers and page mapping for a deal."""
    _deal(deal_id)
    if not store.documents(deal_id, "company"):
        raise ToolError("Add at least one company document first.")
    res = context.process(deal_id)
    mapping = res["mapping"]
    sel = {kind: {k: sum(1 for h in v["hits"] if h["selected"]) for k, v in mapping[kind].items()}
           for kind in ("our_business", "risk_themes")}
    return {"units": res["units"], "figures": _figures(res["indicators"]),
            "triggers": len(res["triggers"]), "required_triggers": sum(t.mandatory for t in res["triggers"]),
            "selected_pages": sel}


def get_figures(deal_id: str) -> Dict[str, Any]:
    """Financial figures read from the statements, with basis, unit and the cited page of each."""
    _deal(deal_id)
    ind = context.load_indicators(deal_id)
    if ind is None:
        raise ToolError("No figures yet: run build_context first.")
    return _figures(ind)


class FigureRow(BaseModel):
    key: str
    label: str
    current: Optional[float] = Field(description="latest year; null removes the value")
    previous: Optional[float] = Field(description="previous year; null removes the value")


def update_figures(deal_id: str, rows: List[FigureRow]) -> Dict[str, Any]:
    """Banker edits to figures. Edited figures keep their cite; new ones are marked as entered by the user."""
    _deal(deal_id)
    ind = context.load_indicators(deal_id)
    if ind is None:
        raise ToolError("No figures yet: run build_context first.")
    rows_d = [r.model_dump() if isinstance(r, BaseModel) else dict(r) for r in rows]
    return _figures(context.update_figures(deal_id, ind, rows_d))


def list_risk_triggers(deal_id: str) -> Dict[str, Any]:
    """Risk triggers found in the company documents, with their evidence pages."""
    _deal(deal_id)
    ts = context.load_triggers(deal_id)
    return {"triggers": [{"key": t.key, "title": t.title, "category": t.category, "required": t.mandatory,
                          "selected": t.selected, "reason": t.reason,
                          "evidence": [{"cite": e.cite, "snippet": _snip(e.snippet, 160)} for e in t.evidence[:5]]}
                         for t in ts]}


def search_company_pages(deal_id: str, query: str, k: int = 6) -> Dict[str, Any]:
    """BM25 search over the company's printed pages. Returns cites and short snippets."""
    _deal(deal_id)
    if not 1 <= k <= 30:
        raise ToolError("k must be between 1 and 30.")
    units = context.load_units(deal_id)
    if not units:
        raise ToolError("The deal has no company pages.")
    hits = UnitIndex(units).search(query, top_k=k)
    return {"query": query, "hits": [{"cite": u.cite, "part": u.part, "score": round(s, 3), "snippet": _snip(u.text)}
                                     for u, s in hits]}


def get_page(deal_id: str, cite: str) -> Dict[str, Any]:
    """Full text of one company page, e.g. cite='D1 p.145' or '[D1 u.2]'."""
    _deal(deal_id)
    u = find_unit(context.load_units(deal_id), cite)
    if u is None:
        raise ToolError(f"No company page matches {cite!r}.")
    return {"cite": u.cite, "location": u.location, "part": u.part, "text": u.text}


# --------------------------------------------------------------------------- blueprint
def get_blueprint(deal_id: str) -> Dict[str, Any]:
    """The deal's current blueprint (Our Business subsections, Risk Factors themes)."""
    _deal(deal_id)
    bp = bpmod.load(deal_id)
    if bp is None:
        raise ToolError("The deal has no blueprint yet.")
    return bp.model_dump()


def set_blueprint(deal_id: str, blueprint: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the blueprint (full JSON). Must have at least one subsection and one theme."""
    _deal(deal_id)
    try:
        bp = Blueprint.model_validate(blueprint)
    except Exception as e:
        raise ToolError(f"Not a valid blueprint: {e}") from None
    if not bp.our_business.subsections or not bp.risk_factors.themes:
        raise ToolError("A blueprint needs at least one Our Business subsection and one risk theme.")
    ids = [s.id for s in bp.our_business.subsections] + [t.id for t in bp.risk_factors.themes]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ToolError(f"Duplicate ids: {', '.join(dup)}")
    bpmod.save(deal_id, bpmod.attach_map_keys(bp))
    return {"subsections": len(bp.our_business.subsections), "themes": len(bp.risk_factors.themes),
            "source": bp.source}


# --------------------------------------------------------------------------- drafts
def check_item(deal_id: str, section: SectionKey, item_id: str) -> Dict[str, Any]:
    """Run the exact checks on one draft item: numbers on cited pages, citations, [TBD]s, copying, RF rules."""
    _, item = _item(deal_id, section, item_id)
    issues = validator.check_item(section, item, context.load_units(deal_id), context.load_indicators(deal_id),
                                  validator.comparable_sentences(deal_id, section))
    return {"item": item_id, "high": sum(i.severity == "high" for i in issues),
            "low": sum(i.severity != "high" for i in issues),
            "issues": [{"kind": i.kind, "severity": i.severity, "message": i.message} for i in issues]}


def edit_item(deal_id: str, section: SectionKey, item_id: str, text: str,
              title: Optional[str] = None) -> Dict[str, Any]:
    """Save new text for an item. The old text is kept as a version."""
    _, item = _item(deal_id, section, item_id)
    if not text.strip():
        raise ToolError("Text is empty.")
    saved = workspace.save_item_edit(deal_id, section, item_id, title if title is not None else item.title, text)
    if saved is None:
        return {"changed": False, **_item_summary(item)}
    return {"changed": True, **_item_summary(saved)}


def restore_version(deal_id: str, section: SectionKey, item_id: str, index: int) -> Dict[str, Any]:
    """Make an earlier version current again (0 = oldest). The current text becomes a version too."""
    _, item = _item(deal_id, section, item_id)
    if not 0 <= index < len(item.versions):
        raise ToolError(f"{item_id} has {len(item.versions)} version(s); index {index} does not exist.")
    return _item_summary(workspace.restore_version(deal_id, section, item_id, index))


def export_section(deal_id: str, section: SectionKey, fmt: Literal["docx", "md"]) -> Dict[str, Any]:
    """Write the section as Word or Markdown into the deal's exports folder. Returns the file path."""
    draft = _draft(deal_id, section)
    out_dir = store.deal_dir(deal_id) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{section}.{fmt}"
    if fmt == "docx":
        path.write_bytes(workspace.section_docx(deal_id, section))
    else:
        path.write_text(workspace.section_markdown(deal_id, section), encoding="utf-8")
    return {"path": str(path), "items": len(draft.items), "bytes": path.stat().st_size}


# --------------------------------------------------------------------------- as LangChain tools
FUNCTIONS = [create_deal, add_document, list_documents, extract_sections, set_section_range, build_context,
             get_figures, update_figures, list_risk_triggers, search_company_pages, get_page, get_blueprint,
             set_blueprint, check_item, edit_item, restore_version, export_section]

TOOLS = [StructuredTool.from_function(f, name=f.__name__, description=(f.__doc__ or "").strip().splitlines()[0])
         for f in FUNCTIONS]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
