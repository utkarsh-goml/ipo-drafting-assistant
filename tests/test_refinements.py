"""Pack size budget (Stylus 10 MB limit) and bold-capital section headings."""
import io
import sys
from pathlib import Path

import pypdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import packer, store  # noqa: E402
from core.packer import PageRef, shrink_images, write_pages_pdf  # noqa: E402
from core.section_extractor import extract_from_pages, load_config, locate_bold_heading, heading_key  # noqa: E402
from core.tasks import Task  # noqa: E402


def _image_pdf(path: Path, n_pages: int, px: int = 1800, text: str = "Revenue 4,06,332.14") -> Path:
    """Pages with a large noisy (incompressible) photo plus a line of real text."""
    import random

    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=(842, 595))
    for i in range(n_pages):
        rnd = random.Random(i)
        img = Image.frombytes("RGB", (px, px), bytes(rnd.getrandbits(8) for _ in range(px * px * 3)))
        c.drawImage(ImageReader(img), 0, 0, width=842, height=595)
        c.drawString(100, 300, f"{text} page {i + 1}")
        c.showPage()
    c.save()
    return path


def test_shrink_images_keeps_text_and_cuts_size(tmp_path):
    src = _image_pdf(tmp_path / "big.pdf", 1, px=1500)
    big, small = io.BytesIO(), io.BytesIO()
    write_pages_pdf(big, [PageRef(str(src), 1, "", "D1 p.1")], image_level=None)
    write_pages_pdf(small, [PageRef(str(src), 1, "", "D1 p.1")], image_level=3)       # 900 px, q50
    assert small.tell() < big.tell() / 2
    page = pypdf.PdfReader(small).pages[0]
    assert "Revenue 4,06,332.14 page 1" in page.extract_text() and "[D1 p.1]" in page.extract_text()
    assert max(next(iter(page.images)).image.size) <= 900


def test_stamp_uses_standard_font_not_embedded(tmp_path):
    src = _image_pdf(tmp_path / "one.pdf", 1, px=50)
    out = tmp_path / "out.pdf"
    write_pages_pdf(out, [PageRef(str(src), 1, "", "D1 p.1", "STYLE ONLY · Our Business")])
    page = pypdf.PdfReader(str(out)).pages[0]
    fonts = page["/Resources"]["/Font"]
    names = [fonts[k].get_object()["/BaseFont"] for k in fonts]
    assert "/Helvetica-Bold" in names
    assert not any(fonts[k].get_object().get("/FontDescriptor") for k in fonts
                   if fonts[k].get_object()["/BaseFont"] == "/Helvetica-Bold")


def test_finalise_fits_size_budget_dropping_lowest_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "deal_dir", lambda d: tmp_path / d)
    monkeypatch.setattr(packer, "SIZE_BUDGET_MB", 0.9)
    monkeypatch.setattr(packer, "IMAGE_LEVELS", [(1800, 95)])          # no image reduction possible
    src = _image_pdf(tmp_path / "ar.pdf", 6, px=300)   # ~0.26 MB per page, 6 pages ~1.6 MB
    refs = [PageRef(str(src), n, "", f"D1 p.{n}") for n in range(1, 7)]
    task = Task(task_id="IPO02-20261119-000000", kind="IPO-02", created="x",
                source_cites=["D1 p.3", "D1 p.1", "D1 p.5", "D1 p.2", "D1 p.6", "D1 p.4"])   # priority order
    t = packer._finalise("t", task, lambda: [("h1", "TASK SHEET")],
                         [("01_SOURCE_x.pdf", "source", refs)])
    assert t.total_mb <= 0.9
    kept = [c for c in ["D1 p.3", "D1 p.1", "D1 p.5", "D1 p.2", "D1 p.6", "D1 p.4"] if c in t.source_cites]
    dropped = [c for c in ["D1 p.3", "D1 p.1", "D1 p.5", "D1 p.2", "D1 p.6", "D1 p.4"] if c not in t.source_cites]
    assert kept and dropped
    assert kept == ["D1 p.3", "D1 p.1", "D1 p.5", "D1 p.2", "D1 p.6", "D1 p.4"][:len(kept)]   # best pages kept
    assert set(dropped) <= set(t.dropped_cites)
    assert any("MB size budget" in n for n in t.notes)
    assert all(f.size_bytes > 0 for f in t.files)


# ----------------------------------------------------------------- bold capital headings
CFG = load_config()


def test_bold_heading_beats_text_only_match():
    pages = [["x"] * 5 for _ in range(10)]
    pages[4] = ["OUR BUSINESS", "a cross-reference page"]          # TOC-predicted page, not bold
    pages[5] = ["OUR BUSINESS", "We are a leading company"]         # the real, bold heading
    bold = lambda p: ["OUR BUSINESS"] if p == 6 else []              # 1-based page numbers
    keys = [heading_key("Our Business")]
    page, line, _, verified = locate_bold_heading(pages, keys, CFG, predicted=4, bold=bold)
    assert (page, line, verified) == (5, 0, True)
    page, _, _, verified = locate_bold_heading(pages, keys, CFG, predicted=4, bold=None)
    assert (page, verified) == (4, False)


def _toc_doc():
    toc = ["TABLE OF CONTENTS", "SECTION II: RISK FACTORS 3", "SECTION III: INTRODUCTION 5",
           "OUR BUSINESS 6", "KEY REGULATIONS AND POLICIES 8"]
    pages = [toc] + [[f"text {i}", str(i)] for i in range(1, 10)]
    pages[3] = ["SECTION II: RISK FACTORS", "An investment involves risk.", "3"]
    pages[5] = ["SECTION III: INTRODUCTION", "Summary.", "5"]
    pages[6] = ["OUR BUSINESS", "We are a company.", "6"]
    pages[8] = ["KEY REGULATIONS AND POLICIES", "Rules.", "8"]
    return pages


@pytest.mark.parametrize("key,start,end", [("risk_factors", 4, 5), ("our_business", 7, 8)])
def test_toc_first_with_bold_verification(key, start, end):
    pages = _toc_doc()
    heads = {4: ["SECTION II: RISK FACTORS"], 6: ["SECTION III: INTRODUCTION"], 7: ["OUR BUSINESS"],
             9: ["KEY REGULATIONS AND POLICIES"]}
    sec = extract_from_pages(pages, doc_id="C1", doc_name="x", section_key=key, cfg=CFG,
                             outline=[("Wrong bookmark", 2, 0)], bold=lambda p: heads.get(p, []))
    assert sec.method == "toc" and (sec.start_page, sec.end_page) == (start, end)
    assert sec.start_verified and sec.end_verified and sec.confidence == 1.0


def test_missing_bold_heading_is_flagged():
    pages = _toc_doc()
    sec = extract_from_pages(pages, doc_id="C1", doc_name="x", section_key="our_business", cfg=CFG,
                             bold=lambda p: [])
    assert sec.method == "toc" and not sec.start_verified and sec.confidence < 1.0
    assert any("bold capital" in n for n in sec.notes)
