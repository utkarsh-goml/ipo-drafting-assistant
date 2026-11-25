"""Graph state. Large things (page text, units, drafts) stay on disk in the deal folder;
the state holds IDs, flags and decisions so checkpoints stay small (LANGGRAPH_PLAN.md §5.2)."""
from __future__ import annotations

import operator
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator
from typing_extensions import TypedDict

RunStatus = Literal["running", "error", "facts_approved", "drafted", "done"]
SectionKey = Literal["our_business", "risk_factors"]
RFGroup = Literal["Internal", "External", "Offer"]


def merge_by_key(*keys: str):
    """Reducer: list of dicts merged by the given keys (later wins), order of first appearance kept."""
    def merge(old: Optional[List[dict]], new: Optional[List[dict]]) -> List[dict]:
        out: Dict[tuple, dict] = {}
        for d in (old or []) + (new or []):
            if d.get("_remove"):
                out.pop(tuple(d[k] for k in keys), None)
                continue
            out[tuple(d[k] for k in keys)] = d
        return list(out.values())
    merge.__name__ = f"merge_by_{'_'.join(keys)}"
    return merge


merge_extraction = merge_by_key("doc_id", "section")
merge_parts = merge_by_key("item_id")


def first_error(old: Optional[str], new: Optional[str]) -> Optional[str]:
    """The first error is kept (parallel tasks may fail together); it is never cleared."""
    return old or new


def status_merge(old: Optional[str], new: Optional[str]) -> Optional[str]:
    return "error" if "error" in (old, new) else new


class NewDocument(TypedDict, total=False):
    path: str
    kind: Literal["company", "comparable"]
    doc_type: Optional[str]


class DealState(TypedDict, total=False):
    deal_id: str
    run_id: str
    add_documents: List[NewDocument]                       # input: files to add at ingest
    docs: List[dict]                                       # document summaries (D1.., C1..)
    extraction: Annotated[List[dict], merge_extraction]    # per (doc_id, section): tools.section_summary + decisions
    context_built: bool
    facts_approved: bool
    status: Annotated[RunStatus, status_merge]
    error: Annotated[Optional[str], first_error]           # set once; the run stops
    notice: Optional[str]                                  # message for the banker at the next pause
    # part B
    blueprint_ready: bool
    work_items: List[dict]                                 # current plan: one dict per OB subsection / RF group
    pages_problems: List[dict]                             # items the banker must give pages for (none / low relevance)
    accepted_low: Annotated[List[str], operator.add]       # items the banker chose to draft despite low relevance
    gap_plan: Optional[dict]                               # {"ob": [ids], "rf": {group: [trigger keys]}} for the gap round
    gap_rounds: int
    parts: Annotated[List[dict], merge_parts]              # per work item: status PASSED / FAILED, scores, issues
    item_status: Dict[str, Dict[str, dict]]                # section -> final item id -> {status, part, ...}
    missing_ob: List[str]
    missing_required: List[str]
    # checkpoint 2
    review_request: Optional[dict]                         # the banker's last reply (ReviewReply), applied next
    review_approved: bool
    not_required: Dict[str, str]                           # required risk key -> banker's reason it doesn't apply
    exports: Dict[str, str]                                # name -> file path, relative to the deal folder
    log: Annotated[List[str], operator.add]                # audit trail shown to the banker


def section_ok(flag: dict) -> bool:
    """Usable without the banker: TOC + both ends verified, or a banker decision."""
    return bool(flag.get("accepted") or flag.get("banker_decision"))


# --------------------------------------------------------------------------- banker replies (interrupt schemas)
class SectionDecision(BaseModel):
    doc_id: str
    section: Literal["our_business", "risk_factors"]
    action: Literal["confirm", "set_range", "drop"] = Field(
        description="confirm = use the pages found; set_range = use start_page..end_page; "
                    "drop = don't use this comparable section")
    start_page: Optional[int] = None
    end_page: Optional[int] = None

    @model_validator(mode="after")
    def _pages(self) -> "SectionDecision":
        if self.action == "set_range" and (self.start_page is None or self.end_page is None):
            raise ValueError("set_range needs start_page and end_page")
        if self.action != "set_range" and (self.start_page is not None or self.end_page is not None):
            raise ValueError("pages are only given with set_range")
        return self


class ReviewExtractionReply(BaseModel):
    decisions: List[SectionDecision] = Field(min_length=1)


class FigureEdit(BaseModel):
    key: str
    label: str
    current: Optional[float]
    previous: Optional[float]


