"""Read Stylus reply files, match them to their task, parse and check them.

IPO-01 -> Blueprint (JSON).  IPO-02 / IPO-03 -> draft items split on '### <ID> ...' headings,
with citation checks (every [D.. p..] must resolve to a page that was in the pack;
comparable [C..] pages must not be cited as facts).
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from . import blueprint as bpmod
from . import context, store
from .company_docs import find_unit
from .tasks import Task, load_task, save_task, task_dir

TASK_ID_RX = re.compile(r"TASK[_ ]?ID\W{0,6}?[:：]\W{0,6}?(IPO0[1-4]-\d{8}-\d{6})", re.I)
CITE_BLOCK_RX = re.compile(r"\[([^\[\]]{2,120})\]")
CITE_RX = re.compile(r"\b([DC]\d+)\s*([pu])\.?\s*(\d+)")
HEAD_RX = re.compile(r"^#{2,4}\s+(.+?)\s*$", re.M)
ITEM_RX = re.compile(r"^(?P<id>(?:OB|RF)\s?\d{1,3})\s*(?:\[(?P<group>[A-Za-z]+)\])?\s*[:.\-–]?\s*(?P<title>.*)$")
TBD_RX = re.compile(r"\[TBD[^\]]*\]", re.I)


class ItemVersion(BaseModel):
    at: str
    source: str                  # "IPO02-…" / "IPO04-…" (Stylus) | "edit" (banker)
    title: str
    text: str


class DraftItem(BaseModel):
    id: str
    title: str
    group: str = ""
    text: str
    citations: List[str] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)       # cites that match no page
    not_in_pack: List[str] = Field(default_factory=list)      # real pages, but not given to the model
    comparable_cites: List[str] = Field(default_factory=list)  # [C..] cited as a source
    tbd: int = 0
    source: str = ""             # where the current text came from (task id or "edit")
    versions: List[ItemVersion] = Field(default_factory=list)   # earlier texts, oldest first


class Draft(BaseModel):
    section: str                 # our_business | risk_factors
    task_id: str
    created: str
    items: List[DraftItem] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    missing_ids: List[str] = Field(default_factory=list)       # blueprint subsections not returned


# --------------------------------------------------------------------------- reading files
def read_reply(data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = []
        for block in d.element.body.iterchildren():
            tag = block.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = next(x for x in d.paragraphs if x._p is block)
                style = (p.style.name or "").lower() if p.style is not None else ""
                text = p.text
                if style.startswith("heading") and text.strip() and not text.lstrip().startswith("#"):
                    level = re.sub(r"\D", "", style) or "3"
                    text = "#" * min(4, max(2, int(level) + 1)) + " " + text
                parts.append(text)
            elif tag == "tbl":
                t = next(x for x in d.tables if x._tbl is block)
                for i, row in enumerate(t.rows):
                    parts.append("| " + " | ".join(c.text.strip() for c in row.cells) + " |")
                    if i == 0:
                        parts.append("|" + "---|" * len(row.cells))
        return "\n".join(parts)
    if ext == ".pdf":
        import pypdf
        r = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages)
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def find_task_id(text: str) -> Optional[str]:
    head = re.sub(r"\\([_\-*])", r"\1", text[:3000])          # markdown escapes: TASK\_ID, IPO01\-...
    m = TASK_ID_RX.search(head)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- IPO-01
CURLY_DQ = "\u201c\u201d\u201e\u201f\u2033"
_FENCE_RX = re.compile(r"(?:```|~~~)[ \t]*(?:json|JSON)?[ \t]*\n(.*?)(?:```|~~~)", re.S)


def _strip_comments(raw: str) -> str:
    """Drop // and /* */ comments that sit outside strings."""
    out, i, n, in_str = [], 0, len(raw), False
    while i < n:
        ch = raw[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif raw.startswith("//", i):
            while i < n and raw[i] != "\n":
                i += 1
            continue
        elif raw.startswith("/*", i):
            j = raw.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _next_nonspace(raw: str, i: int) -> int:
    while i < len(raw) and raw[i].isspace():
        i += 1
    return i


def _curly_to_straight(raw: str) -> str:
    """Turn curly quotes that delimit JSON strings into straight ones; keep prose quotes curly.

    A curly quote inside a string closes it only if what follows fits JSON: ':' (end of a key),
    '}' / ']' / end, or ',' followed by the next key ("...":) in an object or the next element in a list.
    """
    out: List[str] = []
    stack: List[str] = []
    in_str, i, n = False, 0, len(raw)
    quotes = CURLY_DQ + '"'

    def closes(i: int) -> bool:
        j = _next_nonspace(raw, i + 1)
        if j >= n or raw[j] in ":}]":
            return True
        if raw[j] != ",":
            return False
        k = _next_nonspace(raw, j + 1)
        if k >= n:
            return True
        if stack and stack[-1] == "[":
            return raw[k] in quotes + "{[-" or raw[k].isdigit()
        if raw[k] not in quotes:
            return False
        m = k + 1
        while m < n and raw[m] not in quotes and raw[m] != "\n":
            m += 1
        return m < n and raw[m] in quotes and _next_nonspace(raw, m + 1) < n and raw[_next_nonspace(raw, m + 1)] == ":"

    while i < n:
        ch = raw[i]
        if not in_str:
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]" and stack:
                stack.pop()
            if ch in quotes:
                in_str = True
                out.append('"')
            else:
                out.append(ch)
        elif ch == "\\" and i + 1 < n:
            out.append(raw[i:i + 2])
            i += 1
        elif ch in quotes and closes(i):
            in_str = False
            out.append('"')
        elif ch == '"':
            out.append('\\"')                     # a quote inside the text that Stylus did not escape
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _escape_stray_quotes(raw: str, limit: int = 400) -> object:
    """json.loads, escaping quotes that end a string too early (e.g. "uses "we" form")."""
    for _ in range(limit):
        try:
            return json.loads(raw, strict=False)          # strict=False: line breaks inside strings are fine
        except json.JSONDecodeError as e:
            if not any(k in e.msg for k in ("delimiter", "property name", "Extra data")):
                raise
            q = raw.rfind('"', 0, e.pos)
            while q > 0 and raw[q - 1] == "\\":
                q = raw.rfind('"', 0, q - 1)
            if q <= 0:
                raise
            raw = raw[:q] + '\\"' + raw[q + 1:]
    return json.loads(raw, strict=False)


