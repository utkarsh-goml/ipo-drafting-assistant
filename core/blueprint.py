"""Section blueprint: which subsections / risk themes to draft, in what order, and how.

Two sources:
  * IPO-01 reply from Stylus (learned from the comparables), or
  * a Python default built from config + the deal's risk triggers (useful to start drafting
    without IPO-01, or when Stylus is unavailable).
The banker can include/exclude and reorder entries before drafting.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Set

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from . import store
from .company_docs import load_config


class OBSubsection(BaseModel):
    id: str
    title: str
    purpose: str = ""
    target_words: Optional[int] = None
    content_elements: List[str] = Field(default_factory=list)
    tables: List[str] = Field(default_factory=list)
    example_ref: str = ""
    map_key: Optional[str] = None        # key in config our_business (for page mapping)
    include: bool = True


class RFTheme(BaseModel):
    id: str
    group: str = "Internal Risks"
    theme: str
    heading_pattern: str = ""
    quantification: str = ""
    example_ref: str = ""
    map_key: Optional[str] = None        # key in config risk_themes
    include: bool = True


class OurBusinessBP(BaseModel):
    style_notes: str = ""
    subsections: List[OBSubsection] = Field(default_factory=list)


class RiskFactorsBP(BaseModel):
    style_notes: str = ""
    intro_pattern: str = ""
    groups: List[str] = Field(default_factory=lambda: ["Internal Risks", "External Risks", "Risks Relating to the Offer"])
    themes: List[RFTheme] = Field(default_factory=list)


class Blueprint(BaseModel):
    source: str = "default"              # "default" or the IPO-01 task id
    our_business: OurBusinessBP = Field(default_factory=OurBusinessBP)
    risk_factors: RiskFactorsBP = Field(default_factory=RiskFactorsBP)


DEFAULT_PURPOSE = {
    "overview": "Who we are, when incorporated, promoters/parent, scale, market position, segments, key metrics.",
    "strengths": "4-7 competitive strengths, each a bold heading followed by evidence (numbers, rankings).",
    "strategies": "4-6 forward strategies, each a bold heading with how the Company will execute it.",
    "products": "Products / services / segments with their contribution to revenue and key features.",
    "operations": "How the business operates: facilities, processes, capacity, branches, operating model.",
    "customers": "Customers, distribution channels, geographic reach, concentration.",
    "technology": "Technology platforms, digital initiatives, IT systems and cyber security.",
    "quality_rnd": "R&D, quality control, certifications, innovation.",
    "people": "Employees by function, attrition, training, culture.",
    "csr_esg": "CSR spend and initiatives, ESG framework.",
    "risk_compliance": "Risk management framework, compliance, internal controls.",
    "awards": "Key awards and recognitions.",
    "competition": "Principal competitors and basis of competition.",
    "properties_ip": "Registered office and other properties (owned/leased), trademarks and IP, insurance cover.",
    "financial_perf": "Key financial and operational performance indicators table with year-on-year comparison.",
}
DEFAULT_ORDER = ["overview", "financial_perf", "strengths", "strategies", "products", "operations",
                 "customers", "technology", "quality_rnd", "risk_compliance", "people", "csr_esg",
                 "awards", "competition", "properties_ip"]
THEME_GROUP = {"market": "External Risks"}


def default_blueprint(deal_id: str) -> Blueprint:
    """Generic DRHP blueprint from config + this deal's selected risk triggers."""
    from . import context

    cfg = load_config()
    ob = OurBusinessBP(style_notes="Formal DRHP register, first person plural ('we', 'our Company'); "
                                   "defined terms capitalised; figures with units and periods; tables for metrics.")
    for i, key in enumerate(DEFAULT_ORDER, start=1):
        q = cfg["our_business"][key]
        ob.subsections.append(OBSubsection(id=f"OB{i:02d}", title=q["title"], purpose=DEFAULT_PURPOSE.get(key, ""),
                                           map_key=key))
    rf = RiskFactorsBP(style_notes="Each risk: bold full-sentence heading stating the risk and its adverse effect; "
                                   "body quantified with amounts/percentages and periods; no mitigation language.",
                       intro_pattern="Standard caution: an investment in Equity Shares involves a high degree of risk...")
    n = 0
    seen_titles: Set[str] = set()
    for t in context.load_triggers(deal_id):
        if not t.selected or t.title in seen_titles:
            continue
        n += 1
        seen_titles.add(t.title)
        group = {"Internal": "Internal Risks", "External": "External Risks", "Offer": "Risks Relating to the Offer"}[t.category]
        rf.themes.append(RFTheme(id=f"RF{n:02d}", group=group, theme=t.title, map_key=None,
                                 quantification=t.reason))
    for key, q in cfg["risk_themes"].items():
        if any(fuzz.partial_ratio(q["title"].lower(), s.lower()) > 85 for s in seen_titles):
            continue
        n += 1
        rf.themes.append(RFTheme(id=f"RF{n:02d}", group=THEME_GROUP.get(key, "Internal Risks"),
                                 theme=q["title"], map_key=key))
    rf.themes.sort(key=lambda t: rf.groups.index(t.group) if t.group in rf.groups else 9)
    return Blueprint(source="default", our_business=ob, risk_factors=rf)


