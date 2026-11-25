"""Risk triggers: facts in the company documents that call for a specific risk factor.

Two kinds:
  * financial  - computed from the extracted figures (negative cash flow, losses, decline,
                 leverage)
  * textual    - phrases found in the documents (contingent liabilities, related-party
                 transactions, litigation, concentration, attrition, auditor remarks...)

Each trigger carries evidence (citation + snippet). Triggers marked `mandatory` are the
ones SEBI ICDR practice expects to be disclosed whenever the fact exists; the banker can
deselect any trigger in the UI before drafting.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from .company_docs import Unit
from .financials import Indicators


class Evidence(BaseModel):
    cite: str
    location: str
    snippet: str


class Trigger(BaseModel):
    key: str
    title: str                     # suggested risk-factor theme
    category: str                  # Internal | External | Offer
    mandatory: bool
    reason: str                    # why it fired
    evidence: List[Evidence] = Field(default_factory=list)
    selected: bool = True


# key, title, category, mandatory, regex, parts filter (None = any), min hits
TEXT_RULES: List[Tuple[str, str, str, bool, str, Optional[str], int]] = [
    ("contingent_liabilities", "Contingent liabilities could adversely affect our financial condition",
     "Internal", True, r"contingent liabilit", "Notes", 1),
    ("related_party", "We have entered into related party transactions and may continue to do so",
     "Internal", True, r"related party transactions?|transactions with related parties", None, 1),
    ("litigation", "Outstanding legal proceedings involving the Company",
     "Internal", True, r"litigation|legal proceedings|show cause notice|pending (cases|proceedings)|"
                       r"material orders? (passed )?by (the )?regulators?", None, 1),
    ("regulatory_action", "Regulatory inspections, penalties or adverse orders",
     "Internal", False, r"penalt(y|ies) (was |were )?(imposed|levied)|adjudication|administrative warning|"
                        r"deficiency letter|settlement (order|application)", None, 1),
    ("concentration", "Dependence on a limited number of customers / products / channels",
     "Internal", True, r"top (ten|10|five|5|twenty|20) (customers|clients|distributors|suppliers|schemes)|"
                       r"single largest (customer|client)|significant portion of (our )?revenue", None, 1),
    ("attrition", "Inability to attract and retain key personnel; employee attrition",
     "Internal", False, r"attrition", None, 1),
    ("brand_parent", "Dependence on the brand and reputation of our Promoters / parent group",
     "Internal", False, r"joint venture between|brand (name )?(of|owned by)|licen[cs]e to use the (brand|trade ?mark)|"
                        r"holding company|promoter", None, 2),
    ("auditor_qualification", "Auditor qualifications, adverse remarks or emphasis of matter",
     "Internal", True, r"qualified opinion|adverse opinion|emphasis of matter|basis for qualified|disclaimer of opinion",
     "Auditor", 1),
    ("revised_financials", "Our financial statements have been revised / restated",
     "Internal", False, r"revised (independent )?auditor|revised financial statements|\(revised\)", None, 1),
    ("unspent_csr", "Unspent CSR obligations", "Internal", False,
     r"unspent (csr|corporate social responsibility)( amount)?", None, 1),
    ("statutory_dues", "Delays in payment of statutory dues", "Internal", True,
     r"(delay|arrears) (in )?(deposit|payment) of (undisputed )?statutory dues|statutory dues .{0,40}outstanding for a period of more than six months", None, 1),
    ("cyber", "Cyber-attacks, data breaches and IT system failures", "Internal", False,
     r"cyber ?(security|attack|incident)|data (breach|privacy)|information security", None, 1),
    ("regulated_industry", "We operate in a highly regulated industry", "External", False,
     r"\b(sebi|rbi|irdai|pfrda|trai|cdsco|fssai)\b", None, 5),
]


def _snippet(lines: List[str], i: int, width: int = 1) -> str:
    s = " ".join(lines[max(0, i - width): i + width + 1])
    return (s[:280] + "…") if len(s) > 280 else s


def text_evidence(units: List[Unit], pattern: str, part_filter: Optional[str] = None,
                  max_hits: int = 4) -> List[Evidence]:
    rx = re.compile(pattern, re.I)
    out, seen = [], set()
    for u in units:
        if part_filter and part_filter not in u.part:
            continue
        for i, line in enumerate(u.lines):
            if rx.search(line) and u.cite not in seen:
                out.append(Evidence(cite=u.cite, location=f"{u.location} · {u.part}",
                                    snippet=_snippet(u.lines, i)))
                seen.add(u.cite)
                break
        if len(out) >= max_hits:
            break
    return out


def _count_pages(units: List[Unit], pattern: str) -> int:
    rx = re.compile(pattern, re.I)
    return sum(1 for u in units if rx.search(u.text))


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:,.2f}"


def financial_triggers(ind: Indicators) -> List[Trigger]:
    out: List[Trigger] = []
    yrs = ind.years or ["Current year", "Previous year"]
    unit = f" (₹ in {ind.unit})" if ind.unit else ""

    def ev(key):
        f = ind.figures.get(key)
        return [Evidence(cite=f.cite, location=f.label, snippet=f.source_line)] if f else []

    for i, yr in enumerate(yrs[:2]):
        if (ind.value("ocf", i) or 0) < 0:
            out.append(Trigger(key=f"negative_ocf_{yr}", title="We have had negative cash flows from operating activities",
                               category="Internal", mandatory=True,
                               reason=f"Net cash from operating activities {yr}: {_fmt(ind.value('ocf', i))}{unit}",
                               evidence=ev("ocf")))
        if (ind.value("pat", i) or 0) < 0:
            out.append(Trigger(key=f"loss_{yr}", title="We have incurred losses in the past",
                               category="Internal", mandatory=True,
                               reason=f"Loss for {yr}: {_fmt(ind.value('pat', i))}{unit}", evidence=ev("pat")))
    for key, name in (("revenue_ops", "revenue from operations"), ("pat", "profit after tax")):
        g = ind.derived.get(f"{key}_growth", [None])[0]
        if g is not None and g < 0:
            out.append(Trigger(key=f"{key}_decline", title=f"Our {name} declined in the latest year",
                               category="Internal", mandatory=False,
                               reason=f"{name.capitalize()} change y-o-y: {g:.1%}", evidence=ev(key)))
    de = ind.derived.get("debt_equity", [None])[0]
    if de is not None and de > 1:
        out.append(Trigger(key="high_leverage", title="Our indebtedness and debt covenants",
                           category="Internal", mandatory=True,
                           reason=f"Debt to equity {de:.2f}x", evidence=ev("borrowings")))
    return out


def find_triggers(units: List[Unit], ind: Indicators) -> List[Trigger]:
    triggers = financial_triggers(ind)
    for key, title, cat, mandatory, pattern, part, min_hits in TEXT_RULES:
        scope = [u for u in units if not part or part in u.part]
        pages = _count_pages(scope, pattern)
        if pages < min_hits:
            continue
        evidence = text_evidence(scope, pattern)
        if not evidence:
            continue
        shown = f" (first {len(evidence)} shown)" if pages > len(evidence) else ""
        triggers.append(Trigger(key=key, title=title, category=cat, mandatory=mandatory,
                                reason=f"Mentioned on {pages} page(s){shown}", evidence=evidence))
    # Offer-related risks always apply to an IPO
    for key, title in (("offer_price", "The Offer Price may not be indicative of the market price after listing"),
                       ("no_prior_market", "There has been no prior public market for our Equity Shares"),
                       ("ofs_proceeds", "The Company may not receive proceeds from the Offer for Sale portion")):
        triggers.append(Trigger(key=key, title=title, category="Offer", mandatory=True,
                                reason="Standard offer-related risk for an IPO"))
    return triggers
