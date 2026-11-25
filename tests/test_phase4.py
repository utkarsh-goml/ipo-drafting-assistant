"""Phase 4 tests: draft checks, edits with versions, IPO-04 merge, export."""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import replies, store, validator, workspace  # noqa: E402
from core.company_docs import Unit  # noqa: E402
from core.packer import _text_blocks  # noqa: E402
from core.replies import Draft, DraftItem  # noqa: E402
from core.tasks import Task, save_task  # noqa: E402


def U(n, lines):
    return Unit(doc_id="D1", unit_no=n, pdf_page=n, lines=lines, printed_page=n)


UNITS = [U(145, ["Revenue from operations 4,06,332.14 3,27,318.31", "Profit for the year 2,54,015.32"]),
         U(5, ["We are promoted by State Bank of India", "AUM 10,72,000 crore"])]


# ----------------------------------------------------------------- numbers
@pytest.mark.parametrize("a,b", [("4,06,332.14", "406332.140"), ("(1,250.22)", "1250.22"), ("24.10 %", "24.1"),
                                 ("0.50", "0.5"), ("007", "7")])
def test_num_key_normalises(a, b):
    assert validator.num_key(a) == validator.num_key(b)


def test_numbers_to_check_skips_years_counts_ids_and_citations():
    nums = validator.numbers_to_check("In 2025 we had 12 branches and ₹4,06,332.14 lakhs, up 24.1% [D1 p.145] OB01 [TBD: 5,000]")
    assert nums == ["4,06,332.14", "24.1%"]


def test_check_numbers_flags_missing_and_uncited():
    text = ("Revenue was ₹4,06,332.14 lakhs and AUM ₹12,34,567 crore [D1 p.145].\n\n"
            "We had 3,722 employees.\n\n"
            "AUM ₹10,72,000 crore [D1 p.145] [D1 p.5].")
    iss = validator.check_numbers(text, UNITS, None)
    msgs = [(i.kind, i.severity, i.message) for i in iss]
    assert any(k == "number" and s == "high" and "12,34,567" in m for k, s, m in msgs)
    assert any(k == "citation" and "3,722" in m for k, s, m in msgs)
    assert not any("4,06,332.14" in m or "10,72,000" in m for _, _, m in msgs)


def test_table_rows_borrow_source_line_citation():
    text = "| Metric | FY25 |\n|---|---|\n| Revenue | 4,06,332.14 |\n\nSource: [D1 p.145]"
    assert validator.check_numbers(text, UNITS, None) == []


def test_number_on_other_cited_page_is_low_severity():
    text = "Revenue was 4,06,332.14 [D1 p.5].\n\nWe are promoted by SBI [D1 p.145]."
    iss = validator.check_numbers(text, UNITS, None)
    assert [i.severity for i in iss] == ["low"]


def test_copy_check_and_rf_rules():
    comp = ["The market price of our Equity Shares may be volatile and may fluctuate significantly after the Offer."]
    iss = validator.check_copying("The market price of our Equity Shares may be volatile and may fluctuate significantly after this Offer.", comp)
    assert iss and iss[0].kind == "copy"
    rf = validator.check_rf_item("Price", "However, we believe our controls are adequate.")
    kinds = [i.message for i in rf]
    assert any("Mitigation" in m for m in kinds) and any("full sentence" in m for m in kinds)
    good = validator.check_rf_item("A decline in our AUM could adversely affect our revenue and profitability.", "Body.")
    assert good == []


def test_uncovered_required():
    req = [("contingent", "Contingent liabilities could adversely affect our financial condition"),
           ("offer_price", "The Offer Price may not be indicative of the market price")]
    iss = validator.uncovered_required(["Our contingent liabilities could adversely affect our financial condition."],
                                       [""], req)
    assert [i.excerpt for i in iss] == ["offer_price"]


def test_uncovered_required_full_sentence_headings():
    """DRHP-style headings worded differently from the page-3 titles still count as covered."""
    req = [("litigation", "Outstanding legal proceedings involving the Company"),
           ("attrition", "Inability to attract and retain key personnel; employee attrition"),
           ("concentration", "Dependence on a limited number of customers / products / channels"),
           ("negative_ocf_FY2024", "We have had negative cash flows from operating activities"),
           ("statutory_dues", "Delays in payment of statutory dues"),
           ("custom_esg", "Climate and ESG disclosure obligations")]
    heads = ["Our Company, Promoter and Directors are involved in certain legal proceedings, and any adverse "
             "outcome may adversely affect our business.",
             "We depend on our Key Managerial Personnel and Senior Management, and the loss of any of them may "
             "adversely affect our business.",
             "A large part of our AUM comes from a few schemes.",
             "We have experienced negative operating cash flows in the past."]
    texts = ["", "", "Our top five schemes accounted for [__]% of our AUM [D1 p.40].", ""]
    iss = validator.uncovered_required(heads, texts, req)
    # covered: litigation (heading), attrition (heading), concentration (opening paragraph), ocf (heading)
    assert [i.excerpt for i in iss] == ["statutory_dues", "custom_esg"]
    # a custom risk is covered when most of its title's words are present
    iss = validator.uncovered_required(["Our ESG and climate disclosure obligations may increase our costs."],
                                       [""], [("custom_esg", "Climate and ESG disclosure obligations")])
    assert iss == []


