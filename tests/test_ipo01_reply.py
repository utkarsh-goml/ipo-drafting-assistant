"""IPO-01 replies: the blueprint JSON must survive what Stylus, Word, PDF export and markdown do to it."""
import io
import re
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import store  # noqa: E402
from core.blueprint import _int_or_none, from_llm_json  # noqa: E402
from core.replies import find_task_id, ingest_reply, parse_blueprint_json, read_reply  # noqa: E402
from core.tasks import Task, save_task  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ipo01_sample_reply.md"
BASE = FIXTURE.read_text(encoding="utf-8")
TID = "IPO01-20261120-101500"
OB_TITLES = ["Overview", "Our Competitive Strengths", "Our Strategies", "Our Products and Services",
             "Distribution", "Risk Management and Compliance"]
RF_THEMES = ["Dependence on market performance of AUM", "Fund under-performance", "Distributor concentration",
             "Regulatory change on expense ratios", "Offer price and volatility"]


def _docx(text, nbsp=False, smart=False, one_para=False):
    import docx
    d = docx.Document()
    lines = text.splitlines()
    if one_para:
        p = d.add_paragraph()
        for i, ln in enumerate(lines):
            if i:
                p.add_run().add_break()
            p.add_run(ln)
    else:
        for ln in lines:
            if nbsp:
                ln = re.sub(r"^ +", lambda m: " " * len(m.group()), ln)
            if smart:                                   # Word AutoFormat: every straight quote turns curly
                out, opening = [], True
                for ch in ln:
                    if ch == '"':
                        out.append("“" if opening else "”")
                        opening = not opening
                    else:
                        out.append(ch)
                ln = "".join(out)
            d.add_paragraph(ln)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def _pdf(text):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    b = io.BytesIO()
    c = canvas.Canvas(b, pagesize=A4)
    c.setFont("Helvetica", 9)
    y = 800
    for ln in text.replace("₹", "Rs.").splitlines():
        for part in (textwrap.wrap(ln, 95, drop_whitespace=False) or [""]):
            c.drawString(40, y, part)
            y -= 12
            if y < 50:
                c.showPage()
                c.setFont("Helvetica", 9)
                y = 800
    c.save()
    return b.getvalue()


VARIANTS = {
    "clean_md": lambda: (BASE.encode(), "r.md"),
    "preamble_and_note": lambda: (("Here is the blueprint for {deal}:\n\n" + BASE + "\nTell me if you want changes.").encode(), "r.txt"),
    "word_paste": lambda: (_docx(BASE), "r.docx"),
    "word_nbsp_indent": lambda: (_docx(BASE, nbsp=True), "r.docx"),
    "word_autoformat_quotes": lambda: (_docx(BASE, smart=True), "r.docx"),
    "word_single_paragraph": lambda: (_docx(BASE, one_para=True), "r.docx"),
    "pdf_wrapped_lines": lambda: (_pdf(BASE), "r.pdf"),
    "escaped_markdown_task_id": lambda: (BASE.replace("TASK_ID: ", "**TASK\\_ID**: ").encode(), "r.md"),
    "no_code_fence": lambda: (BASE.replace("```json\n", "").replace("\n```", "").encode(), "r.md"),
    "comments_trailing_commas": lambda: (BASE.replace('"groups": [', '// groups\n    "groups": [')
                                         .replace('"C1 p.60"}', '"C1 p.60"},').encode(), "r.md"),
    "wrapper_and_title_case_keys": lambda: (BASE.replace("```json\n{", '```json\n{"blueprint": {')
                                            .replace("}\n```", "}}\n```")
                                            .replace('"our_business"', '"Our Business"')
                                            .replace('"risk_factors"', '"Risk Factors"').encode(), "r.md"),
    "tilde_fence": lambda: (BASE.replace("```json", "~~~JSON").replace("```", "~~~").encode(), "r.md"),
    "unescaped_straight_quotes": lambda: (BASE.replace("(“our Company”, “we”, “us”)", '("our Company", "we", "us")').encode(), "r.md"),
}


@pytest.mark.parametrize("name", list(VARIANTS))
def test_ipo01_reply_variants_read_completely(name):
    data, fname = VARIANTS[name]()
    text = read_reply(data, fname)
    assert find_task_id(text) == TID
    bp = from_llm_json(parse_blueprint_json(text), TID)
    assert [s.title for s in bp.our_business.subsections] == OB_TITLES
    assert [t.theme for t in bp.risk_factors.themes] == RF_THEMES
    assert [s.target_words for s in bp.our_business.subsections] == [600, 1200, 900, 900, 600, 500]
    assert "our Company" in bp.our_business.style_notes
    assert bp.our_business.subsections[1].content_elements[2] == "Pan-India distribution network"
    assert bp.risk_factors.themes[4].group == "Risks Relating to the Offer"
    assert bp.our_business.subsections[0].map_key == "overview"


def test_themes_and_subsections_link_to_page_mapping():
    bp = from_llm_json(parse_blueprint_json(BASE), TID)
    assert [s.map_key for s in bp.our_business.subsections] == [
        "overview", "strengths", "strategies", "products", "customers", "risk_compliance"]
    keys = {t.theme: t.map_key for t in bp.risk_factors.themes}
    assert keys["Dependence on market performance of AUM"] == "market"
    assert keys["Distributor concentration"] == "concentration"
    assert keys["Regulatory change on expense ratios"] == "regulatory"
    assert keys["Offer price and volatility"] is None            # offer risks: no company pages


@pytest.mark.parametrize("value,expected", [(600, 600), ("1,200", 1200), ("800-1000", 900),
                                            ("approx. 600 words", 600), ("", None), (None, None)])
def test_target_words_forms(value, expected):
    assert _int_or_none(value) == expected


def test_unreadable_json_gives_clear_message():
    with pytest.raises(ValueError, match="could not be read|no 'our_business'"):
        parse_blueprint_json('TASK_ID: x\n```json\n{"our_business": {"subsections": [ {"title" "x" ]}\n```')
    with pytest.raises(ValueError, match="No JSON"):
        parse_blueprint_json("TASK_ID: x\nSorry, I cannot help with that.")


def test_ingest_ipo01_reply_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "deal_dir", lambda d: tmp_path / d)
    save_task("deal", Task(task_id=TID, kind="IPO-01", created="x"))
    task, bp = ingest_reply("deal", _docx(BASE, smart=True), "stylus_reply.docx", expected_task=TID)
    assert task.status == "reply_parsed" and not task.errors
    assert bp.source == TID and len(bp.our_business.subsections) == 6
    saved = (tmp_path / "deal" / "blueprint.json").read_text(encoding="utf-8")
    assert "Our Competitive Strengths" in saved
