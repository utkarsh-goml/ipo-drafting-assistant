"""Typed data models shared across the app."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

DocKind = Literal["comparable", "company"]
CompanyDocType = Literal["ANNUAL_REPORT", "FINANCIALS", "INVESTOR_PPT", "MGMT_QA", "OTHER"]


class DocumentMeta(BaseModel):
    doc_id: str                      # C1, C2 ... for comparables; D1, D2 ... for company docs
    kind: DocKind
    name: str                        # original file name
    path: str                        # stored file path
    doc_type: Optional[str] = None   # CompanyDocType for company docs
    page_count: int = 0


class TocEntry(BaseModel):
    title: str
    printed_page: int
    is_part: bool = False            # "SECTION II: ..." style part heading


class PageText(BaseModel):
    page_no: int                     # 1-based PDF page number
    printed_page: Optional[int] = None
    lines: List[str]


class SubHeading(BaseModel):
    page_no: int
    text: str


class ExtractedSection(BaseModel):
    doc_id: str
    doc_name: str
    section_key: str                 # our_business | risk_factors
    section_title: str
    start_page: int                  # 1-based PDF page
    start_line: int                  # 1-based line on start_page
    end_page: int
    end_line: int                    # 1-based, inclusive
    next_section_title: Optional[str] = None
    method: str                      # bookmarks | toc | heading_scan | manual
    start_verified: bool = False     # start line is a bold capital heading matching the TOC title
    end_verified: bool = False       # next section's bold capital heading found
    confidence: float
    notes: List[str] = Field(default_factory=list)
    pages: List[PageText] = Field(default_factory=list)
    subheadings: List[SubHeading] = Field(default_factory=list)

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    def text_with_markers(self) -> str:
        """Section text with [Cx p.N] markers before each page, used for packs and display."""
        out = []
        for p in self.pages:
            out.append(f"[{self.doc_id} p.{p.page_no}]")
            out.extend(p.lines)
        return "\n".join(out)

    def word_count(self) -> int:
        return sum(len(" ".join(p.lines).split()) for p in self.pages)
