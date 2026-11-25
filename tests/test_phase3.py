"""Phase 3 tests: pack building blocks, reply reading/parsing, citation checks."""
import io
import sys
from pathlib import Path

import pypdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.blueprint import from_llm_json  # noqa: E402
from core.company_docs import Unit  # noqa: E402
from core.models import ExtractedSection, SubHeading  # noqa: E402
from core.packer import PageRef, fmt_inr, round_robin, style_pages, write_pages_pdf, write_sheet_pdf  # noqa: E402
from core.replies import check_citations, find_task_id, parse_blueprint_json, read_reply, split_items  # noqa: E402


def U(n, doc="D1", printed=None):
    return Unit(doc_id=doc, unit_no=n, pdf_page=n, lines=["x"], printed_page=printed or n)


@pytest.mark.parametrize("v,s", [(406332.14, "4,06,332.14"), (1234567.0, "12,34,567.00"), (999.5, "999.50"),
                                 (-130489.03, "(1,30,489.03)"), (None, "—"), (0, "0.00")])
def test_fmt_inr(v, s):
    assert fmt_inr(v) == s


def test_round_robin_gives_every_group_its_best_page_first():
    a = [U(1), U(2), U(3)]
    b = [U(10), U(11)]
    c = [U(2), U(20)]                     # duplicate page shared with group a
    chosen, dropped = round_robin([a, b, c], budget=4)
    # round 1: a->1, b->10, c->2 ; round 2: a->3 (its 2 is taken) -> budget reached
    assert [u.unit_no for u in chosen] == [1, 2, 3, 10]
    assert {u.unit_no for u in dropped} == {11, 20}


