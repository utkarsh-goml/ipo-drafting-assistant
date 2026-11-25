"""Low-level PDF access: page text, bookmarks, bold headings, page images.

pypdf is used for fast full-document text; pdfplumber only for font-level work on
a small set of pages (bold headings, page previews). Both are permissively licensed.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import pypdf
from typing import Dict, List, Tuple, Union

from .paths import CACHE_DIR

_QUOTES = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ", "�": "'",
}
_QUOTE_RE = re.compile("|".join(map(re.escape, _QUOTES)))


def clean_line(s: str) -> str:
    s = _QUOTE_RE.sub(lambda m: _QUOTES[m.group(0)], s)
    return re.sub(r"[ \t]+", " ", s).strip()


def file_hash(path: Union[str, Path]) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def read_pages(path: Union[str, Path]) -> List[List[str]]:
    """Return non-empty cleaned lines for every page (index 0 = PDF page 1). Cached on disk."""
    path = Path(path)
    key = file_hash(path)
    cache = CACHE_DIR / f"{key}.pages.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    reader = pypdf.PdfReader(str(path))
    pages: List[List[str]] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # damaged page: keep position, no text
            text = ""
        pages.append([c for c in (clean_line(l) for l in text.splitlines()) if c])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    return pages


def page_count(path: Union[str, Path]) -> int:
    return len(pypdf.PdfReader(str(path)).pages)


def read_outline(path: Union[str, Path]) -> List[Tuple[str, int, int]]:
    """Flattened bookmarks as (title, 1-based page, depth). Empty list when none."""
    reader = pypdf.PdfReader(str(path))
    out: List[Tuple[str, int, int]] = []

    def walk(items, depth):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
                continue
            try:
                out.append((clean_line(it.title), reader.get_destination_page_number(it) + 1, depth))
            except Exception:
                continue

    try:
        walk(reader.outline, 0)
    except Exception:
        return []
    return out


BOLD_MARKERS = ("bold", "black", "heavy", "semibold", "demi", ",b", "-bd", "_bd")


def is_bold_font(fontname: str) -> bool:
    f = fontname.lower()
    return any(m in f for m in BOLD_MARKERS)


def bold_lines(path: Union[str, Path], page_nos: List[int]) -> List[dict]:
    """Bold text lines on the given 1-based pages, with position info.

    Returns dicts: page_no, text, top, bottom, x0, size, at_margin (line starts at the
    page's left text margin). Wrapped bold lines are merged into one heading. Used to
    recover the subsection outline of an extracted section.
    """
    import pdfplumber

    found: List[dict] = []
    with pdfplumber.open(str(path)) as pdf:
        for n in page_nos:
            if not 1 <= n <= len(pdf.pages):
                continue
            words = pdf.pages[n - 1].extract_words(extra_attrs=["fontname", "size"])
            if not words:
                continue
            rows: Dict[int, List[dict]] = {}
            for w in words:
                rows.setdefault(round(w["top"]), []).append(w)
            margin = min(w["x0"] for w in words)
            merged: List[dict] = []
            for top in sorted(rows):
                ws = sorted(rows[top], key=lambda w: w["x0"])
                if not all(is_bold_font(w["fontname"]) for w in ws):
                    continue
                line = {
                    "page_no": n, "text": clean_line(" ".join(w["text"] for w in ws)),
                    "top": ws[0]["top"], "bottom": max(w["bottom"] for w in ws),
                    "x0": ws[0]["x0"], "x1": max(w["x1"] for w in ws), "size": ws[0]["size"],
                    "words": len(ws),
                }
                prev = merged[-1] if merged else None
                # merge a wrapped heading: next line directly below, roughly same left edge
                if prev and 0 <= line["top"] - prev["bottom"] < line["size"] * 0.8 \
                        and abs(line["x0"] - prev["x0"]) < 25 and prev["words"] > 3:
                    prev["text"] = f'{prev["text"]} {line["text"]}'
                    prev["bottom"] = line["bottom"]
                    prev["words"] += line["words"]
                else:
                    merged.append(line)
            for m in merged:
                m["at_margin"] = m["x0"] - margin < 30
                found.append(m)
    return found


def render_unit(path: str, page_no: int, side: str = "", resolution: int = 90):
    """Image of a printed-page unit: whole PDF page, or its left/right half for spreads."""
    img = render_page(path, page_no, resolution)
    if side in ("L", "R"):
        w, h = img.size
        img = img.crop((0, 0, w // 2, h) if side == "L" else (w // 2, 0, w, h))
    return img


@lru_cache(maxsize=64)
def render_page(path: str, page_no: int, resolution: int = 72):
    """PIL image of a 1-based page (for previews in the UI)."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return pdf.pages[page_no - 1].to_image(resolution=resolution).original.copy()


def bold_caps_lines(path: Union[str, Path], page_no: int) -> List[str]:
    """Bold, (mostly) capital-letter lines on one 1-based page: candidate section headings."""
    out = []
    for r in bold_lines(path, [page_no]):
        letters = [c for c in r["text"] if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) >= 0.8:
            out.append(r["text"])
    return out
