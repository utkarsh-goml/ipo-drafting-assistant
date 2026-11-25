"""Extract standard sections (Our Business, Risk Factors) from DRHP / RHP PDFs.

Strategy (first one that works wins):
  1. Table of contents  - TOC printed pages + printed->PDF page offset, then the
                          bold capital heading is confirmed near the predicted page
  2. PDF bookmarks      - outline entry for the section, next outline entry = end
  3. Heading scan      - first page whose top lines are the section heading;
                          end = first later page headed by a known next section

The start/end are reported as (PDF page, line). The end is the line just before the
next section's heading, so a section that ends mid-page is cut correctly.

All matching logic works on `pages: List[List[str]]` (lines per page) so it can be
unit-tested without PDFs.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

import yaml
from rapidfuzz import fuzz

from . import pdf_reader
from .models import ExtractedSection, PageText, SubHeading, TocEntry

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "sections.yaml"

PART_PREFIX = re.compile(r"^\s*SECTION\s+[IVXLC\d]+\s*[:.\-]?\s*", re.I)
TOC_DOTTED = re.compile(r"^(?P<title>.*?[A-Za-z].*?)\s*(?:\.\s*){3,}\s*(?P<page>\d{1,4})\s*$")
TOC_PLAIN = re.compile(r"^(?P<title>[A-Z][A-Z0-9 ,&'\-:()/]{3,}?)\s+(?P<page>\d{1,4})$")
PAGE_NO = re.compile(r"^(\d{1,4}|[ivxlcdm]{1,7})$", re.I)
TOC_TITLES = {"TABLE OF CONTENTS", "CONTENTS", "INDEX"}


# --------------------------------------------------------------------------- helpers
def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().upper().strip(" .:-")


def heading_key(s: str) -> str:
    """Normalised heading without a 'SECTION II:' style prefix."""
    return norm(PART_PREFIX.sub("", norm(s)))


def is_page_number(line: str) -> bool:
    return bool(PAGE_NO.match(line.strip()))


def _mostly_upper(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.8


def heading_score(line: str, keys: List[str]) -> float:
    k = heading_key(line)
    return max((fuzz.ratio(k, a) for a in keys), default=0.0)


# --------------------------------------------------------------------------- TOC
def _toc_match(line: str) -> Optional[re.Match]:
    return TOC_DOTTED.match(line) or TOC_PLAIN.match(line)


def find_toc_pages(pages: List[List[str]], scan: int = 25) -> List[int]:
    """0-based indices of the table-of-contents pages."""
    start = None
    for i in range(min(scan, len(pages))):
        if any(norm(l) in TOC_TITLES for l in pages[i][:8]):
            start = i
            break
    if start is None:  # no title: take the first page with many TOC-like lines
        for i in range(min(scan, len(pages))):
            if sum(bool(TOC_DOTTED.match(l)) for l in pages[i]) >= 8:
                start = i
                break
    if start is None:
        return []
    found = [start]
    j = start + 1
    while j < len(pages) and sum(bool(TOC_DOTTED.match(l)) for l in pages[j]) >= 5:
        found.append(j)
        j += 1
    return found


def parse_toc(pages: List[List[str]], toc_pages: List[int]) -> List[TocEntry]:
    """Parse 'TITLE ...... 155' lines; titles wrapped over two lines are joined."""
    entries: List[TocEntry] = []
    pending: List[str] = []
    for idx in toc_pages:
        for line in pages[idx]:
            if norm(line) in TOC_TITLES:
                continue
            m = _toc_match(line)
            if m:
                title = norm(" ".join(pending + [m.group("title")]))
                pending = []
                entries.append(TocEntry(title=title, printed_page=int(m.group("page")),
                                        is_part=bool(PART_PREFIX.match(title))))
            elif _mostly_upper(line) and len(line) < 160 and not is_page_number(line):
                pending = (pending + [line])[-2:]
            else:
                pending = []
    return entries


def search_pages(pages: List[List[str]], query: str, headings_only: bool = False,
                 max_hits: int = 200) -> List[dict]:
    """Lines containing `query` (case and spacing ignored), in page order.

    Each hit: {"page": 1-based PDF page, "printed": printed page or None, "line": 1-based line,
    "text": the line, "heading": True if the line looks like a capital-letter heading}.
    `headings_only` keeps only heading-like lines (useful to find where a section starts)."""
    q = re.sub(r"\s+", " ", query or "").strip().lower()
    if len(q) < 2:
        return []
    offset, _ = page_offset(pages)
    printed = printed_page_numbers(pages)
    hits: List[dict] = []
    for pi, lines in enumerate(pages):
        for li, line in enumerate(lines):
            text = re.sub(r"\s+", " ", line).strip()
            if q not in text.lower():
                continue
            heading = _mostly_upper(text) and len(text) <= 120
            if headings_only and not heading:
                continue
            p = pi + 1
            hits.append({"page": p, "printed": (p - offset) if offset is not None and p - offset > 0
                         else printed[pi], "line": li + 1, "text": text, "heading": heading})
            if len(hits) >= max_hits:
                return hits
    return hits


def printed_page_numbers(pages: List[List[str]]) -> List[Optional[int]]:
    """Printed (footer/header) page number of each page, when it can be read."""
    out: List[Optional[int]] = []
    for lines in pages:
        num = None
        for cand in (lines[:2] + lines[-2:]) if lines else []:
            if cand.isdigit() and len(cand) <= 4:
                num = int(cand)
                break
        out.append(num)
    return out


def page_offset(pages: List[List[str]]) -> Tuple[Optional[int], int]:
    """Most common (PDF page - printed page). Returns (offset, number of supporting pages)."""
    diffs = Counter((i + 1) - p for i, p in enumerate(printed_page_numbers(pages)) if p is not None)
    if not diffs:
        return None, 0
    offset, support = diffs.most_common(1)[0]
    if support < max(5, len(pages) // 20):
        return None, support
    return offset, support


# --------------------------------------------------------------------------- heading location
def find_heading_on_page(lines: List[str], keys: List[str], min_score: float,
                         top_n: Optional[int]) -> Optional[Tuple[int, float]]:
    """(0-based line index, score) of a heading line on a page.

    Within the top `top_n` lines any whole-line match counts. With top_n=None the
    whole page is searched, but only upper-case lines are accepted, so that
    cross-references inside sentences ("see 'Our Business' on page 155") never match.
    """
    limit = len(lines) if top_n is None else min(top_n, len(lines))
    for i in range(limit):
        line = lines[i]
        if is_page_number(line):
            continue
        if top_n is None and not _mostly_upper(line):
            continue
        cands = [line]
        if i + 1 < len(lines):
            cands.append(line + " " + lines[i + 1])  # heading wrapped over two lines
        best = max(heading_score(c, keys) for c in cands)
        if best >= min_score:
            return i, best
    return None


def locate_heading(pages: List[List[str]], keys: List[str], cfg: dict,
                   predicted: Optional[int] = None, search_from: int = 0,
                   search_to: Optional[int] = None) -> Optional[Tuple[int, int, float]]:
    """Find a heading. Returns (0-based page, 0-based line, score).

    With `predicted` (0-based), pages within +/- verify_window are tried nearest-first.
    Otherwise pages are scanned in order from `search_from`.
    """
    m = cfg["match"]
    min_score, top_n = m["heading_min_score"], m["top_lines"]
    if predicted is not None:
        w = m["verify_window"]
        order = sorted(range(max(0, predicted - w), min(len(pages), predicted + w + 1)),
                       key=lambda p: abs(p - predicted))
        for mode in (top_n, None):
            for p in order:
                hit = find_heading_on_page(pages[p], keys, min_score, mode)
                if hit:
                    return p, hit[0], hit[1]
        return None
    stop = len(pages) if search_to is None else min(search_to, len(pages))
    for p in range(search_from, stop):
        hit = find_heading_on_page(pages[p], keys, min_score, top_n)
        if hit:
            return p, hit[0], hit[1]
    return None


BoldFn = Callable[[int], List[str]]     # 1-based page -> bold capital lines on it


def locate_bold_heading(pages: List[List[str]], keys: List[str], cfg: dict, predicted: int,
                        bold: Optional[BoldFn]) -> Optional[Tuple[int, int, float, bool]]:
    """Find a section heading near the TOC-predicted page (0-based).

    Rule from DRHP practice: a section starts with its title as a BOLD, CAPITAL-letter heading.
    Pages within +/- verify_window are tried nearest-first; the first one with a matching bold
    capital line wins. Falls back to a text-only match. Returns (page, line, score, verified)."""
    m = cfg["match"]
    min_score, w = m["heading_min_score"], m["verify_window"]
    order = sorted(range(max(0, predicted - w), min(len(pages), predicted + w + 1)),
                   key=lambda p: abs(p - predicted))
    if bold:
        for p in order:
            heads = [h for h in bold(p + 1) if heading_score(h, keys) >= min_score]
            if not heads:
                continue
            hit = (find_heading_on_page(pages[p], keys, min_score, m["top_lines"])
                   or find_heading_on_page(pages[p], keys, min_score, None))
            if hit:
                return p, hit[0], hit[1], True
            return p, 0, heading_score(heads[0], keys), True
    hit = locate_heading(pages, keys, cfg, predicted=predicted)
    return (hit[0], hit[1], hit[2], False) if hit else None


def end_before(pages: List[List[str]], next_page: int, next_line: int) -> Tuple[int, int]:
    """(1-based page, 1-based line) of the last line before a heading at (0-based page, line)."""
    before = pages[next_page][:next_line]
    # only page numbers / upper-case running headers above the heading -> it starts the page
    header_only = next_line <= 3 and all(
        is_page_number(l) or (_mostly_upper(l) and len(l) < 80) for l in before)
    if header_only or next_line == 0:
        prev = next_page - 1
        return prev + 1, max(1, len(pages[prev]))
    return next_page + 1, next_line  # line before heading, 1-based == heading's 0-based index


# --------------------------------------------------------------------------- building
def build_section(pages: List[List[str]], *, doc_id: str, doc_name: str, section_key: str,
                  section_title: str, start_page: int, start_line: int, end_page: int,
                  end_line: int, method: str, confidence: float,
                  next_section_title: Optional[str] = None,
                  notes: Optional[List[str]] = None) -> ExtractedSection:
    """Slice the text between (start_page, start_line) and (end_page, end_line), all 1-based."""
    notes = list(notes or [])
    start_page = max(1, min(start_page, len(pages)))
    end_page = max(1, min(end_page, len(pages)))
    if end_page < start_page:
        notes.append(f"End page {end_page} was before start page {start_page}; end set to start.")
        end_page, end_line = start_page, len(pages[start_page - 1])
    printed = printed_page_numbers(pages)
    out: List[PageText] = []
    for pno in range(start_page, end_page + 1):
        lines = pages[pno - 1]
        lo = start_line - 1 if pno == start_page else 0
        hi = end_line if pno == end_page else len(lines)
        body = lines[lo:hi]
        # drop bare page numbers at the top/bottom of each page
        while body and is_page_number(body[0]):
            body = body[1:]
        while body and is_page_number(body[-1]):
            body = body[:-1]
        out.append(PageText(page_no=pno, printed_page=printed[pno - 1], lines=body))
    return ExtractedSection(
        doc_id=doc_id, doc_name=doc_name, section_key=section_key, section_title=section_title,
        start_page=start_page, start_line=start_line, end_page=end_page, end_line=end_line,
        next_section_title=next_section_title, method=method, confidence=round(confidence, 2),
        notes=notes, pages=out,
    )


def _from_bookmarks(pages, outline, keys, sec_key, sec_cfg, cfg, doc_id, doc_name):
    min_score = cfg["match"]["heading_min_score"]
    for i, (title, pg, depth) in enumerate(outline):
        if heading_score(title, keys) < min_score:
            continue
        nxt = next(((t, p) for t, p, d in outline[i + 1:] if d <= depth and p >= pg
                    and heading_score(t, keys) < min_score), None)
        if not nxt:
            return None
        notes = []
        hit = find_heading_on_page(pages[pg - 1], keys, min_score, cfg["match"]["top_lines"])
        start_line = hit[0] + 1 if hit else 1
        nhit = find_heading_on_page(pages[nxt[1] - 1], [heading_key(nxt[0])], min_score,
                                    cfg["match"]["top_lines"])
        if nhit:
            end_page, end_line = end_before(pages, nxt[1] - 1, nhit[0])
        else:
            end_page, end_line = nxt[1] - 1, len(pages[nxt[1] - 2])
            notes.append(f"Next heading '{nxt[0]}' not found on page {nxt[1]}; ended on previous page.")
        return build_section(pages, doc_id=doc_id, doc_name=doc_name, section_key=sec_key,
                             section_title=sec_cfg["title"], start_page=pg, start_line=start_line,
                             end_page=end_page, end_line=end_line, method="bookmarks",
                             confidence=1.0 if hit else 0.85, next_section_title=nxt[0], notes=notes)
    return None


def _from_toc(pages, toc, toc_pages, keys, sec_key, sec_cfg, cfg, doc_id, doc_name, bold=None):
    min_score = cfg["match"]["heading_min_score"]
    idx = next((i for i, e in enumerate(toc) if heading_score(e.title, keys) >= min_score), None)
    if idx is None:
        return None
    entry = toc[idx]
    nxt = next((e for e in toc[idx + 1:] if e.printed_page >= entry.printed_page
                and heading_score(e.title, keys) < min_score), None)
    notes: List[str] = []
    after_toc = (toc_pages[-1] + 1) if toc_pages else 0

    offset, support = page_offset(pages)
    if offset is None:  # derive offset from where the heading really is
        hit = locate_heading(pages, keys, cfg, search_from=after_toc)
        if not hit:
            return None
        offset = (hit[0] + 1) - entry.printed_page
        notes.append(f"Page offset {offset:+d} derived from the heading position (page numbers unreadable).")
    else:
        notes.append(f"Printed-to-PDF page offset {offset:+d} (confirmed on {support} pages).")

    confidence = 1.0
    start = locate_bold_heading(pages, keys, cfg, entry.printed_page + offset - 1, bold)
    s_ok = e_ok = False
    if start:
        s_page, s_line, s_ok = start[0] + 1, start[1] + 1, start[3]
        if bold and not s_ok:
            confidence -= 0.15
            notes.append(f"No bold capital heading '{sec_cfg['title']}' near PDF page {s_page}; matched by text only.")
    else:
        s_page, s_line = entry.printed_page + offset, 1
        confidence -= 0.25
        notes.append(f"Heading '{sec_cfg['title']}' not confirmed on PDF page {s_page}; used TOC page.")

    if nxt:
        end = locate_bold_heading(pages, [heading_key(nxt.title)], cfg, nxt.printed_page + offset - 1, bold)
        if end:
            e_page, e_line = end_before(pages, end[0], end[1])
            e_ok = end[3]
            if bold and not e_ok:
                confidence -= 0.1
                notes.append(f"Next section '{nxt.title}' not found as a bold capital heading; matched by text only.")
        else:
            e_page = nxt.printed_page + offset - 1
            e_line = len(pages[e_page - 1])
            confidence -= 0.2
            notes.append(f"Next heading '{nxt.title}' not confirmed; ended on PDF page {e_page}.")
        next_title = nxt.title
    else:  # last TOC entry: fall back to configured next headings
        end = locate_heading(pages, [heading_key(h) for h in sec_cfg["next_headings"]], cfg,
                             search_from=s_page)
        if not end:
            return None
        e_page, e_line = end_before(pages, end[0], end[1])
        next_title = " ".join(pages[end[0]][end[1]:end[1] + 1])
    sec = build_section(pages, doc_id=doc_id, doc_name=doc_name, section_key=sec_key,
                        section_title=sec_cfg["title"], start_page=s_page, start_line=s_line,
                        end_page=e_page, end_line=e_line, method="toc", confidence=confidence,
                        next_section_title=next_title, notes=notes)
    sec.start_verified, sec.end_verified = s_ok, e_ok
    return sec


def _from_heading_scan(pages, after_toc, keys, sec_key, sec_cfg, cfg, doc_id, doc_name):
    start = locate_heading(pages, keys, cfg, search_from=after_toc)
    if not start:
        return None
    next_keys = [heading_key(h) for h in sec_cfg["next_headings"]]
    end = locate_heading(pages, next_keys, cfg, search_from=start[0] + 1)
    notes = ["No usable bookmarks or table of contents; found by heading scan. Please verify."]
    if end:
        e_page, e_line = end_before(pages, end[0], end[1])
        next_title = pages[end[0]][end[1]]
    else:
        e_page, e_line, next_title = len(pages), len(pages[-1]), None
        notes.append("Next section heading not found; section runs to the end of the document.")
    return build_section(pages, doc_id=doc_id, doc_name=doc_name, section_key=sec_key,
                         section_title=sec_cfg["title"], start_page=start[0] + 1,
                         start_line=start[1] + 1, end_page=e_page, end_line=e_line,
                         method="heading_scan", confidence=0.6 if end else 0.4,
                         next_section_title=next_title, notes=notes)


def extract_from_pages(pages: List[List[str]], *, doc_id: str, doc_name: str, section_key: str,
                       cfg: Optional[dict] = None,
                       outline: Optional[List[Tuple[str, int, int]]] = None,
                       bold: Optional[BoldFn] = None) -> Optional[ExtractedSection]:
    """Table of contents first (DRHP sections are always listed there), then bookmarks,
    then a heading scan as a last resort."""
    cfg = cfg or load_config()
    sec_cfg = cfg["sections"][section_key]
    keys = [heading_key(a) for a in sec_cfg["aliases"]]
    args = (keys, section_key, sec_cfg, cfg, doc_id, doc_name)
    toc_pages = find_toc_pages(pages, cfg["match"]["toc_scan_pages"])
    toc = parse_toc(pages, toc_pages) if toc_pages else []
    if toc:
        res = _from_toc(pages, toc, toc_pages, *args, bold=bold)
        if res:
            return res
    if outline:
        res = _from_bookmarks(pages, outline, *args)
        if res:
            res.notes.insert(0, "Not found in the table of contents; used PDF bookmarks. Please verify.")
            return res
    after_toc = (toc_pages[-1] + 1) if toc_pages else 0
    return _from_heading_scan(pages, after_toc, *args)


def verify_headings(sec: ExtractedSection, bold: BoldFn, cfg: dict) -> ExtractedSection:
    """Set start/end_verified for sections found by other methods or set manually."""
    min_score = cfg["match"]["heading_min_score"]
    keys = [heading_key(a) for a in cfg["sections"][sec.section_key]["aliases"]]
    sec.start_verified = any(heading_score(h, keys) >= min_score for h in bold(sec.start_page))
    if sec.next_section_title:
        nk = [heading_key(sec.next_section_title)]
        sec.end_verified = any(heading_score(h, nk) >= min_score
                               for p in (sec.end_page, sec.end_page + 1) for h in bold(p))
    return sec


# --------------------------------------------------------------------------- public API
NUMBERED = re.compile(r"^\(?\d{1,3}[.)]\s")


def looks_like_heading(text: str, at_margin: bool) -> bool:
    """Filter out bold table headers / cells, keep real subsection headings."""
    if not at_margin or len(text) < 4 or len(text) > 600:
        return False
    if NUMBERED.match(text):              # "12. We depend on ..." risk factor titles
        return True
    words = text.split()
    digits = sum(any(c.isdigit() for c in w) for w in words)
    if digits >= 2 or text.startswith("(") or "₹" in text:
        return False
    if sum(len(w) <= 2 for w in words) > len(words) / 2:   # letter-spaced table header "S ch e m e"
        return False
    if any(c.isdigit() for c in text) and len(words) <= 3:  # "Total 3,722"
        return False
    table_words = {"particulars", "financial year", "as at", "as of", "units", "category", "s. no",
                   "sr. no", "name of entity", "total"}
    low = text.lower()
    if any(low.startswith(t) for t in table_words):
        return False
    return len(words) <= 25 and text[0].isupper()


def add_subheadings(section: ExtractedSection, path: Union[str, Path]) -> ExtractedSection:
    """Attach the bold subsection headings found inside the section (its outline)."""
    page_nos = list(range(section.start_page, section.end_page + 1))
    raw = pdf_reader.bold_lines(path, page_nos)
    counts = Counter(r["text"] for r in raw)
    running = {t for t, c in counts.items() if c > max(2, len(page_nos) // 3)}  # running headers
    title_key = heading_key(section.section_title)
    seen, subs = set(), []
    for r in raw:
        pno, text = r["page_no"], r["text"]
        if text in running or heading_key(text) == title_key or text in seen:
            continue
        if not looks_like_heading(text, r["at_margin"]):
            continue
        if pno == section.end_page and section.next_section_title and \
                fuzz.ratio(heading_key(text), heading_key(section.next_section_title)) >= 90:
            continue
        seen.add(text)
        subs.append(SubHeading(page_no=pno, text=text))
    section.subheadings = subs
    return section


def _bold_lookup(path) -> BoldFn:
    cache: Dict[int, List[str]] = {}

    def bold(page_no: int) -> List[str]:
        if page_no not in cache:
            cache[page_no] = pdf_reader.bold_caps_lines(path, page_no)
        return cache[page_no]
    return bold


def extract_sections(path: Union[str, Path], *, doc_id: str, doc_name: str,
                     section_keys: Optional[List[str]] = None,
                     with_subheadings: bool = True) -> Dict[str, Optional[ExtractedSection]]:
    """Extract the configured sections from one DRHP/RHP PDF."""
    cfg = load_config()
    pages = pdf_reader.read_pages(path)
    outline = pdf_reader.read_outline(path)
    bold = _bold_lookup(path)
    out: Dict[str, Optional[ExtractedSection]] = {}
    for key in section_keys or list(cfg["sections"]):
        sec = extract_from_pages(pages, doc_id=doc_id, doc_name=doc_name, section_key=key,
                                 cfg=cfg, outline=outline, bold=bold)
        if sec and sec.method != "toc":
            verify_headings(sec, bold, cfg)
        if sec and with_subheadings:
            add_subheadings(sec, path)
        out[key] = sec
    return out


def manual_section(path: Union[str, Path], *, doc_id: str, doc_name: str, section_key: str,
                   start_page: int, end_page: int, with_subheadings: bool = True) -> ExtractedSection:
    """Re-extract with banker-supplied PDF page numbers (whole pages, heading line kept if found)."""
    cfg = load_config()
    sec_cfg = cfg["sections"][section_key]
    pages = pdf_reader.read_pages(path)
    keys = [heading_key(a) for a in sec_cfg["aliases"]]
    hit = find_heading_on_page(pages[start_page - 1], keys, cfg["match"]["heading_min_score"],
                               cfg["match"]["top_lines"]) if 1 <= start_page <= len(pages) else None
    sec = build_section(pages, doc_id=doc_id, doc_name=doc_name, section_key=section_key,
                        section_title=sec_cfg["title"], start_page=start_page,
                        start_line=hit[0] + 1 if hit else 1, end_page=end_page,
                        end_line=len(pages[min(end_page, len(pages)) - 1]), method="manual",
                        confidence=1.0, notes=["Page range set manually by the user."])
    verify_headings(sec, _bold_lookup(path), cfg)
    if with_subheadings:
        add_subheadings(sec, path)
    return sec


def document_toc(path: Union[str, Path]) -> List[TocEntry]:
    """Parsed TOC of a document (for display)."""
    cfg = load_config()
    pages = pdf_reader.read_pages(path)
    tp = find_toc_pages(pages, cfg["match"]["toc_scan_pages"])
    return parse_toc(pages, tp) if tp else []