class PageChoice(BaseModel):
    kind: Literal["our_business", "risk_themes"]
    key: str
    cite: str
    selected: bool


class FactsReply(BaseModel):
    """Checkpoint 1. Either approve (with no edits) or send edits (then the facts are shown again)."""
    approved: bool
    figure_edits: List[FigureEdit] = Field(default_factory=list)
    trigger_selection: Dict[str, bool] = Field(default_factory=dict, description="trigger key -> selected")
    page_selection: List[PageChoice] = Field(default_factory=list)

    @property
    def has_edits(self) -> bool:
        return bool(self.figure_edits or self.trigger_selection or self.page_selection)

    @model_validator(mode="after")
    def _approve_what_was_seen(self) -> "FactsReply":
        if self.approved and self.has_edits:
            raise ValueError("approve without edits: send the edits first, review the updated facts, then approve")
        if not self.approved and not self.has_edits:
            raise ValueError("not approved and no edits: send edits, or approve")
        return self


class ChoosePagesReply(BaseModel):
    """Answer to `choose_pages`: change pages, and/or draft low-relevance items as they are, and/or leave out
    Our Business subsections. At least one of the three."""
    page_selection: List[PageChoice] = Field(default_factory=list)
    proceed_anyway: List[str] = Field(default_factory=list, description="item ids to draft despite low relevance")
    exclude_subsections: List[str] = Field(default_factory=list, description="Our Business ids to leave out")

    @model_validator(mode="after")
    def _something(self) -> "ChoosePagesReply":
        if not (self.page_selection or self.proceed_anyway or self.exclude_subsections):
            raise ValueError("choose pages, proceed_anyway, or exclude_subsections")
        return self


class ReviseItem(BaseModel):
    section: SectionKey
    item_id: str
    instructions: str = Field(min_length=5, description="what to change; the model rewrites the item")


class EditItem(BaseModel):
    section: SectionKey
    item_id: str = Field(description="existing item, or a missing Our Business subsection id to write it")
    text: str = Field(min_length=1)
    title: Optional[str] = None


class RestoreItem(BaseModel):
    section: SectionKey
    item_id: str
    index: int = Field(ge=0, description="version number, 0 = oldest")


class RecheckItem(BaseModel):
    section: SectionKey
    item_id: str


class AddRisk(BaseModel):
    group: RFGroup
    title: str = Field(min_length=5)
    text: str = Field(min_length=1)


class NotRequired(BaseModel):
    key: str
    reason: str = Field(min_length=10, description="why this required risk does not apply (kept in the log)")


class ReviewReply(BaseModel):
    """Checkpoint 2. Approve (alone), or send changes; changed items are checked and scored again."""
    approve: bool = False
    revise: List[ReviseItem] = Field(default_factory=list)
    edit: List[EditItem] = Field(default_factory=list)
    restore: List[RestoreItem] = Field(default_factory=list)
    recheck: List[RecheckItem] = Field(default_factory=list, description="check and score again, unchanged")
    add_risk: List[AddRisk] = Field(default_factory=list)
    leave_out: List[str] = Field(default_factory=list, description="missing Our Business subsections to leave out")
    not_required: List[NotRequired] = Field(default_factory=list)

    @property
    def changes(self) -> bool:
        return bool(self.revise or self.edit or self.restore or self.recheck or self.add_risk or self.leave_out
                    or self.not_required)

    @model_validator(mode="after")
    def _one_kind(self) -> "ReviewReply":
        if self.approve and self.changes:
            raise ValueError("approve without changes: send changes first, look at the result, then approve")
        if not self.approve and not self.changes:
            raise ValueError("send changes, or approve")
        return self


class PartState(TypedDict, total=False):
    """State of one work item inside the draft/check/evaluate/fix subgraph."""
    deal_id: str
    run_id: str
    item: dict
    text: str
    gaps: List[str]
    structure: List[dict]
    pack_cites: List[str]
    issues: List[dict]
    high: int
    faithfulness: Optional[float]
    faithfulness_by_round: List[Optional[float]]
    unsupported: List[str]
    rounds: int
    # returned to the main graph
    parts: Annotated[List[dict], merge_parts]
    status: Annotated[RunStatus, status_merge]
    error: Annotated[Optional[str], first_error]
    log: Annotated[List[str], operator.add]


class PartOutput(TypedDict, total=False):
    parts: Annotated[List[dict], merge_parts]
    status: Annotated[RunStatus, status_merge]
    error: Annotated[Optional[str], first_error]
    log: Annotated[List[str], operator.add]
