"""Build Stylus packs: a Task Sheet PDF + source / style PDFs made of stamped original pages.

* Every page of 01_SOURCE / 02_STYLE_REF is an original page (or half of a spread),
  stamped at the top with its citation label, e.g. "[D1 p.145]" or "[C1 p.160] STYLE ONLY".
* All files of one run together stay within PAGE_BUDGET (90) pages and SIZE_BUDGET_MB (9 MB):
  stamps use a standard (non-embedded) font, large images are downsampled step by step,
  and only then are the lowest-priority source pages dropped.
* The Task Sheet carries everything deal-specific (Task ID, blueprint, figures, required
  risks, which pages are where), so the Stylus presets stay fixed and shared.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pypdf
from pypdf.generic import RectangleObject

from . import blueprint as bpmod
from . import context, store
from .company_docs import Unit, find_unit
from .financials import DERIVED_LABELS, SPECS, Indicators
from .retrieval import UnitIndex
from .tasks import (PAGE_BUDGET, PRESETS, SIZE_BUDGET_MB, SIZE_LIMIT_MB, Task, TaskFile, new_task_id,
                    save_task, task_dir)

# --------------------------------------------------------------------------- fonts / numbers
_FONT: Optional[Tuple[str, str]] = None


def fonts() -> Tuple[str, str]:
    """(regular, bold) font names; a TTF with the ₹ glyph when available (Windows Arial)."""
    global _FONT
    if _FONT:
        return _FONT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in (("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
                      ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if Path(reg).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("PackSans", reg))
            pdfmetrics.registerFont(TTFont("PackSans-Bold", bold))
            _FONT = ("PackSans", "PackSans-Bold")
            return _FONT
    _FONT = ("Helvetica", "Helvetica-Bold")
    return _FONT


def _text(s: str) -> str:
    """Make text safe for the chosen font (₹ -> Rs. on core fonts)."""
    return s if fonts()[0] == "PackSans" else s.replace("₹", "Rs.")


def fmt_inr(v: Optional[float], decimals: int = 2) -> str:
    """Indian digit grouping: 406332.14 -> '4,06,332.14'; negatives in brackets."""
    if v is None:
        return "—"
    neg = v < 0
    s = f"{abs(v):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = whole + (f".{frac}" if frac else "")
    return f"({out})" if neg else out


# --------------------------------------------------------------------------- PDF writing
@dataclass
class PageRef:
    src: str            # source PDF path
    pdf_page: int       # 1-based
    side: str           # "", "L", "R"
    label: str          # "D1 p.45" / "C1 p.160"
    note: str = ""      # e.g. "STYLE REFERENCE ONLY"


def _overlay(width: float, height: float, x: float, y: float, text: str) -> pypdf.PageObject:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    bold = "Helvetica-Bold"          # standard PDF font: not embedded, adds ~1 KB per page
    text = text.replace("₹", "Rs.").encode("latin-1", "replace").decode("latin-1")
    c.setFont(bold, 9)
    tw = c.stringWidth(text, bold, 9)
    c.setFillColorRGB(1, 1, 0.75)
    c.setStrokeColorRGB(0.6, 0.45, 0)
    c.rect(x - 3, y - 4, tw + 6, 14, fill=1, stroke=1)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(x, y, text)
    c.save()
    buf.seek(0)
    return pypdf.PdfReader(buf).pages[0]


def page_key(r: PageRef) -> Tuple[str, int]:
    return r.src, r.pdf_page


def count_pages(refs: List[PageRef]) -> int:
    """PDF pages a list of refs occupies (both halves of a spread share one page)."""
    return len({page_key(r) for r in refs})


# image downsampling steps tried in order until a pack fits the size budget: (max px, JPEG quality)
IMAGE_LEVELS: List[Tuple[int, int]] = [(2200, 80), (1600, 70), (1200, 60), (900, 50)]


def shrink_images(page, max_px: int, quality: int) -> int:
    """Downsample images larger than max_px on one writer page. Text is untouched.
    Returns the number of images replaced."""
    n = 0
    try:
        images = list(page.images)
    except Exception:
        return 0
    for im in images:
        try:
            img = im.image
            if max(img.size) <= max_px:
                continue
            img = img.copy()
            img.thumbnail((max_px, max_px))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            im.replace(img, quality=quality)
            n += 1
        except Exception:
            continue                 # unusual image types (masks, CMYK JPX…) stay as they are
    return n


def write_pages_pdf(out, refs: List[PageRef], image_level: Optional[int] = 0) -> int:
    """Copy each referenced PDF page ONCE and stamp every label on its own half.

    Spreads are kept whole (never cropped): cropping hides but does not remove the other
    half's text, which a text extractor would then read under the wrong label. With both
    halves stamped, every printed page is labelled correctly, and a spread costs one page
    of the Stylus budget for two printed pages.
    """
    writer = pypdf.PdfWriter()
    readers: Dict[str, pypdf.PdfReader] = {}
    grouped: Dict[Tuple[str, int], List[PageRef]] = {}
    for r in refs:
        grouped.setdefault(page_key(r), []).append(r)
    for (src, pno), group in grouped.items():
        reader = readers.setdefault(src, pypdf.PdfReader(src))
        page = writer.add_page(reader.pages[pno - 1])
        box = page.mediabox
        x0, y1 = float(box.left), float(box.top)
        mid = x0 + (float(box.right) - x0) / 2
        seen = set()
        for r in group:
            if (r.side, r.label) in seen:
                continue
            seen.add((r.side, r.label))
            label = f"[{r.label}]" + (f"  {r.note}" if r.note else "")
            x = mid + 8 if r.side == "R" else x0 + 8
            page.merge_page(_overlay(float(box.right), float(box.top), x, y1 - 14, _text(label)))
        try:
            page.compress_content_streams()      # merging leaves the page content uncompressed
        except Exception:
            pass
    if image_level is not None:
        max_px, quality = IMAGE_LEVELS[image_level]
        for page in writer.pages:
            shrink_images(page, max_px, quality)
    try:
        writer.compress_identical_objects()
    except Exception:
        pass
    if hasattr(out, "write"):
        writer.write(out)
    else:
        with open(out, "wb") as f:
            writer.write(f)
    return len(grouped)


def write_sheet_pdf(out: Path, blocks: List[tuple]) -> int:
    """Render Task Sheet blocks: ('h1'|'h2'|'h3'|'p'|'kv', text) / ('bullets', [..]) / ('table', rows)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    reg, bold = fonts()
    st = {
        "h1": ParagraphStyle("h1", fontName=bold, fontSize=13, leading=16, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=10.5, leading=13, spaceBefore=8, spaceAfter=4,
                             textColor=colors.HexColor("#003B70")),
        "h3": ParagraphStyle("h3", fontName=bold, fontSize=9, leading=11, spaceBefore=5, spaceAfter=2),
        "p": ParagraphStyle("p", fontName=reg, fontSize=8.5, leading=10.5, spaceAfter=3),
        "cell": ParagraphStyle("cell", fontName=reg, fontSize=7.5, leading=9),
    }

    def para(text, style="p"):
        return Paragraph(escape(_text(str(text))).replace("\n", "<br/>"), st[style])

    story = []
    for b in blocks:
        kind = b[0]
        if kind in ("h1", "h2", "h3", "p"):
            story.append(para(b[1], kind))
        elif kind == "kv":
            k, v = b[1]
            story.append(Paragraph(f"<font name='{bold}'>{escape(_text(k))}:</font> {escape(_text(str(v)))}", st["p"]))
        elif kind == "bullets":
            story.append(ListFlowable([ListItem(para(x), leftIndent=10) for x in b[1]],
                                      bulletType="bullet", start="•", leftIndent=10, bulletFontSize=7))
        elif kind == "table":
            rows = [[para(c, "cell") for c in row] for row in b[1]]
            t = Table(rows, repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story += [t, Spacer(1, 4)]
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title="TASK SHEET")
    doc.build(story)
    return len(pypdf.PdfReader(str(out)).pages)


