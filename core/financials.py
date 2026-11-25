"""Key financial figures from the statements in company documents + derived ratios.

Reads rows like "X. Profit / (loss) for the period (VII+VIII-IX) 2,54,015.32 2,07,278.35"
from the Statement of Profit and Loss, Balance Sheet and Cash Flow Statement
(consolidated preferred, standalone as fallback). Every figure keeps its source page
so the LLM gets ready-made, citable numbers instead of doing arithmetic itself.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .company_docs import Unit

# (key, display label, statement, [compact-label regexes in priority order])
SPECS: List[Tuple[str, str, str, List[str]]] = [
    ("revenue_ops", "Revenue from operations", "pl", [r"^totalrevenuefromoperations", r"^revenuefromoperations$", r"^netrevenuefromoperations"]),
    ("other_income", "Other income", "pl", [r"^otherincome$"]),
    ("total_income", "Total income", "pl", [r"^totalincome$", r"^totalrevenue$"]),
    ("employee_exp", "Employee benefits expense", "pl", [r"^employeebenefits?expenses?$"]),
    ("finance_cost", "Finance cost", "pl", [r"^financecosts?$"]),
    ("depreciation", "Depreciation and amortisation", "pl", [r"^depreciation"]),
    ("total_expenses", "Total expenses", "pl", [r"^totalexpenses$"]),
    ("pbt", "Profit before tax", "pl", [r"^profit(loss)?beforetax$", r"^(net)?profit(loss)?beforetax"]),
    ("pat", "Profit for the year (PAT)", "pl", [r"^(net)?profit(loss)?for(the)?(year|period)$", r"^(net)?profit(loss)?for(the)?(year|period)"]),
    ("eps_basic", "Basic EPS", "pl", [r"^basic"]),
    ("total_assets", "Total assets", "bs", [r"^totalassets$"]),
    ("total_equity", "Total equity", "bs", [r"^totalequity$", r"^subtotalequity$", r"^totalequityattributabletoownersof",
                                            r"^totalshareholdersfunds?$"]),
    ("share_capital", "Equity share capital", "bs", [r"^equitysharecapital$", r"^sharecapital$"]),
    ("borrowings", "Borrowings", "bs", [r"^borrowings", r"^debtsecurities"]),
    ("trade_receivables", "Trade receivables", "bs", [r"^tradereceivables"]),
    ("cash", "Cash and cash equivalents", "bs", [r"^cashandcashequivalents$"]),
    ("ocf", "Net cash from operating activities", "cf", [r"^netcash.*operatingactivities"]),
    ("icf", "Net cash from investing activities", "cf", [r"^netcash.*investingactivities"]),
    ("fcf_fin", "Net cash from financing activities", "cf", [r"^netcash.*financingactivities"]),
]

STATEMENT_TITLES = {
    "pl": re.compile(r"statement of profit (and|&) loss|profit (and|&) loss (account|statement)", re.I),
    "bs": re.compile(r"balance sheet", re.I),
    "cf": re.compile(r"statement of cash flows?|cash flow statement", re.I),
}
AMOUNT_TOKEN = re.compile(r"^\(?-?[\d,]*\d(\.\d+)?\)?$|^-$|^–$|^nil$", re.I)
UNIT_RX = re.compile(r"in\s+(lakhs?|lacs?|crores?|millions?|billions?|thousands?)", re.I)
YEAR_RX = re.compile(r"(?:March|Mar|December|Dec|June|Jun|September|Sept?)\.?\s*3[01],?\s*(\d{4})"
                     r"|3[01](?:st)?\s+(?:March|December|June|September),?\s*(\d{4})", re.I)
FORMULA_RX = re.compile(r"\((?:[IVXLC]+\s*[+\-]\s*)+[IVXLC]+\)|\((?:[A-Z]\s*[+\-]\s*)+[A-Z]\)")
LEAD_RX = re.compile(r"^\s*(?:\(?[ivxlcIVXLC]{1,5}[.)]|\([a-zA-Z]{1,3}\)|[a-zA-Z][.)]|\d{1,2}[.)])\s+")


class Figure(BaseModel):
    key: str
    label: str
    values: List[Optional[float]]           # [current, previous]
    cite: str                               # "D1 p.145"
    source_line: str


class Indicators(BaseModel):
    basis: str = ""                          # consolidated | standalone
    unit: str = ""                           # lakhs | crores | millions ...
    years: List[str] = Field(default_factory=list)   # ["FY2025", "FY2024"]
    figures: Dict[str, Figure] = Field(default_factory=dict)
    derived: Dict[str, List[Optional[float]]] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)

    def value(self, key: str, i: int = 0) -> Optional[float]:
        f = self.figures.get(key)
        return f.values[i] if f and len(f.values) > i else None


def parse_amount(tok: str) -> Optional[float]:
    t = tok.strip()
    if t in {"-", "–"} or t.lower() == "nil":
        return 0.0
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = t.strip("()-").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _looks_formatted(t: str) -> bool:
    return "," in t or "." in t or t in {"-", "–"} or t.lower() == "nil"


def split_row(line: str, n_cols: int = 2) -> Tuple[str, List[float]]:
    """'(iii) Employee benefits expenses 22 42,107.78 36,838.76' -> (label, [42107.78, 36838.76]).

    Trailing numeric tokens are collected from the right. The last `n_cols` of them are
    the year columns (by position, so unformatted amounts like '860' are kept); anything
    before them is the note-number column. With fewer tokens than columns, a leading
    short unformatted integer followed by a formatted amount is treated as a note.
    """
    toks = line.split()
    tail: List[str] = []
    while toks and AMOUNT_TOKEN.match(toks[-1]):
        tail.insert(0, toks.pop())
    if len(tail) > n_cols:
        values_tok = tail[-n_cols:]
    elif len(tail) >= 2 and not _looks_formatted(tail[0]) and len(tail[0]) <= 3 \
            and _looks_formatted(tail[1]):
        values_tok = tail[1:]                                   # 'Other income 21 17,282.78'
    else:
        values_tok = tail
    label = " ".join(toks)
    label = re.sub(r"\s\d{1,2}[A-Z]?(-\d{1,2}[A-Z]?)?$", "", label)  # note column like '9-11', '6A'
    values = [parse_amount(t) for t in values_tok]
    return label, [v for v in values if v is not None]


def compact(label: str) -> str:
    s = FORMULA_RX.sub("", label)
    s = LEAD_RX.sub("", s)
    return re.sub(r"[^a-z]", "", s.lower().replace("&", "and"))


def _statement_units(units: List[Unit], basis: str) -> Dict[str, List[Unit]]:
    want = "Consolidated" if basis == "consolidated" else "Standalone"
    out: Dict[str, List[Unit]] = {"pl": [], "bs": [], "cf": []}
    for u in units:
        if not u.part.startswith("Financial Statements") or want not in u.part:
            continue
        head = " ".join(u.lines[:4])
        for kind, rx in STATEMENT_TITLES.items():
            if rx.search(head):
                out[kind].append(u)
                break
    return out


def extract_indicators(units: List[Unit]) -> Indicators:
    """Consolidated figures if consolidated statements exist, else standalone."""
    for basis in ("consolidated", "standalone"):
        stmts = _statement_units(units, basis)
        if stmts["pl"] or stmts["bs"]:
            break
    else:
        return Indicators(notes=["No financial statements found. Enter key figures manually."])

    ind = Indicators(basis=basis)
    sample = " ".join(" ".join(u.lines[:10]) for kind in stmts for u in stmts[kind][:1])
    m = UNIT_RX.search(sample)
    ind.unit = m.group(1).lower().rstrip("s") + "s" if m else ""
    years = []
    for a, b in YEAR_RX.findall(sample):
        y = a or b
        if f"FY{y}" not in years:
            years.append(f"FY{y}")
    years = sorted(years, reverse=True)
    n_cols = min(max(len(years), 2), 4)        # 2 for annual reports, 3 for restated statements
    ind.years = years[:2]
    if len(years) > 2:
        ind.notes.append(f"{len(years)} year columns found; using the latest two ({', '.join(years[:2])}).")

    for key, label, kind, patterns in SPECS:
        found = None
        for pat in patterns:
            rx = re.compile(pat)
            for u in stmts[kind]:
                for line in u.lines:
                    lab, vals = split_row(line, n_cols)
                    if len(vals) >= 1 and rx.search(compact(lab)):
                        # columns run latest -> oldest; keep the latest two
                        found = Figure(key=key, label=label, values=(vals[:2] if len(vals) >= 2 else vals + [None]),
                                       cite=u.cite, source_line=line)
                        break
                if found:
                    break
            if found:
                break
        if found:
            ind.figures[key] = found
    missing = [label for key, label, _, _ in SPECS if key not in ind.figures]
    if missing:
        ind.notes.append("Not found (enter manually if relevant): " + ", ".join(missing))
    compute_derived(ind)
    return ind


def _ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def compute_derived(ind: Indicators) -> None:
    d: Dict[str, List[Optional[float]]] = {}
    v = ind.value
    for i in (0, 1):
        ebitda = None
        if v("pbt", i) is not None:
            ebitda = (v("pbt", i) + (v("finance_cost", i) or 0) + (v("depreciation", i) or 0)
                      - (v("other_income", i) or 0))
        d.setdefault("ebitda", []).append(ebitda)
        d.setdefault("ebitda_margin", []).append(_ratio(ebitda, v("revenue_ops", i)))
        d.setdefault("pat_margin", []).append(_ratio(v("pat", i), v("total_income", i)))
        d.setdefault("roe", []).append(_ratio(v("pat", i), v("total_equity", i)))
        d.setdefault("debt_equity", []).append(_ratio(v("borrowings", i), v("total_equity", i)))
        d.setdefault("ocf_to_pat", []).append(_ratio(v("ocf", i), v("pat", i)))
    for key in ("revenue_ops", "total_income", "pat"):
        cur, prev = v(key, 0), v(key, 1)
        d[f"{key}_growth"] = [_ratio(cur - prev, abs(prev)) if cur is not None and prev else None, None]
    ind.derived = d


DERIVED_LABELS = {
    "ebitda": ("EBITDA (PBT + finance cost + D&A − other income)", "amount"),
    "ebitda_margin": ("EBITDA margin (on revenue from operations)", "pct"),
    "pat_margin": ("PAT margin (on total income)", "pct"),
    "roe": ("Return on equity (PAT / closing equity)", "pct"),
    "debt_equity": ("Debt to equity", "x"),
    "ocf_to_pat": ("Operating cash flow / PAT", "x"),
    "revenue_ops_growth": ("Revenue from operations growth (y-o-y)", "pct"),
    "total_income_growth": ("Total income growth (y-o-y)", "pct"),
    "pat_growth": ("PAT growth (y-o-y)", "pct"),
}