def _load_json_tolerant(raw: str) -> object:
    raw = raw.replace("\ufeff", "").replace("\u200b", "")
    raw = re.sub(r"[\u00a0\u2007\u202f\u2002\u2003]", " ", raw)     # Word non-breaking spaces
    raw = _strip_comments(raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)                          # trailing commas
    try:
        return json.loads(raw, strict=False)          # strict=False: line breaks inside strings are fine
    except json.JSONDecodeError as e:
        err: Exception = e
    # Word AutoFormat makes the JSON's own quotes curly, and Stylus may leave quotes in the text unescaped:
    # decide quote by quote which ones end a string
    fixed = _curly_to_straight(raw)
    try:
        return json.loads(fixed, strict=False)
    except json.JSONDecodeError:
        pass
    try:
        return _escape_stray_quotes(fixed)            # last resort
    except json.JSONDecodeError:
        raise err


def _norm_keys(obj):
    if isinstance(obj, dict):
        return {re.sub(r"[\s\-]+", "_", str(k).strip()).lower(): _norm_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm_keys(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\s+", " ", obj).strip()                      # PDF line wraps inside text
    return obj


def _find_blueprint(obj) -> Optional[dict]:
    if isinstance(obj, dict):
        if "our_business" in obj or "risk_factors" in obj:
            return obj
        for v in obj.values():
            hit = _find_blueprint(v)
            if hit is not None:
                return hit
    return None


def parse_blueprint_json(text: str) -> dict:
    """Find and read the IPO-01 JSON, tolerating what Word, PDF export and markdown do to it."""
    candidates = [m.group(1) for m in _FENCE_RX.finditer(text)]
    last = text.rfind("}")
    starts = [m.start() for m in re.finditer(r"\{", text)][:30]
    candidates += [text[i:last + 1] for i in starts if i < last]
    if not candidates:
        raise ValueError("No JSON block found in the reply.")
    first_err: Optional[Exception] = None
    for raw in candidates:
        if "{" not in raw:
            continue
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        try:
            data = _find_blueprint(_norm_keys(_load_json_tolerant(raw)))
        except (json.JSONDecodeError, ValueError) as e:
            first_err = first_err or e
            continue
        if data is not None:
            return data
    if first_err:
        raise ValueError(f"The blueprint JSON could not be read ({first_err}). "
                         "Copy Stylus's reply as plain text (.txt or .md) and upload it again.")
    raise ValueError("The reply has JSON, but no 'our_business' or 'risk_factors' part.")


# --------------------------------------------------------------------------- IPO-02 / 03
def split_items(text: str) -> Tuple[List[Tuple[str, str, str, str]], List[str]]:
    """[(id, group, title, body)], gaps."""
    heads = list(HEAD_RX.finditer(text))
    items, gaps = [], []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
        head = h.group(1).strip().strip("*").strip()
        if head.upper().startswith("GAPS"):
            gaps = [re.sub(r"^[-*•]\s*", "", l).strip() for l in body.splitlines() if l.strip()]
            continue
        m = ITEM_RX.match(head)
        if not m:
            if items:  # a sub-heading inside an item: keep it in the body
                iid, grp, title, prev = items[-1]
                items[-1] = (iid, grp, title, f"{prev}\n\n#### {head}\n{body}".strip())
            continue
        iid = m.group("id").replace(" ", "")
        title = m.group("title").strip().strip("*").strip()
        items.append((iid, (m.group("group") or "").title(), title, body))
    return items, gaps


def check_citations(text: str, units, pack_cites: Set[str]) -> dict:
    cites, unresolved, not_in_pack, comp = [], [], [], []
    for block in CITE_BLOCK_RX.findall(text):
        for doc, kind, num in CITE_RX.findall(block):
            c = f"{doc} {kind}.{num}"
            if c in cites:
                continue
            cites.append(c)
            if doc.startswith("C"):
                comp.append(c)
            elif not find_unit(units, c):
                unresolved.append(c)
            elif pack_cites and c not in pack_cites:
                not_in_pack.append(c)
    return {"citations": cites, "unresolved": unresolved, "not_in_pack": not_in_pack,
            "comparable_cites": comp, "tbd": len(TBD_RX.findall(text))}


def parse_draft(text: str, task: Task, deal_id: str) -> Draft:
    section = "our_business" if task.kind == "IPO-02" else "risk_factors"
    units = context.load_units(deal_id)
    pack = set(task.source_cites)
    raw_items, gaps = split_items(text)
    if not raw_items:
        raise ValueError("No '### <ID> <Title>' headings found. Is this the right reply file?")
    draft = Draft(section=section, task_id=task.task_id, created=datetime.now().isoformat(timespec="seconds"),
                  gaps=gaps)
    for iid, grp, title, body in raw_items:
        chk = check_citations(body, units, pack)
        draft.items.append(DraftItem(id=iid, title=title, group=grp, text=body, source=task.task_id, **chk))
    if section == "our_business":
        bp = bpmod.load(deal_id)
        if bp:
            got = {i.id for i in draft.items}
            draft.missing_ids = [s.id for s in bp.our_business.subsections if s.include and s.id not in got]
    return draft


def save_draft(deal_id: str, draft: Draft) -> None:
    d = store.deal_dir(deal_id) / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{draft.section}.json").write_text(draft.model_dump_json(indent=1), encoding="utf-8")
    hist = d / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{draft.section}_{draft.task_id}.json").write_text(draft.model_dump_json(indent=1), encoding="utf-8")


def load_draft(deal_id: str, section: str) -> Optional[Draft]:
    f = store.deal_dir(deal_id) / "drafts" / f"{section}.json"
    return Draft.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


def merge_revision(deal_id: str, text: str, task: Task) -> Draft:
    """IPO-04 reply: replace one item of the current draft, keeping the old text as a version."""
    tgt = task.target or {}
    draft = load_draft(deal_id, tgt.get("section", ""))
    if not draft:
        raise ValueError("The draft this revision belongs to no longer exists.")
    raw_items, _ = split_items(text)
    if not raw_items:
        raise ValueError("No '### <ID> <Title>' heading found in the revision reply.")
    iid, grp, title, body = next((r for r in raw_items if r[0] == tgt.get("item_id")), raw_items[0])
    item = next((i for i in draft.items if i.id == tgt.get("item_id")), None)
    if not item:
        raise ValueError(f"Item {tgt.get('item_id')} is not in the current draft.")
    item.versions.append(ItemVersion(at=datetime.now().isoformat(timespec="seconds"),
                                     source=item.source or draft.task_id, title=item.title, text=item.text))
    chk = check_citations(body, context.load_units(deal_id), set(task.source_cites))
    item.title, item.text, item.source = title or item.title, body, task.task_id
    item.group = grp or item.group
    for k, v in chk.items():
        setattr(item, k, v)
    save_draft(deal_id, draft)
    return draft


# --------------------------------------------------------------------------- entry point
def ingest_reply(deal_id: str, data: bytes, filename: str, expected_task: Optional[str] = None) -> Tuple[Task, object]:
    """Read a reply file, find its task, parse it and store the result.

    Returns (task, Blueprint | Draft). Raises ValueError with a readable message on problems.
    """
    text = read_reply(data, filename)
    tid = find_task_id(text)
    if not tid:
        if not expected_task:
            raise ValueError("No 'TASK_ID: …' line found at the top of the reply.")
        tid = expected_task
    elif expected_task and tid != expected_task:
        raise ValueError(f"This reply belongs to task {tid}, not {expected_task}.")
    task = load_task(deal_id, tid)
    if not task:
        raise ValueError(f"Task {tid} does not exist in this deal.")
    d = task_dir(deal_id, tid)
    (d / f"reply{Path(filename).suffix.lower() or '.txt'}").write_bytes(data)
    (d / "reply_text.md").write_text(text, encoding="utf-8")
    task.reply_file = filename
    try:
        if task.kind == "IPO-01":
            result = bpmod.from_llm_json(parse_blueprint_json(text), tid)
            if not result.our_business.subsections:
                raise ValueError("The blueprint has no Our Business subsections.")
            bpmod.save(deal_id, result)
        elif task.kind == "IPO-04":
            result = merge_revision(deal_id, text, task)
        else:
            result = parse_draft(text, task, deal_id)
            save_draft(deal_id, result)
        task.status, task.errors = "reply_parsed", []
    except (ValueError, json.JSONDecodeError) as e:
        task.status, task.errors = "reply_error", [str(e)]
        save_task(deal_id, task)
        raise ValueError(str(e)) from e
    save_task(deal_id, task)
    return task, result
