"""Packs as labelled text for the OpenAI API (the agent's version of core/packer.py).

The Stylus packs are PDFs with a page budget. The agent sends the same three parts as text:

    === 00_TASK_SHEET ===            instructions, blueprint, verified figures
    === 01_SOURCE ===                company pages, each starting with its label [D1 p.45]
    === 02_STYLE_REF ===             comparable pages [C1 p.160] (STYLE ONLY)

It is the same page text the validator checks numbers against. There is no page budget:
every banker-selected page goes in. Instead the whole call is counted with tiktoken and
`check_budget` raises `ItemTooLarge` (naming the work item) if it is over MAX_INPUT_TOKENS.
Nothing is ever trimmed or dropped here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core import blueprint as bpmod
from core import context, store
from core.blueprint import Blueprint, OBSubsection, RFTheme
from core.company_docs import Unit, find_unit
from core.financials import Indicators
from core.models import ExtractedSection
from core.packer import _figures_blocks, statement_units
from core.replies import CITE_BLOCK_RX, CITE_RX
from core.risk_triggers import Trigger

SOURCE_RX = re.compile(r"^D\d+ [pu]\.\d+$")
STYLE_RX = re.compile(r"^C\d+ p\.\d+$")

RF_GROUP_OF_CATEGORY = {"Internal": "Internal Risks", "External": "External Risks",
                        "Offer": "Risks Relating to the Offer"}
# Each Risk Factors group is drafted separately; these number ranges keep IDs unique across groups.
# Assembly renumbers them RF01, RF02, ... in one run across the section.
RF_NUMBER_RANGE = {"Internal Risks": (1, 39), "External Risks": (40, 69), "Risks Relating to the Offer": (70, 99)}


class PackError(ValueError):
    """A pack can't be built as asked (missing data or no pages). The run hands this to the banker."""


class NoPagesSelected(PackError):
    def __init__(self, item_id: str, what: str):
        super().__init__(f"{item_id}: no company pages are selected for '{what}'. "
                         "Select pages for it (Company Context) before drafting.")
        self.item_id = item_id


class ItemTooLarge(ValueError):
    def __init__(self, item_id: str, tokens: int, cap: int):
        super().__init__(f"{item_id}: the input is {tokens:,} tokens, over MAX_INPUT_TOKENS={cap:,}. "
                         "Deselect pages or split the item; nothing was dropped automatically.")
        self.item_id, self.tokens, self.cap = item_id, tokens, cap


# --------------------------------------------------------------------------- pages
@dataclass(frozen=True)
class PageBlock:
    cite: str          # "D1 p.45" / "D1 u.3" for source pages, "C1 p.160" for style pages
    role: str          # source | style
    text: str
    note: str = ""     # style pages: which comparable section

    def __post_init__(self):
        if self.role == "source" and not SOURCE_RX.match(self.cite):
            raise ValueError(f"source pages must be company pages (D…), got {self.cite!r}")
        if self.role == "style" and not STYLE_RX.match(self.cite):
            raise ValueError(f"style pages must be comparable pages (C…), got {self.cite!r}")
        if self.role not in ("source", "style"):
            raise ValueError(f"unknown role {self.role!r}")

    def render(self) -> str:
        if self.role == "source":
            return f"[{self.cite}]\n{self.text.strip()}"
        return f"[{self.cite}] STYLE ONLY - do not use facts ({self.note})\n{self.text.strip()}"


def source_block(u: Unit) -> PageBlock:
    return PageBlock(cite=u.cite, role="source", text=u.text)


def style_blocks(sec: ExtractedSection) -> List[PageBlock]:
    return [PageBlock(cite=f"{sec.doc_id} p.{p.page_no}", role="style", text="\n".join(p.lines),
                      note=sec.section_title) for p in sec.pages]


def _dedupe(units: Iterable[Unit]) -> List[Unit]:
    seen, out = set(), []
    for u in units:
        k = (u.doc_id, u.unit_no)
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


# --------------------------------------------------------------------------- task sheet as text
def blocks_to_text(blocks: Sequence[tuple]) -> str:
    """core.packer sheet blocks -> plain text / markdown."""
    out: List[str] = []
    for kind, val in blocks:
        if kind == "h1":
            out.append(f"# {val}")
        elif kind == "h2":
            out.append(f"\n## {val}")
        elif kind == "h3":
            out.append(f"\n### {val}")
        elif kind == "kv":
            out.append(f"{val[0]}: {val[1]}")
        elif kind == "p":
            out.append(str(val))
        elif kind == "bullets":
            out.extend(f"- {x}" for x in val)
        elif kind == "table":
            rows = [[str(c).replace("|", "/").replace("\n", " ") for c in r] for r in val]
            if rows:
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("|" + "---|" * len(rows[0]))
                out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        else:
            raise ValueError(f"unknown sheet block {kind!r}")
    return "\n".join(out).strip()