def test_uncovered_required_terms_only_in_opening_paragraph():
    """Key terms later in the body (not the opening paragraph) do not count."""
    req = [("litigation", "Outstanding legal proceedings involving the Company")]
    body = "Our revenue depends on market conditions.\n\nWe are also party to some legal proceedings."
    iss = validator.uncovered_required(["Market volatility could reduce our revenue."], [body], req)
    assert [i.excerpt for i in iss] == ["litigation"]


def test_check_item_flags_comparable_and_unknown_cites():
    item = DraftItem(id="RF01", title="x", text="Prices fall [C1 p.40] and [D1 p.999].")
    msgs = [i.message for i in validator.check_item("our_business", item, UNITS, None, [])]
    assert any("Comparable page" in m for m in msgs) and any("unknown page" in m for m in msgs)


# ----------------------------------------------------------------- edits / versions / IPO-04 / export
@pytest.fixture
def deal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path, raising=False)
    monkeypatch.setattr(store, "deal_dir", lambda d: tmp_path / d)
    (tmp_path / "t").mkdir()
    monkeypatch.setattr(store, "load_meta", lambda d: {"name": "Test Co"})
    from core import context
    monkeypatch.setattr(context, "load_units", lambda d: UNITS)
    draft = Draft(section="our_business", task_id="IPO02-20261118-000000", created="x", items=[
        DraftItem(id="OB01", title="Overview", text="Revenue 4,06,332.14 [D1 p.145].", source="IPO02-20261118-000000"),
        DraftItem(id="OB02", title="Strengths", text="| A | B |\n|---|---|\n| **x** | 1 [D1 p.5] |")],
        gaps=["Headcount"])
    replies.save_draft("t", draft)
    return "t"


def test_edit_keeps_versions_and_restore(deal):
    assert workspace.save_item_edit(deal, "our_business", "OB01", "Overview", "Revenue 4,06,332.14 [D1 p.145].") is None
    it = workspace.save_item_edit(deal, "our_business", "OB01", "Overview", "New text [D1 p.5].")
    assert it.source == "edit" and len(it.versions) == 1 and it.citations == ["D1 p.5"]
    it = workspace.restore_version(deal, "our_business", "OB01", 0)
    assert it.text.startswith("Revenue") and len(it.versions) == 2
    assert replies.load_draft(deal, "our_business").items[0].text.startswith("Revenue")


def test_ipo04_reply_replaces_only_target(deal, monkeypatch):
    from core import tasks
    monkeypatch.setattr(tasks, "tasks_root", lambda d: (store.deal_dir(d) / "tasks"))
    (store.deal_dir(deal) / "tasks").mkdir()
    t = Task(task_id="IPO04-20261118-000001", kind="IPO-04", created="x",
             target={"section": "our_business", "item_id": "OB02", "instructions": ""})
    save_task(deal, t)
    reply = "TASK_ID: IPO04-20261118-000001\n### OB02 Our Strengths\nRevised [D1 p.5].\n"
    _, d = replies.ingest_reply(deal, reply.encode(), "r.md", expected_task=t.task_id)
    o1, o2 = d.items
    assert o1.text.startswith("Revenue") and not o1.versions
    assert o2.title == "Our Strengths" and o2.source == t.task_id and len(o2.versions) == 1


def test_export_markdown_and_docx(deal):
    md = workspace.section_markdown(deal, "our_business")
    assert md.startswith("# OUR BUSINESS") and "### Overview" in md and "- Headcount" in md
    import docx
    doc = docx.Document(io.BytesIO(workspace.section_docx(deal, "our_business")))
    texts = [p.text for p in doc.paragraphs]
    assert "Overview" in texts and any("[D1 p.145]" in t for t in texts)
    assert len(doc.tables) == 1 and doc.tables[0].cell(1, 0).text == "x"


def test_text_blocks_for_task_sheet():
    b = _text_blocks("Para one.\n\n| A | B |\n|---|---|\n| 1 |\n\n#### Sub\nBody")
    assert b[0] == ("p", "Para one.")
    assert b[1] == ("table", [["A", "B"], ["1", ""]])
    assert b[2:] == [("h3", "Sub"), ("p", "Body")]