# --------------------------------------------------------------------------- page selection
def unit_ref(u: Unit, docs: dict) -> PageRef:
    return PageRef(src=docs[u.doc_id].path, pdf_page=u.pdf_page, side=u.side, label=u.cite)


def round_robin(groups: List[List[Unit]], budget: int,
                all_units: Optional[List[Unit]] = None,
                priority: Optional[List[Unit]] = None) -> Tuple[List[Unit], List[Unit]]:
    """Each round, every group contributes its best page not yet chosen, until `budget`
    PDF pages are used. A unit on a PDF page that is already in costs nothing. When
    `all_units` is given, the other half of every chosen spread is added too (it travels
    on the same PDF page). Returns (included in document order, relevant-but-dropped)."""
    chosen: List[Unit] = []
    ukeys: Set[Tuple[str, int]] = set()
    pkeys: Set[Tuple[str, int]] = set()
    ptr = [0] * len(groups)
    progress = True
    while progress:
        progress = False
        for gi, g in enumerate(groups):
            while ptr[gi] < len(g) and (g[ptr[gi]].doc_id, g[ptr[gi]].unit_no) in ukeys:
                ptr[gi] += 1
            if ptr[gi] >= len(g):
                continue
            u = g[ptr[gi]]
            pk = (u.doc_id, u.pdf_page)
            if pk not in pkeys and len(pkeys) >= budget:
                continue
            chosen.append(u)
            ukeys.add((u.doc_id, u.unit_no))
            pkeys.add(pk)
            ptr[gi] += 1
            progress = True
    if all_units:
        for u in all_units:
            if (u.doc_id, u.pdf_page) in pkeys and (u.doc_id, u.unit_no) not in ukeys:
                chosen.append(u)
                ukeys.add((u.doc_id, u.unit_no))
    dropped, dseen = [], set(ukeys)
    for g in groups:
        for u in g:
            k = (u.doc_id, u.unit_no)
            if k not in dseen:
                dropped.append(u)
                dseen.add(k)
    if priority is not None:
        priority[:] = list(chosen)          # selection order = importance (best page of each group first)
    chosen.sort(key=lambda u: (u.doc_id, u.unit_no))
    return chosen, dropped