def _header(title: str, item_id: str, meta: dict) -> List[tuple]:
    return [("h1", f"TASK SHEET - {title}"), ("kv", ("Work item", item_id)), ("kv", ("Company", meta["name"])),
            ("kv", ("Sector", meta.get("sector") or "not stated")),
            ("kv", ("Latest financial year", meta.get("fy") or "not stated"))]


# --------------------------------------------------------------------------- the pack
@dataclass
class ItemPack:
    item_id: str                 # BLUEPRINT, OB03, RF-Internal, REV-OB03, ...
    kind: str                    # IPO-01 | IPO-02 | IPO-03 | IPO-04 (which preset's rules apply)
    sheet: str
    source: List[PageBlock] = field(default_factory=list)
    style: List[PageBlock] = field(default_factory=list)

    @property
    def source_cites(self) -> List[str]:
        return [b.cite for b in self.source]

    @property
    def style_cites(self) -> List[str]:
        return [b.cite for b in self.style]

    def render(self) -> str:
        """The user message for the call."""
        parts = ["=== 00_TASK_SHEET ===", self.sheet, "",
                 f"=== 01_SOURCE ({len(self.source)} company pages - the ONLY source of facts) ==="]
        parts += [b.render() + "\n" for b in self.source] or ["(no company pages for this item)\n"]
        if self.style:
            parts += [f"=== 02_STYLE_REF ({len(self.style)} comparable pages - STYLE ONLY) ==="]
            parts += [b.render() + "\n" for b in self.style]
        return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- tokens
@lru_cache(maxsize=4)
def _encoding(name: str):
    import tiktoken
    return tiktoken.get_encoding(name)


def count_tokens(text: str, encoding_name: str) -> int:
    return len(_encoding(encoding_name).encode(text, disallowed_special=()))


# Chat format adds a few tokens per message (role markers); counted so the cap is not undershot.
PER_MESSAGE_OVERHEAD = 4


def call_tokens(system: str, user: str, encoding_name: str) -> int:
    return count_tokens(system, encoding_name) + count_tokens(user, encoding_name) + 2 * PER_MESSAGE_OVERHEAD


def check_budget(item_id: str, system: str, user: str, encoding_name: str, cap: int) -> int:
    """Token count of the call; raises ItemTooLarge when over the cap."""
    n = call_tokens(system, user, encoding_name)
    if n > cap:
        raise ItemTooLarge(item_id, n, cap)
    return n


# --------------------------------------------------------------------------- deal data
@dataclass
class DealData:
    meta: dict
    units: List[Unit]
    extractions: Dict[Tuple[str, str], ExtractedSection]
    indicators: Optional[Indicators]
    triggers: List[Trigger]
    mapping: dict
    blueprint: Optional[Blueprint] = None

    @property
    def lookup(self) -> Dict[Tuple[str, int], Unit]:
        return context.unit_lookup(self.units)

    def sections(self, section_key: str) -> List[ExtractedSection]:
        return [s for (_, k), s in sorted(self.extractions.items()) if k == section_key]


def load_deal_data(deal_id: str) -> DealData:
    return DealData(meta=store.load_meta(deal_id), units=context.load_units(deal_id),
                    extractions=store.load_extractions(deal_id), indicators=context.load_indicators(deal_id),
                    triggers=context.load_triggers(deal_id), mapping=context.load_mapping(deal_id) or {},
                    blueprint=bpmod.load(deal_id))


def selected_units(dd: DealData, kind: str, key: Optional[str]) -> List[Unit]:
    """Banker-selected pages for a subsection (kind=our_business) or risk theme (kind=risk_themes)."""
    if not key:
        return []
    hits = dd.mapping.get(kind, {}).get(key, {}).get("hits", [])
    lookup = dd.lookup
    out = []
    for h in hits:
        if h.get("selected"):
            u = lookup.get((h["doc_id"], h["unit_no"]))
            if u is None:
                raise PackError(f"mapping points at {h.get('cite')} (unit {h['unit_no']}), which is not in the documents")
            out.append(u)
    return out


def _statement_units(dd: DealData) -> List[Unit]:
    return statement_units(dd.indicators, dd.units)


def _need_blueprint(dd: DealData) -> Blueprint:
    if dd.blueprint is None:
        raise PackError("There is no blueprint yet: generate it first.")
    return dd.blueprint


