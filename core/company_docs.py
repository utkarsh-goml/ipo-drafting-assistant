"""Company documents (annual report, financials, presentations, notes) -> citable page units.

A *unit* is one printed page. Landscape spreads (two printed pages on one PDF page,
common in annual reports) are split into left/right halves. Each unit gets a citation
label like "D1 p.45" (printed page number when it can be read, else a running number),
plus the PDF page and side so the banker can find it and packs can crop it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Set, Union

from pydantic import BaseModel

from .pdf_reader import CACHE_DIR, clean_line, file_hash

SPREAD_RATIO = 1.2  # width / height above which a PDF page is treated as a two-page spread


class Unit(BaseModel):
    doc_id: str
    unit_no: int                  # 1-based running number within the document
    pdf_page: int                 # 1-based PDF page
    side: str = ""                # "L" / "R" for spread halves, "" for whole pages
    printed_page: Optional[int] = None
    part: str = "Other"           # annual-report part (Directors' Report, MD&A, ...)
    lines: List[str]

    @property
    def page_label(self) -> str:
        """Printed page number, or 'u<n>' for pages without one (covers etc.), so a
        label can never collide with a real printed page."""
        return str(self.printed_page) if self.printed_page else f"u{self.unit_no}"

    @property
    def cite(self) -> str:
        if self.printed_page:
            return f"{self.doc_id} p.{self.printed_page}"
        return f"{self.doc_id} u.{self.unit_no}"

    @property
    def location(self) -> str:
        half = {"L": " (left half)", "R": " (right half)"}.get(self.side, "")
        return f"PDF page {self.pdf_page}{half}"

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _lines(text: str) -> List[str]:
    return [c for c in (clean_line(l) for l in (text or "").splitlines()) if c]


def _printed_number(lines: List[str]) -> Optional[int]:
    """Page number printed at the top or bottom of a page (first/last 3 lines)."""
    for cand in lines[-3:][::-1] + lines[:3]:
        m = re.fullmatch(r"(\d{1,3})", cand)
        if m:
            return int(m.group(1))
        # footers like "45 | Annual Report 2024-25" or "Annual Report 2024-25 | 45"
        m = re.fullmatch(r"(\d{1,3})\s*[|•]\s*.+|.+\s*[|•]\s*(\d{1,3})", cand)
        if m:
            return int(m.group(1) or m.group(2))
    return None


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "company_docs.yaml"
DOC_TYPE_PART = {"INVESTOR_PPT": "Investor Presentation", "MGMT_QA": "Management Q&A",
                 "OTHER": "Other Company Document"}
_DROP_CAP = re.compile(r"(?<![A-Za-z])([B-HJ-Z]) ([a-z]{2,})")
_SHORT_WORDS = {"of", "to", "is", "in", "on", "at", "an", "and", "the", "for", "or", "as", "by", "be",
                "are", "was", "has", "shall", "will", "with", "from", "under", "above", "below"}


def fix_drop_caps(line: str) -> str:
    """Re-join decorative drop caps split from their word: 'F inance cost' -> 'Finance cost'.

    Only where a paragraph starts: line start, after '(' or after a numbering token such as
    '6.1.3.' / '30.'. Never for A / I (real one-letter words) or before short function words,
    so 'Annexure B to' and 'Category B investors' stay intact.
    """
    def repl(m: re.Match) -> str:
        before = line[:m.start()].rstrip()
        para_start = (not before or before.endswith("(")
                      or re.search(r"(^|\s)(\d+(\.\d+)*\.?|[a-z]\)|\([a-z]{1,4}\))$", before) is not None)
        if para_start and m.group(2) not in _SHORT_WORDS:
            return m.group(1) + m.group(2)
        return m.group(0)

    return _DROP_CAP.sub(repl, line)


def load_config(path: Path = CONFIG_PATH) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def strip_running_headers(units: List["Unit"], min_share: float = 0.3) -> None:
    """Remove lines repeated at the top/bottom of many pages (running headers/footers)
    and bare page numbers."""
    from collections import Counter

    edge = Counter()
    for u in units:
        for l in set(u.lines[:2] + u.lines[-2:]):
            edge[l.lower()] += 1
    limit = max(3, int(len(units) * min_share))
    running = {l for l, c in edge.items() if c >= limit}
    for u in units:
        u.lines = [l for l in u.lines
                   if l.lower() not in running and not re.fullmatch(r"\d{1,3}", l)]


def tag_parts(units: List["Unit"], cfg: Optional[dict] = None) -> None:
    """Assign annual-report parts from headings at the top of each unit (carried forward)."""
    cfg = cfg or load_config()
    rules = [(p["name"], re.compile(p["pattern"], re.I)) for p in cfg["parts"]]
    max_chars = cfg.get("heading_max_chars", 90)
    current = "Corporate Overview"
    for u in units:
        for line in u.lines[:5]:
            line = line.strip()
            if len(line) > max_chars:
                continue
            hit = next((name for name, rx in rules if rx.search(line)), None)
            if hit:
                # an auditor's report on consolidated statements belongs with them
                if hit == "Auditor's Report" and "consolidated" in " ".join(u.lines[:12]).lower():
                    hit = "Auditor's Report - Consolidated"
                current = hit
                break
        u.part = current


def read_units(path: Union[str, Path], doc_id: str, doc_type: str = "ANNUAL_REPORT") -> List[Unit]:
    """Read a company document into clean, part-tagged printed-page units."""
    units = _read_raw_units(path, doc_id)
    for u in units:
        u.lines = [fix_drop_caps(l) for l in u.lines]
    # tag parts BEFORE stripping running headers: a heading such as "Notes to the
    # Standalone Financial Statements" repeats on every notes page and would be stripped
    if doc_type in DOC_TYPE_PART:
        for u in units:
            u.part = DOC_TYPE_PART[doc_type]
    else:
        tag_parts(units)
    strip_running_headers(units)
    return units


def part_ranges(units: List[Unit]) -> List[dict]:
    """Contiguous runs of the same part: [{part, first, last, pages}] using page labels."""
    out: List[dict] = []
    for u in units:
        if out and out[-1]["part"] == u.part and out[-1]["doc_id"] == u.doc_id:
            out[-1]["last"] = u.page_label
            out[-1]["pages"] += 1
        else:
            out.append({"doc_id": u.doc_id, "part": u.part, "first": u.page_label,
                        "last": u.page_label, "pages": 1})
    return out


def _read_raw_units(path: Union[str, Path], doc_id: str) -> List[Unit]:
    """Read a company PDF into printed-page units (raw text cached on disk by file hash)."""
    import pdfplumber

    path = Path(path)
    cache = CACHE_DIR / f"{file_hash(path)}.units.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = []
        with pdfplumber.open(str(path)) as pdf:
            for i, pg in enumerate(pdf.pages, start=1):
                if pg.width / pg.height > SPREAD_RATIO:
                    mid = pg.width / 2
                    halves = [("L", pg.crop((0, 0, mid, pg.height))),
                              ("R", pg.crop((mid, 0, pg.width, pg.height)))]
                else:
                    halves = [("", pg)]
                for side, part in halves:
                    try:
                        text = part.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                    except Exception:
                        text = ""
                    raw.append({"pdf_page": i, "side": side, "lines": _lines(text)})
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    units = [Unit(doc_id=doc_id, unit_no=n, pdf_page=r["pdf_page"], side=r["side"],
                  printed_page=_printed_number(r["lines"]), lines=r["lines"])
             for n, r in enumerate(raw, start=1)]
    _fill_printed_numbers(units)
    _dedupe_printed_numbers(units)
    return units


def _fill_printed_numbers(units: List[Unit]) -> None:
    """Infer missing printed page numbers from the dominant (unit_no -> printed) offset.

    Printed numbers that disagree with the dominant offset (e.g. a figure '12' read as
    a page number) are replaced too.
    """
    from collections import Counter

    diffs = Counter(u.printed_page - u.unit_no for u in units if u.printed_page)
    if not diffs:
        return
    offset, support = diffs.most_common(1)[0]
    if support < max(3, len(units) // 10):
        return
    for u in units:
        expected = u.unit_no + offset
        u.printed_page = expected if expected >= 1 else None


def _dedupe_printed_numbers(units: List[Unit]) -> None:
    """Keep each printed page number on one unit only (e.g. numbering restarting in an
    appendix); repeats fall back to 'u<n>' labels so citations stay unambiguous."""
    seen: Set[int] = set()
    for u in units:
        if u.printed_page in seen:
            u.printed_page = None
        elif u.printed_page:
            seen.add(u.printed_page)


def find_unit(units: List[Unit], label: str) -> Optional[Unit]:
    """Resolve a citation such as 'D1 p.45', '[D1 p.45]' or 'D1 u.2' to its unit."""
    m = re.search(r"(D\d+)\s*([pu])\.?\s*(\d+)", label)
    if not m:
        return None
    doc, kind, num = m.group(1), m.group(2), int(m.group(3))
    if kind == "u":
        return next((u for u in units if u.doc_id == doc and u.unit_no == num), None)
    return next((u for u in units if u.doc_id == doc and u.printed_page == num), None)