def style_pages(section, budget: int) -> List[int]:
    """Pick comparable pages that cover the most subheadings (start page always included)."""
    counts: Dict[int, int] = {}
    for h in section.subheadings:
        counts[h.page_no] = counts.get(h.page_no, 0) + 1
    pages = [section.start_page]
    for p, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        if len(pages) >= budget:
            break
        if p not in pages:
            pages.append(p)
    # fill remaining budget with consecutive pages from the start (to show prose flow)
    p = section.start_page + 1
    while len(pages) < min(budget, section.page_count) and p <= section.end_page:
        if p not in pages:
            pages.append(p)
        p += 1
    return sorted(pages)


def statement_units(ind: Optional[Indicators], units: List[Unit]) -> List[Unit]:
    """Pages of the P&L / balance sheet / cash flow that the figures were read from."""
    if not ind:
        return []
    cites = []
    for f in ind.figures.values():
        if f.cite not in cites and f.cite != "manual":
            cites.append(f.cite)
    return [u for c in cites if (u := find_unit(units, c))]


# --------------------------------------------------------------------------- task sheet blocks
def _header(task_id: str, kind: str, meta: dict) -> List[tuple]:
    return [
        ("h1", f"TASK SHEET — {PRESETS[kind]['title']}"),
        ("kv", ("TASK_ID", task_id)),
        ("kv", ("Preset", PRESETS[kind]["name"])),
        ("kv", ("Company", meta["name"])),
        ("kv", ("Sector", meta.get("sector") or "—")),
        ("kv", ("Latest financial year", meta.get("fy") or "—")),
        ("kv", ("Prepared", datetime.now().strftime("%d %b %Y %H:%M"))),
        ("p", f"Your reply MUST start with the line:  TASK_ID: {task_id}"),
    ]


def _files_table(rows: List[Tuple[str, str, str]]) -> tuple:
    return ("table", [["File", "Holds", "Contents"]] + [list(r) for r in rows])


def _figures_blocks(ind: Optional[Indicators]) -> List[tuple]:
    if not ind or not ind.figures:
        return [("h2", "Key figures"), ("p", "No figures extracted. Use the source pages.")]
    yrs = ind.years + ["Previous"] * (2 - len(ind.years))
    unit = f"₹ in {ind.unit}" if ind.unit else "as in source"
    rows = [["Figure", yrs[0], yrs[1], "Cite as"]]
    for key, label, _, _ in SPECS:
        f = ind.figures.get(key)
        if f:
            rows.append([label, fmt_inr(f.values[0]), fmt_inr(f.values[1] if len(f.values) > 1 else None), f"[{f.cite}]"])
    drows = [["Ratio (computed from the figures above)", yrs[0], yrs[1]]]
    for k, (lab, kind) in DERIVED_LABELS.items():
        vals = ind.derived.get(k, [None, None])
        fmtd = [("—" if v is None else (f"{v:.1%}" if kind == "pct" else f"{v:.2f}x" if kind == "x" else fmt_inr(v)))
                for v in vals]
        if any(x != "—" for x in fmtd):
            drows.append([lab] + fmtd)
    return [
        ("h2", f"Key figures — verified from the financial statements ({ind.basis}, {unit})"),
        ("table", rows),
        ("p", "Ratios below are computed by the tool. When you use one, cite the underlying figures."),
        ("table", drows),
    ]