# --------------------------------------------------------------------------- builders
def blueprint_pack(dd: DealData) -> ItemPack:
    """Call 1 (IPO-01 rules): banker-selected overview pages + the full text of every extracted
    comparable section."""
    if not dd.extractions:
        raise PackError("No comparable sections are extracted yet.")
    overview = selected_units(dd, "our_business", "overview")
    if not overview:
        raise NoPagesSelected("BLUEPRINT", "Overview")
    style: List[PageBlock] = []
    for sec in [s for _, s in sorted(dd.extractions.items())]:
        style += style_blocks(sec)

    b = _header("Learn style & build blueprint", "BLUEPRINT", dd.meta)
    b.append(("h2", "Comparable sections provided (full text in 02_STYLE_REF)"))
    rows = [["ID", "Prospectus", "Section", "PDF pages"]]
    for (doc_id, _), sec in sorted(dd.extractions.items()):
        rows.append([doc_id, sec.doc_name, sec.section_title, f"{sec.start_page}-{sec.end_page}"])
    b.append(("table", rows))
    b.append(("h2", "Outlines of the comparable sections (headings in order)"))
    for (doc_id, _), sec in sorted(dd.extractions.items()):
        b.append(("h3", f"{doc_id} - {sec.section_title}"))
        b.append(("bullets", [f"[p.{h.page_no}] {h.text[:160]}" for h in sec.subheadings] or ["(no headings detected)"]))
    b.append(("h2", "What to produce"))
    b.append(("bullets", [
        "A blueprint for OUR BUSINESS adapted to the target company (see 01_SOURCE): subsections in order, "
        "each with purpose, content elements, tables and target length in words.",
        "A RISK FACTORS taxonomy: groups (Internal / External / Offer-related) and themes, each with heading "
        "pattern and how comparables quantify it.",
    ]))
    return ItemPack("BLUEPRINT", "IPO-01", blocks_to_text(b), [source_block(u) for u in overview], style)


def ob_pack(dd: DealData, sub: OBSubsection) -> ItemPack:
    """Call 2 (IPO-02 rules) for ONE Our Business subsection."""
    bp = _need_blueprint(dd)
    pages = selected_units(dd, "our_business", sub.map_key)
    if not pages:
        raise NoPagesSelected(sub.id, sub.title)
    units = _dedupe(_statement_units(dd) + pages)
    style = [blk for sec in dd.sections("our_business") for blk in style_blocks(sec)]

    b = _header("Draft Our Business - one subsection", sub.id, dd.meta)
    b.append(("h2", "Style notes (learned from comparables)"))
    b.append(("p", bp.our_business.style_notes or "(none given)"))
    b.append(("h2", "Write ONLY this subsection"))
    b.append(("h3", f"{sub.id} {sub.title}"))
    lines = []
    if sub.purpose:
        lines.append(f"Purpose: {sub.purpose}")
    if sub.content_elements:
        lines.append("Cover: " + "; ".join(sub.content_elements))
    if sub.tables:
        lines.append("Tables: " + "; ".join(sub.tables))
    if sub.target_words:
        lines.append(f"Target length: about {sub.target_words} words")
    lines.append("Pages selected for it: " + ", ".join(u.cite for u in pages))
    b.append(("bullets", lines))
    b.append(("p", "Use the heading line exactly: ### " + f"{sub.id} {sub.title}"))
    b += _figures_blocks(dd.indicators)
    return ItemPack(sub.id, "IPO-02", blocks_to_text(b), [source_block(u) for u in units], style)