def test_round_robin_counts_pdf_pages_and_adds_spread_partner():
    def S(n, pdf):
        return Unit(doc_id="D1", unit_no=n, pdf_page=pdf, side="L" if n % 2 == 0 else "R", lines=["x"], printed_page=n)
    units = [S(n, n // 2 + 1) for n in range(2, 12)]          # units 2/3 on pdf 2, 4/5 on pdf 3, ...
    chosen, _ = round_robin([[units[0]], [units[1]], [units[2]]], budget=2, all_units=units)
    # units 2 and 3 share pdf page 2 (one page), unit 4 is on pdf page 3 -> 2 pages; partner 5 added free
    assert [u.unit_no for u in chosen] == [2, 3, 4, 5]


def test_style_pages_cover_subheadings():
    sec = ExtractedSection(doc_id="C1", doc_name="x", section_key="our_business", section_title="Our Business",
                           start_page=100, start_line=1, end_page=120, end_line=1, method="toc", confidence=1,
                           subheadings=[SubHeading(page_no=p, text=f"h{p}") for p in (103, 103, 103, 110, 110, 118)])
    assert style_pages(sec, 4) == [100, 103, 110, 118]        # start page + most headings first
    assert style_pages(sec, 6) == [100, 101, 102, 103, 110, 118]  # then fill from the start


# ----------------------------------------------------------------- PDF writing
def _spread_pdf(tmp: Path) -> Path:
    from reportlab.pdfgen import canvas
    p = tmp / "spread.pdf"
    c = canvas.Canvas(str(p), pagesize=(1190, 842))
    c.drawString(100, 400, "LEFT PAGE TEXT")
    c.drawString(700, 400, "RIGHT PAGE TEXT")
    c.showPage()
    c.save()
    return p


def test_spread_written_once_with_each_half_labelled(tmp_path):
    src = _spread_pdf(tmp_path)
    out = tmp_path / "out.pdf"
    n = write_pages_pdf(out, [PageRef(str(src), 1, "L", "D1 p.6"), PageRef(str(src), 1, "R", "D1 p.7"),
                              PageRef(str(src), 1, "L", "D1 p.6")])        # duplicate ignored
    r = pypdf.PdfReader(str(out))
    assert n == 1 and len(r.pages) == 1
    assert round(float(r.pages[0].mediabox.width)) == 1190            # whole spread, not cropped
    import pdfplumber
    with pdfplumber.open(str(out)) as pdf:
        pg = pdf.pages[0]
        left = pg.crop((0, 0, 595, pg.height)).extract_text()
        right = pg.crop((595, 0, 1190, pg.height)).extract_text()
    assert "[D1 p.6]" in left and "LEFT PAGE TEXT" in left and "[D1 p.7]" not in left
    assert "[D1 p.7]" in right and "RIGHT PAGE TEXT" in right and "[D1 p.6]" not in right
    assert left.count("[D1 p.6]") == 1


def test_same_page_in_two_files_does_not_share_stamps(tmp_path):
    src = _spread_pdf(tmp_path)
    write_pages_pdf(tmp_path / "a.pdf", [PageRef(str(src), 1, "", "C1 p.1", "STYLE")])
    write_pages_pdf(tmp_path / "b.pdf", [PageRef(str(src), 1, "", "C1 p.1")])
    b = pypdf.PdfReader(str(tmp_path / "b.pdf")).pages[0].extract_text()
    assert "STYLE" not in b


def test_sheet_pdf_renders_rupee_and_tables(tmp_path):
    n = write_sheet_pdf(tmp_path / "s.pdf", [("h1", "TASK SHEET"), ("kv", ("TASK_ID", "IPO02-20261118-120000")),
                                            ("table", [["Figure", "FY2025"], ["Revenue", "₹ 4,06,332.14"]]),
                                            ("bullets", ["a", "b"])])
    assert n == 1


# ----------------------------------------------------------------- replies
def test_find_task_id_variants():
    assert find_task_id("TASK_ID: IPO02-20261118-123606\n### OB01") == "IPO02-20261118-123606"
    assert find_task_id("**TASK ID:** IPO03-20261118-123611") == "IPO03-20261118-123611"
    assert find_task_id("no id here") is None


def test_parse_blueprint_json_tolerates_word_quirks():
    text = 'TASK_ID: x\n```json\n{“our_business”: {"subsections": [{"title": "Overview", "target_words": "500"},]}}\n```'
    data = parse_blueprint_json(text)
    bp = from_llm_json(data, "IPO01-x")
    s = bp.our_business.subsections[0]
    assert s.id == "OB01" and s.target_words == 500 and s.map_key == "overview"


def test_split_items_handles_bold_headings_subheadings_and_gaps():
    text = """TASK_ID: x
### OB01 Overview
Para [D1 p.5].
#### Our Promoters
More.
### **RF02 [Offer] The Offer Price may not be indicative.**
Body.
### GAPS
- folios
- AUM by city
"""
    items, gaps = split_items(text)
    assert [(i[0], i[1]) for i in items] == [("OB01", ""), ("RF02", "Offer")]
    assert "#### Our Promoters" in items[0][3]
    assert items[1][2] == "The Offer Price may not be indicative."
    assert gaps == ["folios", "AUM by city"]


def test_check_citations():
    units = [U(5), U(145)]
    chk = check_citations("A [D1 p.5]. B [D1 p.145; D1 p.999]. C [C1 p.160]. D [TBD: x] [D1 p.5]",
                          units, pack_cites={"D1 p.5"})
    assert chk["citations"] == ["D1 p.5", "D1 p.145", "D1 p.999", "C1 p.160"]
    assert chk["unresolved"] == ["D1 p.999"]
    assert chk["not_in_pack"] == ["D1 p.145"]
    assert chk["comparable_cites"] == ["C1 p.160"]
    assert chk["tbd"] == 1


def test_read_reply_docx_keeps_headings_and_tables():
    import docx
    d = docx.Document()
    d.add_paragraph("TASK_ID: IPO02-20261118-123606")
    d.add_heading("OB01 Overview", level=2)
    d.add_paragraph("We are large [D1 p.5].")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Metric", "FY25", "PAT", "100"
    buf = io.BytesIO()
    d.save(buf)
    text = read_reply(buf.getvalue(), "reply.docx")
    assert "### OB01 Overview" in text
    assert "| PAT | 100 |" in text
    items, _ = split_items(text)
    assert items[0][0] == "OB01" and "| PAT | 100 |" in items[0][3]