# --------------------------------------------------------------------------- pack building
def _finalise(deal_id: str, task: Task, sheet_blocks_fn, parts: List[Tuple[str, str, List[PageRef]]]) -> Task:
    """Write the sheet and page PDFs within PAGE_BUDGET pages and SIZE_BUDGET_MB megabytes.

    Order of measures: (1) drop lowest-priority source pages for the page budget;
    (2) downsample images step by step (IMAGE_LEVELS) for the size budget; (3) if still too
    big, drop lowest-priority source pages by their measured size. Priority = the order in
    task.source_cites (builders put the best page of each subsection / risk first)."""
    d = task_dir(deal_id, task.task_id)
    sheet_name = f"00_TASK_SHEET_{task.task_id}.pdf"
    rank = {c: i for i, c in enumerate(task.source_cites)}
    budget = int(SIZE_BUDGET_MB * 2 ** 20)

    def src_index():
        return next((i for i, (_, role, _r) in enumerate(parts) if role == "source"), None)

    def drop_keys(keys: Set[Tuple[str, int]], reason: str):
        i = src_index()
        name, role, refs = parts[i]
        cut = [r for r in refs if page_key(r) in keys]
        parts[i] = (name, role, [r for r in refs if page_key(r) not in keys])
        labels = [r.label for r in cut]
        task.dropped_cites = labels + [c for c in task.dropped_cites if c not in labels]
        task.source_cites = [c for c in task.source_cites if c not in set(labels)]
        task.notes.append(f"{len(keys)} source page(s) left out to fit the {reason}: {', '.join(labels[:12])}"
                          + ("…" if len(labels) > 12 else ""))

    def keys_by_priority(refs: List[PageRef]) -> List[Tuple[str, int]]:
        best: Dict[Tuple[str, int], int] = {}
        for r in refs:
            k = page_key(r)
            best[k] = min(best.get(k, 10 ** 6), rank.get(r.label, 10 ** 6))
        return sorted(best, key=lambda k: best[k])          # most important first

    # (1) page budget
    for _ in range(4):
        sheet_pages = write_sheet_pdf(d / sheet_name, sheet_blocks_fn())
        total = sheet_pages + sum(count_pages(refs) for _, _, refs in parts)
        i = src_index()
        if total <= PAGE_BUDGET or i is None:
            break
        drop_keys(set(keys_by_priority(parts[i][2])[-(total - PAGE_BUDGET):]), f"{PAGE_BUDGET}-page budget")

    # (2) + (3) size budget
    level = 0
    sizes: Dict[str, int] = {}
    for _ in range(12):
        sheet_pages = write_sheet_pdf(d / sheet_name, sheet_blocks_fn())
        sizes = {sheet_name: (d / sheet_name).stat().st_size}
        for name, role, refs in parts:
            if refs:
                write_pages_pdf(d / name, refs, level)
                sizes[name] = (d / name).stat().st_size
            elif (d / name).exists():
                (d / name).unlink()
        total = sum(sizes.values())
        if total <= budget:
            break
        if level < len(IMAGE_LEVELS) - 1:
            level += 1
            continue
        i = src_index()
        if i is None or not parts[i][2]:
            break
        # measure each source page alone (an over-estimate, shared resources counted every time)
        keys = keys_by_priority(parts[i][2])
        cost: Dict[Tuple[str, int], int] = {}
        for k in keys:
            buf = io.BytesIO()
            write_pages_pdf(buf, [r for r in parts[i][2] if page_key(r) == k], level)
            cost[k] = buf.tell()
        excess, cut = total - int(budget * 0.97), set()
        for k in reversed(keys):                              # least important first
            if excess <= 0:
                break
            cut.add(k)
            excess -= cost[k]
        drop_keys(cut, f"{SIZE_BUDGET_MB:.0f} MB size budget")
    if level:
        px, q = IMAGE_LEVELS[level]
        task.notes.append(f"Images reduced to at most {px} px (JPEG quality {q}) to fit {SIZE_LIMIT_MB:.0f} MB; "
                          "text is unchanged.")

    files = [TaskFile(name=sheet_name, role="sheet", pages=sheet_pages, size_bytes=sizes.get(sheet_name, 0))]
    for name, role, refs in parts:
        if refs:
            files.append(TaskFile(name=name, role=role, pages=count_pages(refs), size_bytes=sizes.get(name, 0)))
    task.files = files
    if task.total_pages > PAGE_BUDGET:
        task.notes.append(f"WARNING: {task.total_pages} pages exceed the {PAGE_BUDGET}-page budget.")
    if task.total_mb > SIZE_LIMIT_MB:
        task.notes.append(f"WARNING: {task.total_mb:.1f} MB exceeds the {SIZE_LIMIT_MB:.0f} MB Stylus limit.")
    save_task(deal_id, task)
    return task