def rf_group_pack(dd: DealData, group: str, *, only_triggers: Optional[Sequence[str]] = None,
                  first_number: Optional[int] = None) -> ItemPack:
    """Call 3 (IPO-03 rules) for ONE Risk Factors group (Internal / External / Offer).
    Gap round: `only_triggers` limits it to those required risks (no themes), numbered from `first_number`."""
    bp = _need_blueprint(dd)
    if group not in RF_NUMBER_RANGE:
        raise PackError(f"unknown Risk Factors group {group!r}")
    lo, hi = RF_NUMBER_RANGE[group]
    gap = only_triggers is not None
    if gap:
        if first_number is None or not lo <= first_number <= hi:
            raise PackError(f"{group}: gap round needs a first number within RF{lo:02d}-RF{hi:02d}")
        lo = first_number
    triggers = [t for t in dd.triggers if t.selected and RF_GROUP_OF_CATEGORY.get(t.category) == group
                and (not gap or t.key in only_triggers)]
    themes = [] if gap else [t for t in bp.risk_factors.themes if t.include and t.group == group]
    if not triggers and not themes:
        raise PackError(f"{group}: no selected risks and no blueprint themes for this group.")
    evidence = [u for t in triggers for e in t.evidence if (u := find_unit(dd.units, e.cite))]
    theme_pages = [u for t in themes for u in selected_units(dd, "risk_themes", t.map_key)]
    offer = group == "Risks Relating to the Offer"
    if not offer and not (evidence or theme_pages):
        raise NoPagesSelected(group_item_id(group) + ("-GAP" if gap else ""), group)
    units = _dedupe(_statement_units(dd) + evidence + theme_pages)
    style = [blk for sec in dd.sections("risk_factors") for blk in style_blocks(sec)]

    item_id = group_item_id(group) + ("-GAP" if gap else "")
    b = _header(f"Draft Risk Factors - {group}" + (" - missing required risks only" if gap else ""), item_id, dd.meta)
    b.append(("h2", "Style notes"))
    b.append(("p", bp.risk_factors.style_notes or "(none given)"))
    b.append(("h2", f"Write ONLY the risks of this group: {group}"))
    b.append(("p", f"Number them RF{lo:02d}, RF{lo + 1:02d}, ... (at most RF{hi:02d}), in order of materiality. "
                   f"The group label in each heading is [{group.split()[0] if not offer else 'Offer'}]."))
    req = [t for t in triggers if t.mandatory]
    opt = [t for t in triggers if not t.mandatory]
    b.append(("h2", f"REQUIRED risks - write each of these ({len(req)})"))
    for t in req:
        ev = "; ".join(f"[{e.cite}] {e.snippet[:200]}" for e in t.evidence[:3]) or "Standard IPO offer risk."
        b.append(("bullets", [f"{t.title} - {t.reason}. Evidence: {ev}"]))
    if opt:
        b.append(("h2", "Further risks flagged by the documents (write if supported)"))
        for t in opt:
            b.append(("bullets", [f"{t.title} - {t.reason}. " + "; ".join(f"[{e.cite}]" for e in t.evidence[:4])]))
    if themes:
        b.append(("h2", "Risk themes for this group (from the blueprint)"))
        rows = [["Theme", "Heading pattern / quantification", "Pages selected"]]
        for t in themes:
            sel = ", ".join(u.cite for u in selected_units(dd, "risk_themes", t.map_key)) or "none"
            rows.append([t.theme, " ".join(x for x in (t.heading_pattern, t.quantification) if x), sel])
        b.append(("table", rows))
    b += _figures_blocks(dd.indicators)
    return ItemPack(item_id, "IPO-03", blocks_to_text(b), [source_block(u) for u in units], style)


def group_item_id(group: str) -> str:
    return "RF-" + {"Internal Risks": "Internal", "External Risks": "External",
                    "Risks Relating to the Offer": "Offer"}[group]


def cited_units(dd: DealData, text: str) -> List[Unit]:
    """Company pages cited in a draft text, in order of first citation. Unknown cites raise PackError."""
    out, bad = [], []
    for block in CITE_BLOCK_RX.findall(text):
        for doc, kind, num in CITE_RX.findall(block):
            if doc.startswith("C"):
                continue
            c = f"{doc} {kind}.{num}"
            u = find_unit(dd.units, c)
            if u is None:
                bad.append(c)
            else:
                out.append(u)
    if bad:
        raise PackError("cites that match no company page: " + ", ".join(dict.fromkeys(bad)))
    return _dedupe(out)


def revise_pack(dd: DealData, *, section: str, item_id: str, title: str, group: str, text: str,
                instructions: str, issues: Sequence[str], map_key: Optional[str]) -> ItemPack:
    """Call 4 (IPO-04 rules): revise one item. Pages = those it cites + those selected for it."""
    if section not in ("our_business", "risk_factors"):
        raise PackError(f"unknown section {section!r}")
    kind = "our_business" if section == "our_business" else "risk_themes"
    units = _dedupe(_statement_units(dd) + cited_units(dd, text) + selected_units(dd, kind, map_key))
    head = f"{item_id} {('[' + group + '] ') if group else ''}{title}"
    b = _header("Revise one subsection", f"REV-{item_id}", dd.meta)
    b.append(("h2", f"Subsection to revise: {head}"))
    b.append(("kv", ("Section", "Risk Factors" if section == "risk_factors" else "Our Business")))
    b.append(("h2", "REVISION INSTRUCTIONS"))
    b.append(("p", instructions.strip() or "Fix the problems listed below; otherwise keep the text."))
    if issues:
        b.append(("h2", "Problems found by automated checks"))
        b.append(("bullets", list(issues)))
    b.append(("h2", "CURRENT TEXT"))
    b.append(("p", text.strip()))
    b.append(("p", f"Use the heading line exactly: ### {head}"))
    b += _figures_blocks(dd.indicators)
    return ItemPack(f"REV-{item_id}", "IPO-04", blocks_to_text(b), [source_block(u) for u in units], [])


__all__ = ["PageBlock", "ItemPack", "DealData", "load_deal_data", "blueprint_pack", "ob_pack", "rf_group_pack",
           "revise_pack", "count_tokens", "call_tokens", "check_budget", "ItemTooLarge", "PackError",
           "NoPagesSelected", "RF_NUMBER_RANGE", "group_item_id", "selected_units", "cited_units", "blocks_to_text"]
