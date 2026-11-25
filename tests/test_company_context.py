"""Phase 2 tests: company-document units, part tagging, financial parsing, triggers, retrieval.

Integration test: set IPO_SAMPLE_AR to the SBI Funds Management AR 2024-25 PDF.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.company_docs import (  # noqa: E402
    Unit, _dedupe_printed_numbers, _fill_printed_numbers, find_unit, fix_drop_caps, strip_running_headers, tag_parts,
)
from core.financials import compact, extract_indicators, split_row  # noqa: E402
from core.retrieval import BM25, UnitIndex, pick_sector_pack, tokenize  # noqa: E402
from core.risk_triggers import find_triggers  # noqa: E402


def U(n, lines, part="Other", printed=None):
    return Unit(doc_id="D1", unit_no=n, pdf_page=n, lines=lines, part=part, printed_page=printed)


# ----------------------------------------------------------------- financial row parsing
@pytest.mark.parametrize("line,label,values", [
    ("(iii) Employee benefits expenses 22 42,107.78 36,838.76", "(iii) Employee benefits expenses", [42107.78, 36838.76]),
    ("(iv) Depreciation and amortization expense 9-11 3,943.71 3,699.63", "(iv) Depreciation and amortization expense", [3943.71, 3699.63]),
    ("VI. Exceptional Items - -", "VI. Exceptional Items", [0.0, 0.0]),
    ("i ) Remeasurements of the defined benefit plans (1,250.22) (81.05)", "i ) Remeasurements of the defined benefit plans", [-1250.22, -81.05]),
    ("Revenue from Operations", "Revenue from Operations", []),
    # review fixes: unformatted amounts kept by position, alphanumeric note numbers stripped
    ("Finance cost 40 860 771", "Finance cost", [860.0, 771.0]),
    ("Other income 12 45 30", "Other income", [45.0, 30.0]),
    ("Revenue from operations 2,345 1,980", "Revenue from operations", [2345.0, 1980.0]),
    ("(f) Investments accounted for using equity method 6A 5,717.49 4,255.43",
     "(f) Investments accounted for using equity method", [5717.49, 4255.43]),
    ("Other income 21 17,282.78", "Other income", [17282.78]),
])
def test_split_row(line, label, values):
    lab, vals = split_row(line)
    assert lab == label and vals == values


def test_split_row_three_year_restated():
    lab, vals = split_row("Revenue from operations 19 1,200.0 1,000.0 800.0", n_cols=3)
    assert vals == [1200.0, 1000.0, 800.0]


@pytest.mark.parametrize("raw,fixed", [
    ("F inance cost", "Finance cost"),
    ("T he Audit Committee met four times", "The Audit Committee met four times"),
    ("30. M aterial Orders by Regulator", "30. Material Orders by Regulator"),
    ("( b) D uring the year", "( b) During the year"),
    ("Annexure B to the Auditor's Report", "Annexure B to the Auditor's Report"),
    ("Category B investors and Schedule C of the Act", "Category B investors and Schedule C of the Act"),
    ("A nnual report", "A nnual report"),  # 'A' is a real word: left alone
])
def test_fix_drop_caps(raw, fixed):
    assert fix_drop_caps(raw) == fixed


def test_cover_pages_never_collide_with_printed_pages():
    # cover + inside cover unnumbered; printed numbering starts at unit 3
    units = [U(i, ["x"], printed=(i - 2 if i >= 3 else None)) for i in range(1, 12)]
    _fill_printed_numbers(units)
    _dedupe_printed_numbers(units)
    cites = [u.cite for u in units]
    assert cites[:3] == ["D1 u.1", "D1 u.2", "D1 p.1"]
    assert len(set(cites)) == len(cites)
    assert find_unit(units, "D1 u.2").unit_no == 2 and find_unit(units, "[D1 p.2]").unit_no == 4


def test_december_year_end():
    pl = U(1, ["Statement of Profit and Loss for the year ended December 31, 2024", "(₹ in million)",
               "Particulars Note December 31, 2024 December 31, 2023",
               "Revenue from operations 18 900.5 750.0"], part="Financial Statements - Standalone", printed=50)
    ind = extract_indicators([pl])
    assert ind.years == ["FY2024", "FY2023"] and ind.unit == "millions"
    assert ind.value("revenue_ops") == 900.5


@pytest.mark.parametrize("label,expected", [
    ("X. Profit / (loss) for the period (VII+VIII-IX)", "profitlossfortheperiod"),
    ("VII. Profit/(loss) before tax (V-VI)", "profitlossbeforetax"),
    ("(a) Cash & Cash Equivalents", "cashandcashequivalents"),
    ("Net Cash used in Investing Activities (B)", "netcashusedininvestingactivitiesb"),
])
def test_compact(label, expected):
    assert compact(label) == expected


def _statements():
    pl = U(10, ["Consolidated Statement of Profit and Loss", "(H In lakhs)",
                "Particulars Note No. March 31, 2025 March 31, 2024",
                "I. Total revenue from operations 1,000.00 800.00",
                "II. Other Income 21 50.00 40.00",
                "III. Total Income (I+II) 1,050.00 840.00",
                "(i) Finance cost 40 10.00 8.00",
                "(iv) Depreciation and amortization expense 9-11 20.00 18.00",
                "VII. Profit/(loss) before tax (V-VI) 300.00 250.00",
                "X. Profit / (loss) for the period (VII+VIII-IX) 220.00 240.00"],
           part="Financial Statements - Consolidated", printed=101)
    bs = U(9, ["Consolidated Balance Sheet", "As at March 31, 2025",
               "Total Assets 5,000.00 4,000.00", "Sub-total - Equity 2,000.00 1,800.00",
               "(a) Borrowings 12 3,000.00 2,000.00"],
           part="Financial Statements - Consolidated", printed=100)
    cf = U(11, ["Consolidated Statement of Cash Flows",
                "Net cash (used in) / generated from Operating Activities (A) (120.00) 90.00"],
           part="Financial Statements - Consolidated", printed=102)
    return [bs, pl, cf]


def test_extract_indicators_and_derived():
    ind = extract_indicators(_statements())
    assert ind.basis == "consolidated" and ind.unit == "lakhs" and ind.years == ["FY2025", "FY2024"]
    assert ind.value("revenue_ops") == 1000.0 and ind.value("pat", 1) == 240.0
    assert ind.figures["pat"].cite == "D1 p.101"
    assert ind.value("ocf") == -120.0
    # EBITDA = PBT + finance + D&A - other income = 300 + 10 + 20 - 50
    assert ind.derived["ebitda"][0] == pytest.approx(280.0)
    assert ind.derived["debt_equity"][0] == pytest.approx(1.5)
    assert ind.derived["revenue_ops_growth"][0] == pytest.approx(0.25)


def test_financial_triggers_fire():
    units = _statements()
    keys = {t.key for t in find_triggers(units, extract_indicators(units))}
    assert "negative_ocf_FY2025" in keys       # OCF -120
    assert "pat_decline" in keys               # 220 < 240
    assert "high_leverage" in keys             # D/E 1.5
    assert {"offer_price", "no_prior_market"} <= keys


def test_text_trigger_with_evidence():
    units = [U(1, ["Notes", "Contingent liabilities not provided for: claims against the company 12.5"],
               part="Notes - Consolidated", printed=150)]
    t = next(t for t in find_triggers(units, extract_indicators(units)) if t.key == "contingent_liabilities")
    assert t.mandatory and t.evidence[0].cite == "D1 p.150"


# ----------------------------------------------------------------- units / parts
def test_tag_parts_and_headers():
    units = [U(1, ["XYZ Annual Report", "Corporate overview text"]),
             U(2, ["XYZ Annual Report", "Directors' Report", "To the members, the Board presents..."]),
             U(3, ["XYZ Annual Report", "We thank our auditors. Revised Independent Auditor's Report on consolidated statements was received"]),
             U(4, ["XYZ Annual Report", "Notes to the Standalone Financial Statements", "1. Corporate info"]),
             U(5, ["XYZ Annual Report", "Notes to the Standalone Financial Statements", "2. Policies"])]
    tag_parts(units)
    assert [u.part for u in units] == ["Corporate Overview", "Directors' Report", "Directors' Report",
                                       "Notes - Standalone", "Notes - Standalone"]
    strip_running_headers(units)
    assert all("XYZ Annual Report" not in u.lines for u in units)


def test_printed_numbers_inferred_and_citation_lookup():
    units = [U(i, ["x"], printed=(i + 2 if i in (3, 4, 5, 6) else None)) for i in range(1, 9)]
    units[6].printed_page = 99  # a stray number misread as page number
    _fill_printed_numbers(units)
    assert [u.printed_page for u in units] == [3, 4, 5, 6, 7, 8, 9, 10]
    assert find_unit(units, "[D1 p.7]").unit_no == 5


# ----------------------------------------------------------------- retrieval
def test_bm25_ranks_relevant_page_first():
    docs = [tokenize(t) for t in ["employees attrition training talent headcount", "revenue profit growth",
                                  "cyber security data breach"]]
    s = BM25(docs).scores(tokenize("employee attrition talent"))
    assert s.index(max(s)) == 0


def test_unit_index_penalises_notes_for_business():
    filler = " lorem" * 50
    units = [U(1, ["We have 3,000 employees and low attrition" + filler], part="Directors' Report"),
             U(2, ["employees employees attrition gratuity" + filler], part="Notes - Consolidated")]
    hits = UnitIndex(units).search("employees attrition", penalize_prefixes=["Notes"])
    assert hits[0][0].unit_no == 1


def test_sector_pack():
    assert pick_sector_pack("Asset Management") == "asset_management"
    assert pick_sector_pack("Specialty Chemicals manufacturing") == "manufacturing"
    assert pick_sector_pack("") is None


SAMPLE_AR = os.environ.get("IPO_SAMPLE_AR")


@pytest.mark.skipif(not SAMPLE_AR or not Path(SAMPLE_AR).exists(), reason="IPO_SAMPLE_AR not set")
def test_real_annual_report():
    from core.company_docs import part_ranges, read_units
    units = read_units(SAMPLE_AR, "D1")
    assert len(units) == 206
    parts = {r["part"]: (r["first"], r["last"]) for r in part_ranges(units)}
    assert parts["Directors' Report"][0] == "24"
    assert parts["Notes - Consolidated"] == ("150", "204")
    cites = [u.cite for u in units]
    assert len(set(cites)) == len(cites)          # no duplicate citations
    assert cites[0] == "D1 u.1"                  # unnumbered cover
    ind = extract_indicators(units)
    assert ind.basis == "consolidated"
    assert ind.value("pat") == pytest.approx(254015.32)
    assert ind.value("total_equity") == pytest.approx(829753.06)