def _base(deal_id: str):
    meta = store.load_meta(deal_id)
    docs = {d.doc_id: d for d in store.documents(deal_id)}
    return meta, docs


def build_ipo01(deal_id: str, company_pages: int = 4) -> Task:
    """Style & blueprint: comparable section pages + a few company overview pages."""
    meta, docs = _base(deal_id)
    ext = store.load_extractions(deal_id)
    if not ext:
        raise ValueError("Extract sections from the comparables first (page 2).")
    task = Task(task_id=new_task_id("IPO-01"), kind="IPO-01", created=datetime.now().isoformat(timespec="seconds"))
    units = context.load_units(deal_id)
    mapping = context.load_mapping(deal_id) or {}
    lookup = context.unit_lookup(units)
    top = [lookup[(h["doc_id"], h["unit_no"])] for h in
           mapping.get("our_business", {}).get("overview", {}).get("hits", [])]
    prio: List[Unit] = []
    overview, _ = round_robin([top], company_pages, units, priority=prio)

    budget = PAGE_BUDGET - 8 - len({(u.doc_id, u.pdf_page) for u in overview})  # 8 reserved for the sheet
    per_section = max(4, budget // max(1, len(ext)))
    style_refs: List[PageRef] = []
    for (doc_id, key), sec in sorted(ext.items()):
        for p in style_pages(sec, per_section):
            style_refs.append(PageRef(src=docs[doc_id].path, pdf_page=p, side="",
                                      label=f"{doc_id} p.{p}", note=f"STYLE ONLY · {sec.section_title}"))
    style_refs = style_refs[:budget]
    task.source_cites = [u.cite for u in prio]

    def blocks():
        b = _header(task.task_id, "IPO-01", meta)
        b.append(_files_table([
            (f"00_TASK_SHEET_{task.task_id}.pdf", "—", "This sheet"),
            (f"01_SOURCE_{task.task_id}.pdf", f"{len(overview)} printed pages", "Target company overview pages (to adapt the blueprint)"),
            (f"02_STYLE_REF_{task.task_id}.pdf", f"{len(style_refs)} pages", "Selected pages of comparable sections"),
        ]))
        b.append(("h2", "Comparable sections provided"))
        rows = [["ID", "Prospectus", "Section", "PDF pages", "Pages in 02_STYLE_REF"]]
        for (doc_id, key), sec in sorted(ext.items()):
            n = sum(1 for r in style_refs if r.label.startswith(doc_id + " ") and sec.section_title in r.note)
            rows.append([doc_id, docs[doc_id].name, sec.section_title, f"{sec.start_page}–{sec.end_page}", str(n)])
        b.append(("table", rows))
        b.append(("h2", "Full outlines of the comparable sections (headings in order)"))
        for (doc_id, key), sec in sorted(ext.items()):
            b.append(("h3", f"{doc_id} · {sec.section_title}"))
            heads = [f"[p.{h.page_no}] {h.text[:160]}" for h in sec.subheadings] or ["(no headings detected)"]
            b.append(("bullets", heads))
        b.append(("h2", "What to produce"))
        b.append(("bullets", [
            "A blueprint for OUR BUSINESS adapted to the target company (see 01_SOURCE): subsections in order, each with purpose, content elements, tables and target length.",
            "A RISK FACTORS taxonomy: groups (Internal / External / Offer-related) and themes, each with heading pattern and how comparables quantify it.",
            "Return exactly the JSON format defined in the preset.",
        ]))
        return b

    src = [unit_ref(u, docs) for u in overview]
    return _finalise(deal_id, task, blocks, [(f"01_SOURCE_{task.task_id}.pdf", "source", src),
                                             (f"02_STYLE_REF_{task.task_id}.pdf", "style", style_refs)])


def _subsection_units(sub_key: Optional[str], query: str, mapping: dict, kind: str,
                      lookup: dict, index: UnitIndex, top: int = 5) -> List[Unit]:
    if sub_key and sub_key in mapping.get(kind, {}):
        hits = [h for h in mapping[kind][sub_key]["hits"] if h["selected"]]
        return [lookup[(h["doc_id"], h["unit_no"])] for h in hits]
    return [u for u, _ in index.search(query, top_k=top)]


def _style_refs_for(deal_id: str, docs: dict, section_key: str, budget: int) -> List[PageRef]:
    ext = store.load_extractions(deal_id)
    secs = [s for (d, k), s in sorted(ext.items()) if k == section_key]
    if not secs:
        return []
    per = max(3, budget // len(secs))
    refs = []
    for s in secs:
        for p in style_pages(s, per):
            refs.append(PageRef(src=docs[s.doc_id].path, pdf_page=p, side="", label=f"{s.doc_id} p.{p}",
                                note="STYLE ONLY - do not use facts"))
    return refs[:budget]


def build_ipo02(deal_id: str, style_budget: int = 14) -> Task:
    """Draft Our Business: blueprint + figures on the sheet, selected company pages, short style refs."""
    meta, docs = _base(deal_id)
    bp = bpmod.load(deal_id)
    if not bp:
        raise ValueError("Create the blueprint first (IPO-01 or the default blueprint).")
    subs = [s for s in bp.our_business.subsections if s.include]
    units = context.load_units(deal_id)
    lookup = context.unit_lookup(units)
    mapping = context.load_mapping(deal_id) or {}
    ind = context.load_indicators(deal_id)
    index = UnitIndex(units)
    task = Task(task_id=new_task_id("IPO-02"), kind="IPO-02", created=datetime.now().isoformat(timespec="seconds"))

    style_refs = _style_refs_for(deal_id, docs, "our_business", style_budget)
    per_sub = {s.id: _subsection_units(s.map_key, f"{s.title} {s.purpose} {' '.join(s.content_elements)}",
                                       mapping, "our_business", lookup, index) for s in subs}
    groups = [statement_units(ind, units)] + [per_sub[s.id] for s in subs]
    budget = PAGE_BUDGET - 8 - len(style_refs)
    prio: List[Unit] = []
    chosen, dropped = round_robin(groups, budget, units, priority=prio)
    task.source_cites = [u.cite for u in prio]
    task.dropped_cites = [u.cite for u in dropped]

    def blocks():
        included = set(task.source_cites)
        b = _header(task.task_id, "IPO-02", meta)
        b.append(_files_table([
            (f"00_TASK_SHEET_{task.task_id}.pdf", "—", "This sheet"),
            (f"01_SOURCE_{task.task_id}.pdf", f"{len(task.source_cites)} printed pages", "Company document pages — the ONLY source of facts"),
            (f"02_STYLE_REF_{task.task_id}.pdf", f"{len(style_refs)} pages", "Comparable 'Our Business' pages — style only"),
        ]))
        b.append(("h2", "Style notes (learned from comparables)"))
        b.append(("p", bp.our_business.style_notes or "Formal DRHP register, first person plural."))
        b.append(("h2", f"Blueprint — write these {len(subs)} subsections in this order"))
        for s in subs:
            b.append(("h3", f"{s.id} {s.title}"))
            lines = []
            if s.purpose:
                lines.append(f"Purpose: {s.purpose}")
            if s.content_elements:
                lines.append("Cover: " + "; ".join(s.content_elements))
            if s.tables:
                lines.append("Tables: " + "; ".join(s.tables))
            if s.target_words:
                lines.append(f"Target length: about {s.target_words} words")
            pages = [u.cite for u in per_sub[s.id] if u.cite in included]
            lines.append("Most relevant source pages: " + (", ".join(pages) if pages else "none found — write [TBD] where needed"))
            b.append(("bullets", lines))
        b += _figures_blocks(ind)
        if task.dropped_cites:
            b.append(("h2", "Relevant pages NOT included (page budget)"))
            b.append(("p", ", ".join(task.dropped_cites[:60]) + ("…" if len(task.dropped_cites) > 60 else "")))
            b.append(("p", "If a subsection needs facts from these pages, write [TBD: see <page>]."))
        return b

    src = [unit_ref(u, docs) for u in chosen]
    return _finalise(deal_id, task, blocks, [(f"01_SOURCE_{task.task_id}.pdf", "source", src),
                                             (f"02_STYLE_REF_{task.task_id}.pdf", "style", style_refs)])


def build_ipo03(deal_id: str, style_budget: int = 14) -> Task:
    """Draft Risk Factors: required risks + evidence, taxonomy, figures; evidence pages first."""
    meta, docs = _base(deal_id)
    bp = bpmod.load(deal_id)
    if not bp:
        raise ValueError("Create the blueprint first (IPO-01 or the default blueprint).")
    themes = [t for t in bp.risk_factors.themes if t.include]
    triggers = [t for t in context.load_triggers(deal_id) if t.selected]
    units = context.load_units(deal_id)
    lookup = context.unit_lookup(units)
    mapping = context.load_mapping(deal_id) or {}
    ind = context.load_indicators(deal_id)
    index = UnitIndex(units)
    task = Task(task_id=new_task_id("IPO-03"), kind="IPO-03", created=datetime.now().isoformat(timespec="seconds"))

    style_refs = _style_refs_for(deal_id, docs, "risk_factors", style_budget)
    evidence_groups = [[u for e in t.evidence if (u := find_unit(units, e.cite))] for t in triggers]
    theme_groups = [_subsection_units(t.map_key, f"{t.theme} {t.quantification}", mapping, "risk_themes",
                                      lookup, index, top=4) for t in themes]
    groups = [statement_units(ind, units)] + evidence_groups + theme_groups
    budget = PAGE_BUDGET - 8 - len(style_refs)
    prio: List[Unit] = []
    chosen, dropped = round_robin(groups, budget, units, priority=prio)
    task.source_cites = [u.cite for u in prio]
    task.dropped_cites = [u.cite for u in dropped]

    def blocks():
        b = _header(task.task_id, "IPO-03", meta)
        b.append(_files_table([
            (f"00_TASK_SHEET_{task.task_id}.pdf", "—", "This sheet"),
            (f"01_SOURCE_{task.task_id}.pdf", f"{len(task.source_cites)} printed pages", "Company document pages — the ONLY source of facts"),
            (f"02_STYLE_REF_{task.task_id}.pdf", f"{len(style_refs)} pages", "Comparable 'Risk Factors' pages — style only"),
        ]))
        b.append(("h2", "Style notes"))
        b.append(("p", bp.risk_factors.style_notes or "Bold full-sentence headings; quantified; no mitigation."))
        req = [t for t in triggers if t.mandatory]
        opt = [t for t in triggers if not t.mandatory]
        b.append(("h2", f"REQUIRED risks — write each of these ({len(req)})"))
        for t in req:
            ev = "; ".join(f"[{e.cite}] {e.snippet[:200]}" for e in t.evidence[:3]) or "Standard IPO offer risk."
            b.append(("bullets", [f"[{t.category}] {t.title} — {t.reason}. Evidence: {ev}"]))
        if opt:
            b.append(("h2", "Further risks flagged by the documents (write if supported)"))
            for t in opt:
                ev = "; ".join(f"[{e.cite}]" for e in t.evidence[:4])
                b.append(("bullets", [f"[{t.category}] {t.title} — {t.reason}. {ev}"]))
        b.append(("h2", "Risk taxonomy (from the blueprint)"))
        rows = [["ID", "Group", "Theme", "Heading pattern / quantification"]]
        for t in themes:
            rows.append([t.id, t.group, t.theme, " ".join(x for x in (t.heading_pattern, t.quantification) if x)])
        b.append(("table", rows))
        b.append(("p", "Use your own continuous numbering RF01, RF02, … in the reply (not the taxonomy IDs)."))
        b += _figures_blocks(ind)
        if task.dropped_cites:
            b.append(("h2", "Relevant pages NOT included (page budget)"))
            b.append(("p", ", ".join(task.dropped_cites[:60])))
        return b

    src = [unit_ref(u, docs) for u in chosen]
    return _finalise(deal_id, task, blocks, [(f"01_SOURCE_{task.task_id}.pdf", "source", src),
                                             (f"02_STYLE_REF_{task.task_id}.pdf", "style", style_refs)])


def _text_blocks(text: str) -> List[tuple]:
    """Markdown-ish draft text -> sheet blocks (tables kept as tables)."""
    out: List[tuple] = []
    for para in [p for p in re.split(r"\n\s*\n", text) if p.strip()]:
        lines = [l for l in para.splitlines() if l.strip()]
        if lines and all(l.lstrip().startswith("|") for l in lines):
            rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines
                    if not re.fullmatch(r"[\s|:\-]+", l)]
            if rows:
                w = max(len(r) for r in rows)
                out.append(("table", [r + [""] * (w - len(r)) for r in rows]))
        elif lines and lines[0].startswith("#"):
            out.append(("h3", lines[0].lstrip("#").strip()))
            if len(lines) > 1:
                out.append(("p", " ".join(lines[1:])))
        else:
            out.append(("p", " ".join(l.strip() for l in lines)))
    return out


def build_ipo04(deal_id: str, section: str, item_id: str, instructions: str,
                issues: Optional[List[str]] = None, style_budget: int = 6, source_budget: int = 40) -> Task:
    """Revise one subsection: current text + instructions + check results, the pages it cites,
    the pages mapped to it, and a few comparable pages."""
    from . import replies
    meta, docs = _base(deal_id)
    draft = replies.load_draft(deal_id, section)
    item = next((i for i in draft.items if i.id == item_id), None) if draft else None
    if not item:
        raise ValueError(f"Item {item_id} is not in the current {section} draft.")
    units = context.load_units(deal_id)
    lookup = context.unit_lookup(units)
    mapping = context.load_mapping(deal_id) or {}
    ind = context.load_indicators(deal_id)
    index = UnitIndex(units)
    task = Task(task_id=new_task_id("IPO-04"), kind="IPO-04", created=datetime.now().isoformat(timespec="seconds"),
                target={"section": section, "item_id": item_id, "instructions": instructions})

    cited = [u for c in item.citations if not c.startswith("C") and (u := find_unit(units, c))]
    sub_key = None
    bp = bpmod.load(deal_id)
    if section == "our_business" and bp:
        sub = next((s for s in bp.our_business.subsections if s.id == item_id), None)
        sub_key = sub.map_key if sub else None
    kind = "our_business" if section == "our_business" else "risk_themes"
    mapped = _subsection_units(sub_key, f"{item.title} {instructions}", mapping, kind, lookup, index, top=8)
    searched = [u for u, _ in index.search(f"{item.title} {instructions}", top_k=8)]
    style_refs = _style_refs_for(deal_id, docs, section, style_budget)
    prio: List[Unit] = []
    chosen, dropped = round_robin([cited, mapped, searched], min(source_budget, PAGE_BUDGET - 8 - len(style_refs)),
                                  units, priority=prio)
    task.source_cites = [u.cite for u in prio]
    task.dropped_cites = [u.cite for u in dropped]

    def blocks():
        b = _header(task.task_id, "IPO-04", meta)
        b.append(_files_table([
            (f"00_TASK_SHEET_{task.task_id}.pdf", "—", "This sheet"),
            (f"01_SOURCE_{task.task_id}.pdf", f"{len(task.source_cites)} printed pages", "Company document pages — the ONLY source of facts"),
        ] + ([(f"02_STYLE_REF_{task.task_id}.pdf", f"{len(style_refs)} pages", "Comparable pages — style only")]
             if style_refs else [])))
        b.append(("h2", f"Subsection to revise: {item.id} {('[' + item.group + '] ') if item.group else ''}{item.title}"))
        b.append(("kv", ("Section", "Risk Factors" if section == "risk_factors" else "Our Business")))
        b.append(("h2", "REVISION INSTRUCTIONS (from the banker)"))
        b.append(("p", instructions.strip() or "Fix the problems listed below; otherwise keep the text."))
        if issues:
            b.append(("h2", "Problems found by automated checks"))
            b.append(("bullets", issues[:25]))
        b.append(("h2", "CURRENT TEXT"))
        b += _text_blocks(item.text)
        b += _figures_blocks(ind)
        return b

    src = [unit_ref(u, docs) for u in chosen]
    parts = [(f"01_SOURCE_{task.task_id}.pdf", "source", src)]
    if style_refs:
        parts.append((f"02_STYLE_REF_{task.task_id}.pdf", "style", style_refs))
    return _finalise(deal_id, task, blocks, parts)


BUILDERS = {"IPO-01": build_ipo01, "IPO-02": build_ipo02, "IPO-03": build_ipo03}
