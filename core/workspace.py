"""Section Workspace back end: banker edits with version history, and Word / Markdown export."""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Optional

from . import context, store
from .replies import CITE_BLOCK_RX, Draft, DraftItem, ItemVersion, check_citations, load_draft, save_draft

SECTION_TITLES = {"our_business": "Our Business", "risk_factors": "Risk Factors"}
MAX_VERSIONS = 30
STYLUS_NOTE = "AI-assisted draft (Stylus, Sonnet 5.5)"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _recheck(deal_id: str, item: DraftItem) -> None:
    chk = check_citations(item.text, context.load_units(deal_id), set())
    for k, v in chk.items():
        setattr(item, k, v)


def get_item(draft: Draft, item_id: str) -> DraftItem:
    for i in draft.items:
        if i.id == item_id:
            return i
    raise KeyError(item_id)


def save_item_edit(deal_id: str, section: str, item_id: str, title: str, text: str,
                   group: Optional[str] = None) -> Optional[DraftItem]:
    """Save a banker edit. The previous text becomes a version. Returns None if nothing changed."""
    draft = load_draft(deal_id, section)
    item = get_item(draft, item_id)
    title, text = title.strip(), text.strip()
    if title == item.title and text == item.text.strip() and (group is None or group == item.group):
        return None
    item.versions.append(ItemVersion(at=_now(), source=item.source or draft.task_id, title=item.title, text=item.text))
    item.versions = item.versions[-MAX_VERSIONS:]
    item.title, item.text, item.source = title, text, "edit"
    if group is not None:
        item.group = group
    _recheck(deal_id, item)
    save_draft(deal_id, draft)
    return item


def restore_version(deal_id: str, section: str, item_id: str, index: int) -> DraftItem:
    """Make version `index` current again; the current text is kept as a version too."""
    draft = load_draft(deal_id, section)
    item = get_item(draft, item_id)
    v = item.versions[index]
    item.versions.append(ItemVersion(at=_now(), source=item.source or draft.task_id, title=item.title, text=item.text))
    item.title, item.text, item.source = v.title, v.text, f"restored {v.source} ({v.at})"
    _recheck(deal_id, item)
    save_draft(deal_id, draft)
    return item


# --------------------------------------------------------------------------- export
def section_markdown(deal_id: str, section: str, with_ids: bool = False, note: Optional[str] = None) -> str:
    """`note` says how the draft was made (default: the Stylus workflow)."""
    draft = load_draft(deal_id, section)
    meta = store.load_meta(deal_id)
    out = [f"# {SECTION_TITLES[section].upper()}", "",
           f"_{meta['name']} · draft from {draft.task_id} · exported {_now()} · {note or STYLUS_NOTE} — "
           "verify every fact against the cited page_", ""]
    groups_seen = set()
    for n, i in enumerate(draft.items, 1):
        if section == "risk_factors" and i.group and i.group not in groups_seen:
            groups_seen.add(i.group)
            out += [f"## {i.group.upper()} RISKS", ""]
        prefix = f"{i.id} " if with_ids else (f"{n}. " if section == "risk_factors" else "")
        out += [f"### {prefix}{i.title}", "", i.text.strip(), ""]
    if draft.gaps:
        out += ["---", "", "## Gaps to obtain from the company", ""] + [f"- {g}" for g in draft.gaps] + [""]
    return "\n".join(out)


INLINE_RX = re.compile(r"(\*\*[^*]+\*\*|\[[^\[\]]{2,120}\])")


def _runs(par, text: str, cite_color=True):
    from docx.shared import RGBColor
    for part in INLINE_RX.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            par.add_run(part[2:-2]).bold = True
        elif part.startswith("[") and CITE_BLOCK_RX.fullmatch(part):
            r = par.add_run(part)
            if cite_color:
                r.font.color.rgb = RGBColor(0x05, 0x6D, 0xAE) if "TBD" not in part.upper() else RGBColor(0xC0, 0x00, 0x00)
        else:
            par.add_run(part.replace("*", ""))


def section_docx(deal_id: str, section: str, note: Optional[str] = None) -> bytes:
    """Word file of a full section: headings, paragraphs, bullets, markdown tables; citations kept."""
    import docx
    md = section_markdown(deal_id, section, note=note)
    d = docx.Document()
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip() or line.strip() == "---":
            i += 1
            continue
        if line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.fullmatch(r"[\s|:\-]+", lines[i]):
                    block.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            w = max(len(r) for r in block)
            t = d.add_table(rows=len(block), cols=w)
            t.style = "Table Grid"
            for r, row in enumerate(block):
                for c in range(w):
                    cell = t.cell(r, c)
                    cell.text = ""
                    _runs(cell.paragraphs[0], row[c] if c < len(row) else "")
                    if r == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
            d.add_paragraph()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            h = d.add_heading(level=min(level, 3))
            _runs(h, m.group(2).strip(), cite_color=False)
        elif re.match(r"^\s*[-*•]\s+", line):
            _runs(d.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*•]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            _runs(d.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", line))
        elif line.startswith("_") and line.endswith("_"):
            p = d.add_paragraph()
            r = p.add_run(line.strip("_"))
            r.italic = True
        else:
            _runs(d.add_paragraph(), line)
        i += 1
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
