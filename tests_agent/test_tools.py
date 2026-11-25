"""Step 3: tools over core/. Fast tests on a temp deal; a real-document run gated by IPO_SAMPLE_AR + IPO_SAMPLE_DRHP."""
import json
import os
from pathlib import Path

import pytest

from agent import tools as TL
from core import context, replies, section_extractor, store
from core.company_docs import Unit
from core.financials import Figure, Indicators
from core.models import ExtractedSection, PageText
from core.replies import Draft, DraftItem
from core.risk_triggers import Evidence, Trigger


def _pdf(path: Path, pages):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return path


UNITS = [
    Unit(doc_id="D1", unit_no=1, pdf_page=1, printed_page=None, lines=["Annual Report 2024-25"]),
    Unit(doc_id="D1", unit_no=5, pdf_page=5, printed_page=5, part="Corporate Overview",
         lines=["We are the largest asset management company in India by quarterly average assets under management "
                "with a wide network of branches and distributors across the country and many investor folios."]),
    Unit(doc_id="D1", unit_no=145, pdf_page=145, printed_page=145, part="Financial Statements",
         lines=["Consolidated Statement of Profit and Loss", "Profit for the year 2,54,015.32 2,07,278.35"]),
]


@pytest.fixture
def deal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "load_units", lambda d: UNITS)
    deal_id = TL.create_deal("Test AMC", "Asset Management", "FY2025")["deal_id"]
    return deal_id


def _save_context(deal_id):
    ind = Indicators(basis="consolidated", unit="lakhs", years=["FY2025", "FY2024"], figures={
        "pat": Figure(key="pat", label="Profit for the year (PAT)", values=[254015.32, 207278.35],
                      cite="D1 p.145", source_line="Profit for the year")})
    context.save_indicators(deal_id, ind)
    context.save_triggers(deal_id, [Trigger(key="reg", title="Regulatory risk", category="Internal", mandatory=True,
                                            reason="SEBI", evidence=[Evidence(cite="D1 p.5", location="PDF page 5",
                                                                              snippet="x" * 400)])])


def _save_draft(deal_id, text):
    item = DraftItem(id="OB01", title="Overview", text=text)
    replies.save_draft(deal_id, Draft(section="our_business", task_id="agent", created="2026-11-24T10:00:00",
                                      items=[item]))


# --------------------------------------------------------------------------- deals and documents
def test_create_and_unknown_deal(deal):
    assert deal == "test-amc"
    with pytest.raises(TL.ToolError, match="Unknown deal 'nope'"):
        TL.list_documents("nope")
    with pytest.raises(TL.ToolError, match="needs a name"):
        TL.create_deal("  ", "", "")


def test_add_and_list_documents(deal, tmp_path):
    pdf = _pdf(tmp_path / "ar.pdf", ["page one", "page two"])
    d = TL.add_document(deal, str(pdf), "company", "ANNUAL_REPORT")
    assert d == {"doc_id": "D1", "kind": "company", "name": "ar.pdf", "doc_type": "ANNUAL_REPORT", "page_count": 2}
    c = TL.add_document(deal, str(_pdf(tmp_path / "drhp.pdf", ["x"])), "comparable")
    assert c["doc_id"] == "C1"
    docs = TL.list_documents(deal)["documents"]
    assert [x["doc_id"] for x in docs] == ["D1", "C1"] and all(x["file_present"] for x in docs)
    with pytest.raises(TL.ToolError, match="needs a doc_type"):
        TL.add_document(deal, str(pdf), "company")
    with pytest.raises(TL.ToolError, match="Not a PDF"):
        TL.add_document(deal, str(tmp_path / "missing.pdf"), "comparable")


def _sec(method, sv=True, ev=True, key="our_business"):
    return ExtractedSection(doc_id="C1", doc_name="drhp.pdf", section_key=key, section_title="Our Business",
                            start_page=10, start_line=1, end_page=20, end_line=5, method=method,
                            start_verified=sv, end_verified=ev, confidence=0.9,
                            pages=[PageText(page_no=10, lines=["OUR BUSINESS"])])


@pytest.mark.parametrize("found,needs", [
    ({"our_business": "toc", "risk_factors": "toc"}, False),
    ({"our_business": "toc", "risk_factors": "heading_scan"}, True),     # fallback method: banker decides
    ({"our_business": "toc", "risk_factors": None}, True),               # not found: banker decides
])
def test_extract_sections_reports_what_needs_the_banker(deal, tmp_path, monkeypatch, found, needs):
    TL.add_document(deal, str(_pdf(tmp_path / "drhp.pdf", ["x"])), "comparable")
    monkeypatch.setattr(section_extractor, "extract_sections", lambda path, doc_id, doc_name: {
        k: (_sec(m, key=k) if m else None) for k, m in found.items()})
    res = TL.extract_sections(deal, "C1")
    assert res["needs_banker"] is needs
    assert {s["section"]: s["accepted"] for s in res["sections"]} == {k: m == "toc" for k, m in found.items() if m}
    assert set(store.load_extractions(deal)) == {("C1", k) for k, m in found.items() if m}