def _words(text: str) -> Set[str]:
    """Lower-case words of 3+ letters, crudely singularised (redemptions -> redemption)."""
    return {re.sub(r"(?<=[a-z]{3})s$", "", w) for w in re.findall(r"[a-z]{3,}", text.lower())} - {
        "our", "the", "and", "may", "could", "which", "from", "with", "that", "their", "this"}


def _best_key(title: str, extra: str, entries: dict) -> Optional[str]:
    """Config key for a title: fuzzy title match first, else the most shared keywords (2 or more)."""
    titles = {v["title"]: k for k, v in entries.items()}
    m = process.extractOne(title, list(titles), scorer=fuzz.token_set_ratio)
    if m and m[1] >= 70:
        return titles[m[0]]
    mine = _words(f"{title} {extra}")
    scored = sorted(((len(mine & _words(f"{v['title']} {v.get('terms', '')}")), k) for k, v in entries.items()),
                    reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 2 else None


def attach_map_keys(bp: Blueprint) -> Blueprint:
    """Link LLM-proposed subsections/themes to config keys (for page mapping): title match, then keywords."""
    cfg = load_config()
    for s in bp.our_business.subsections:
        if not s.map_key:
            s.map_key = _best_key(s.title, f"{s.purpose} {' '.join(s.content_elements)}", cfg["our_business"])
    for t in bp.risk_factors.themes:
        if not t.map_key and "offer" not in t.group.lower():      # offer risks need no company pages
            t.map_key = _best_key(t.theme, f"{t.heading_pattern} {t.quantification}", cfg["risk_themes"])
    return bp


def save(deal_id: str, bp: Blueprint) -> None:
    (store.deal_dir(deal_id) / "blueprint.json").write_text(bp.model_dump_json(indent=1), encoding="utf-8")


def load(deal_id: str) -> Optional[Blueprint]:
    f = store.deal_dir(deal_id) / "blueprint.json"
    return Blueprint.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


def from_llm_json(data: dict, task_id: str) -> Blueprint:
    """Build a Blueprint from the IPO-01 JSON (tolerant to missing fields)."""
    ob = data.get("our_business", {}) or {}
    rf = data.get("risk_factors", {}) or {}
    subs = []
    for i, s in enumerate(ob.get("subsections", []), start=1):
        s = dict(s)
        s.setdefault("id", f"OB{i:02d}")
        s["title"] = s.get("title") or f"Subsection {i}"
        s["target_words"] = _int_or_none(s.get("target_words"))
        subs.append(OBSubsection(**{k: v for k, v in s.items() if k in OBSubsection.model_fields}))
    themes = []
    for i, t in enumerate(rf.get("themes", []), start=1):
        t = dict(t)
        t.setdefault("id", f"RF{i:02d}")
        t["theme"] = t.get("theme") or t.get("title") or f"Risk theme {i}"
        themes.append(RFTheme(**{k: v for k, v in t.items() if k in RFTheme.model_fields}))
    bp = Blueprint(
        source=task_id,
        our_business=OurBusinessBP(style_notes=str(ob.get("style_notes", "")), subsections=subs),
        risk_factors=RiskFactorsBP(style_notes=str(rf.get("style_notes", "")),
                                   intro_pattern=str(rf.get("intro_pattern", "")),
                                   groups=rf.get("groups") or RiskFactorsBP().groups, themes=themes),
    )
    return attach_map_keys(bp)


def _int_or_none(v) -> Optional[int]:
    """600 -> 600; "1,200" -> 1200; "800-1000" -> 900; "approx. 600 words" -> 600."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    nums = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", str(v or ""))]
    if not nums:
        return None
    return round(sum(nums[:2]) / len(nums[:2]))


def to_json(bp: Blueprint) -> str:
    return json.dumps(bp.model_dump(), indent=1)
