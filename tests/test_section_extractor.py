"""Unit tests on synthetic page text + optional integration test on a real DRHP.

Integration test: set IPO_SAMPLE_DRHP to a DRHP path, e.g. the ICICI Prudential AMC
DRHP (Risk Factors = PDF p35-65, Our Business = PDF p160-184).
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.section_extractor import (  # noqa: E402
    end_before, extract_from_pages, find_heading_on_page, heading_key, load_config,
    looks_like_heading, page_offset, parse_toc, search_pages,
)

CFG = load_config()
OFFSET = 4  # synthetic: printed page 1 == PDF page 5


def make_doc():
    """40-page fake DRHP: cover(2) + TOC(1) + filler, with printed page numbers."""
    pages = [["DRAFT RED HERRING PROSPECTUS", "XYZ LIMITED"], ["Notice to investors"],
             ["TABLE OF CONTENTS",
              "SECTION I: GENERAL ......................... 1",
              "SECTION II: RISK FACTORS .................. 5",
              "SECTION III: INTRODUCTION ................ 12",
              "THE OFFER ................................ 12",
              "INDUSTRY OVERVIEW ........................ 15",
              "OUR BUSINESS ............................. 20",
              "KEY REGULATIONS AND POLICIES IN INDIA .... 28",
              "CERTAIN CONVENTIONS, PRESENTATION OF FINANCIAL, INDUSTRY AND",
              "MARKET DATA .............................. 30"],
             ["Blank page"]]
    for printed in range(1, 37):
        pages.append([str(printed), f"Body text of printed page {printed}."])
    pdf = lambda printed: printed + OFFSET - 1  # 0-based index  # noqa: E731
    pages[pdf(5)] = ["5", "SECTION II: RISK FACTORS", "An investment involves risk.",
                     "For details see 'Our Business' on page 20."]
    pages[pdf(12)] = ["12", "SECTION III: INTRODUCTION", "THE OFFER", "Offer details."]
    pages[pdf(20)] = ["20", "OUR BUSINESS", "Overview", "We are a company."]
    # next section starts mid-page
    pages[pdf(28)] = ["28", "Last business paragraph.", "KEY REGULATIONS AND POLICIES IN INDIA",
                      "Regulations text."]
    return pages


def test_parse_toc_joins_wrapped_titles():
    toc = parse_toc(make_doc(), [2])
    titles = [e.title for e in toc]
    assert "OUR BUSINESS" in titles
    assert titles[-1] == "CERTAIN CONVENTIONS, PRESENTATION OF FINANCIAL, INDUSTRY AND MARKET DATA"
    assert next(e for e in toc if e.title == "SECTION II: RISK FACTORS").is_part


def test_page_offset():
    offset, support = page_offset(make_doc())
    assert offset == OFFSET and support >= 30


def test_heading_key_strips_part_prefix():
    assert heading_key("Section II: Risk Factors") == "RISK FACTORS"


def test_cross_reference_is_not_a_heading():
    line = "For details see 'Our Business' on page 20."
    assert find_heading_on_page([line], ["OUR BUSINESS"], 90, None) is None


def test_our_business_ends_mid_page_before_next_heading():
    sec = extract_from_pages(make_doc(), doc_id="C1", doc_name="x", section_key="our_business", cfg=CFG)
    assert sec.method == "toc"
    assert (sec.start_page, sec.start_line) == (20 + OFFSET, 2)
    assert (sec.end_page, sec.end_line) == (28 + OFFSET, 2)  # line before the heading
    assert sec.pages[-1].lines == ["Last business paragraph."]
    assert sec.next_section_title == "KEY REGULATIONS AND POLICIES IN INDIA"


def test_risk_factors_ends_on_previous_page():
    sec = extract_from_pages(make_doc(), doc_id="C1", doc_name="x", section_key="risk_factors", cfg=CFG)
    assert sec.start_page == 5 + OFFSET
    assert sec.end_page == 11 + OFFSET  # SECTION III heading is at top of next page
    assert sec.pages[0].lines[0] == "SECTION II: RISK FACTORS"


def test_heading_scan_fallback_without_toc():
    pages = [p for i, p in enumerate(make_doc()) if i != 2]  # drop TOC page
    sec = extract_from_pages(pages, doc_id="C1", doc_name="x", section_key="our_business", cfg=CFG)
    assert sec.method == "heading_scan"
    assert sec.pages[0].lines[0] == "OUR BUSINESS"
    assert sec.pages[-1].lines == ["Last business paragraph."]


def test_end_before_top_of_page():
    pages = [["a", "b", "c"], ["7", "NEXT SECTION", "x"]]
    assert end_before(pages, 1, 1) == (1, 3)


@pytest.mark.parametrize("text,ok", [
    ("Our Strengths", True),
    ("12. We depend on third parties for our operations.", True),
    ("Particulars As of March 31, 2025", False),
    ("Total 3,722", False),
    ("S ch e m e S i n c e", False),
])
def test_looks_like_heading(text, ok):
    assert looks_like_heading(text, True) is ok


def test_search_pages():
    pages = make_doc()
    hits = search_pages(pages, "our  business")          # spacing and case ignored
    assert [(h["page"], h["printed"]) for h in hits] == [(3, None), (9, 5), (24, 20)]
    assert hits[2]["text"] == "OUR BUSINESS" and hits[2]["heading"] and hits[2]["line"] == 2
    # "Headings only" keeps capital-letter lines (the TOC entry and the real heading) and drops
    # the cross-reference in running text on page 9
    heads = search_pages(pages, "Our Business", headings_only=True)
    assert [h["page"] for h in heads] == [3, 24]
    assert search_pages(pages, "x") == [] and search_pages(pages, "no such words") == []


SAMPLE = os.environ.get("IPO_SAMPLE_DRHP")


@pytest.mark.skipif(not SAMPLE or not Path(SAMPLE).exists(), reason="IPO_SAMPLE_DRHP not set")
def test_real_drhp():
    from core.section_extractor import extract_sections
    res = extract_sections(SAMPLE, doc_id="C1", doc_name="sample", with_subheadings=False)
    ob, rf = res["our_business"], res["risk_factors"]
    assert (ob.start_page, ob.end_page) == (160, 184)
    assert (rf.start_page, rf.end_page) == (35, 65)
    assert ob.next_section_title.startswith("KEY REGULATIONS")