def test_unverified_toc_section_is_not_accepted():
    assert TL.section_summary(_sec("toc", sv=True, ev=False))["accepted"] is False


def test_extract_refuses_company_doc_and_bad_range(deal, tmp_path):
    TL.add_document(deal, str(_pdf(tmp_path / "ar.pdf", ["x"])), "company", "ANNUAL_REPORT")
    with pytest.raises(TL.ToolError, match="company document"):
        TL.extract_sections(deal, "D1")
    TL.add_document(deal, str(_pdf(tmp_path / "drhp.pdf", ["a", "b"])), "comparable")
    with pytest.raises(TL.ToolError, match=r"<= 2"):
        TL.set_section_range(deal, "C1", "our_business", 1, 5)
    with pytest.raises(TL.ToolError, match="no document 'C9'"):
        TL.extract_sections(deal, "C9")


# --------------------------------------------------------------------------- context
def test_figures_triggers_pages(deal):
    with pytest.raises(TL.ToolError, match="run build_context first"):
        TL.get_figures(deal)
    _save_context(deal)
    f = TL.get_figures(deal)
    assert f["basis"] == "consolidated" and f["figures"]["pat"] == {
        "label": "Profit for the year (PAT)", "values": [254015.32, 207278.35], "cite": "D1 p.145"}
    upd = TL.update_figures(deal, [TL.FigureRow(key="pat", label="Profit for the year (PAT)",
                                                current=254015.32, previous=207000.0)])
    assert upd["figures"]["pat"]["values"] == [254015.32, 207000.0]
    assert upd["figures"]["pat"]["cite"] == "D1 p.145"
    trig = TL.list_risk_triggers(deal)["triggers"]
    assert trig[0]["required"] is True and len(trig[0]["evidence"][0]["snippet"]) <= 160
    assert TL.get_page(deal, "[D1 p.145]")["text"].endswith("2,54,015.32 2,07,278.35")
    assert TL.get_page(deal, "D1 u.1")["cite"] == "D1 u.1"
    with pytest.raises(TL.ToolError, match="No company page matches 'D1 p.999'"):
        TL.get_page(deal, "D1 p.999")
    hits = TL.search_company_pages(deal, "asset management branches distributors", k=3)["hits"]
    assert hits[0]["cite"] == "D1 p.5"


# --------------------------------------------------------------------------- blueprint
BP = {"source": "test",
      "our_business": {"style_notes": "", "subsections": [{"id": "OB01", "title": "Overview"}]},
      "risk_factors": {"themes": [{"id": "RF01", "group": "Internal Risks", "theme": "Regulatory risk"}]}}


def test_blueprint_set_get(deal):
    with pytest.raises(TL.ToolError, match="no blueprint"):
        TL.get_blueprint(deal)
    assert TL.set_blueprint(deal, BP) == {"subsections": 1, "themes": 1, "source": "test"}
    got = TL.get_blueprint(deal)
    assert got["our_business"]["subsections"][0]["map_key"] == "overview"
    with pytest.raises(TL.ToolError, match="at least one"):
        TL.set_blueprint(deal, {**BP, "risk_factors": {"themes": []}})
    two = json.loads(json.dumps(BP))
    two["our_business"]["subsections"].append({"id": "OB01", "title": "Again"})
    with pytest.raises(TL.ToolError, match="Duplicate ids: OB01"):
        TL.set_blueprint(deal, two)


# --------------------------------------------------------------------------- drafts
def test_check_edit_restore_export(deal):
    _save_context(deal)
    with pytest.raises(TL.ToolError, match="no our_business draft"):
        TL.check_item(deal, "our_business", "OB01")
    _save_draft(deal, "Our profit for the year was ₹2,54,015.32 lakhs [D1 p.145].")
    ok = TL.check_item(deal, "our_business", "OB01")
    assert ok["high"] == 0, ok
    bad = TL.edit_item(deal, "our_business", "OB01", "Our profit was ₹9,99,999.00 lakhs [D1 p.145; C1 p.160].")
    assert bad["changed"] and bad["versions"] == 1 and bad["comparable_cites"] == ["C1 p.160"]
    chk = TL.check_item(deal, "our_business", "OB01")
    msgs = " | ".join(i["message"] for i in chk["issues"])
    assert chk["high"] >= 2 and "C1 p.160" in msgs and "9,99,999" in msgs
    assert TL.edit_item(deal, "our_business", "OB01", "Our profit was ₹9,99,999.00 lakhs [D1 p.145; C1 p.160].")[
        "changed"] is False
    back = TL.restore_version(deal, "our_business", "OB01", 0)
    assert back["comparable_cites"] == [] and back["versions"] == 2
    with pytest.raises(TL.ToolError, match="index 7 does not exist"):
        TL.restore_version(deal, "our_business", "OB01", 7)
    with pytest.raises(TL.ToolError, match="no item 'OB09'"):
        TL.check_item(deal, "our_business", "OB09")
    for fmt in ("md", "docx"):
        r = TL.export_section(deal, "our_business", fmt)
        assert Path(r["path"]).exists() and r["bytes"] > 0 and r["items"] == 1
    assert "2,54,015.32" in Path(TL.export_section(deal, "our_business", "md")["path"]).read_text(encoding="utf-8")


def test_tools_are_langchain_tools(deal):
    assert set(TL.TOOLS_BY_NAME) == {f.__name__ for f in TL.FUNCTIONS}
    assert all(t.description for t in TL.TOOLS)
    out = TL.TOOLS_BY_NAME["list_documents"].invoke({"deal_id": deal})
    assert out["name"] == "Test AMC"
    schema = TL.TOOLS_BY_NAME["set_section_range"].args
    assert schema["section"]["enum"] == ["our_business", "risk_factors"]
    with pytest.raises(TL.ToolError):                       # errors are raised, not swallowed
        TL.TOOLS_BY_NAME["get_page"].invoke({"deal_id": deal, "cite": "D7 p.1"})


# --------------------------------------------------------------------------- real documents
SAMPLE_AR = os.environ.get("IPO_SAMPLE_AR")
SAMPLE_DRHP = os.environ.get("IPO_SAMPLE_DRHP")


@pytest.mark.skipif(not (SAMPLE_AR and SAMPLE_DRHP and Path(SAMPLE_AR).exists() and Path(SAMPLE_DRHP).exists()),
                    reason="IPO_SAMPLE_AR and IPO_SAMPLE_DRHP not set")
def test_every_tool_on_real_documents(tmp_path, monkeypatch):
    """SBI Funds AR 2024-25 (D1) + ICICI Pru AMC DRHP (C1): every tool, real core code, temp data folder."""
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    deal = TL.create_deal("SBI Funds Test", "Asset Management", "FY2025")["deal_id"]
    assert TL.add_document(deal, SAMPLE_AR, "company", "ANNUAL_REPORT")["doc_id"] == "D1"
    assert TL.add_document(deal, SAMPLE_DRHP, "comparable")["doc_id"] == "C1"

    ext = TL.extract_sections(deal, "C1")
    pages = {s["section"]: s["pdf_pages"] for s in ext["sections"]}
    assert pages == {"our_business": [160, 184], "risk_factors": [35, 65]}
    assert ext["needs_banker"] is False
    ob = TL.set_section_range(deal, "C1", "our_business", 160, 184)
    assert ob["method"] == "manual" and ob["start_verified"]

    ctx = TL.build_context(deal)
    assert ctx["units"] == 206
    pat = TL.get_figures(deal)["figures"]["pat"]
    assert pat["values"][0] == 254015.32 and pat["cite"].startswith("D1 p.")
    assert TL.list_risk_triggers(deal)["triggers"]
    hit = TL.search_company_pages(deal, "assets under management market share", k=3)["hits"][0]
    page = TL.get_page(deal, hit["cite"])
    assert page["cite"] == hit["cite"] and page["text"]
    assert TL.get_page(deal, pat["cite"])["text"]

    TL.set_blueprint(deal, BP)
    assert TL.get_blueprint(deal)["our_business"]["subsections"][0]["title"] == "Overview"
    _save_draft(deal, f"Our profit for the year was ₹2,54,015.32 lakhs [{pat['cite']}].")
    assert TL.check_item(deal, "our_business", "OB01")["high"] == 0
    TL.edit_item(deal, "our_business", "OB01", f"Our profit was ₹2,54,015.32 lakhs [{pat['cite']}]. [TBD: AUM]")
    assert TL.restore_version(deal, "our_business", "OB01", 0)["tbd"] == 0
    assert Path(TL.export_section(deal, "our_business", "docx")["path"]).stat().st_size > 0
    TL.update_figures(deal, [TL.FigureRow(key="pat", label="Profit for the year (PAT)",
                                          current=254015.32, previous=207278.35)])
