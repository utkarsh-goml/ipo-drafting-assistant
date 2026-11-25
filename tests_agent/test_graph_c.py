"""Step 7: checkpoint 2 (review), the revise / edit loop, and export.

Done when: a Word file is exported after `approve`; `approve` is refused while a part is FAILED.
"""
import csv
import io

import docx
import pytest
from pydantic import ValidationError

from agent import graph as G
from core import blueprint as bpmod
from core import replies, store
from tests_agent.test_graph_b import OB_REPLY, RF_OTHER, RF_REPLY, StubScorer, env  # noqa: F401  (fixture)
from tests_agent.test_graph_a import settings

S = settings()
LOW = {"profit for the year": 0.5}          # the first OB draft scores 0.5 -> FAILED after 2 fix rounds
GOOD_OB = "### OB01 Overview\nWe are an asset management company [D1 p.5]."


def to_review(env, **kw):
    env.make(stop=None, **kw)
    snap = env.to_drafting()
    assert snap["waiting_for"] == "checkpoint_review", snap["error"] or snap["log"]
    return snap


def answer(env, reply):
    return G.resume(env.graph, "r1", reply, S, env.tel)


def items(snap, sec):
    return {i["item_id"]: i for i in snap["pending"]["sections"][sec]["items"]}


# --------------------------------------------------------------------------- the done-when
def test_approve_exports_word_markdown_and_checks(env):
    snap = to_review(env)
    p = snap["pending"]
    assert p["blockers"] == [] and p["reply_schema"]["title"] == "ReviewReply"
    ob = items(snap, "our_business")["OB01"]
    assert ob["status"] == "PASSED" and "₹2,54,015.32 lakhs [D1 p.145]" in ob["text"]
    assert items(snap, "risk_factors")["RF01"]["status"] == "PASSED"
    done = answer(env, {"approve": True})
    assert done["status"] == "done" and done["pending"] is None
    files = {k: store.deal_dir(env.deal) / v for k, v in done["exports"].items()}
    assert set(files) == {"our_business.docx", "our_business.md", "risk_factors.docx", "risk_factors.md", "checks.csv"}
    text = "\n".join(par.text for par in docx.Document(files["our_business.docx"]).paragraphs)
    assert "OUR BUSINESS" in text and "₹2,54,015.32 lakhs [D1 p.145]" in text
    assert "agent run r1, drafting model m" in text and "Stylus" not in text
    rows = list(csv.DictReader(open(files["checks.csv"], encoding="utf-8")))
    assert [(r["item_id"], r["status"]) for r in rows] == [("OB01", "PASSED"), ("RF01", "PASSED")]


def test_approve_refused_while_a_part_is_failed_then_revise_fixes_it(env):
    snap = to_review(env, scorer=StubScorer(faith=LOW), fix=lambda i: OB_REPLY)
    assert items(snap, "our_business")["OB01"]["status"] == "FAILED"
    assert snap["pending"]["blockers"] == ["OB01 FAILED the checks (0 high issue(s), faithfulness 0.5)"]
    snap = answer(env, {"approve": True})
    assert snap["waiting_for"] == "checkpoint_review" and snap["status"] == "waiting_banker"
    assert snap["pending"]["notice"].startswith("Approval refused: OB01 FAILED")
    assert not (store.deal_dir(env.deal) / "exports").exists()
    # the banker asks the model to revise OB01
    env.fix.answer = lambda i: GOOD_OB
    snap = answer(env, {"revise": [{"section": "our_business", "item_id": "OB01",
                                     "instructions": "Remove the profit sentence; keep the overview."}]})
    ob = items(snap, "our_business")["OB01"]
    assert ob["status"] == "PASSED" and ob["source"] == "revised by the model" and ob["versions"] == 1
    assert ob["text"] == "We are an asset management company [D1 p.5]."
    assert snap["pending"]["blockers"] == [] and env.fix.seen[-1] == "REV-OB01"
    assert any("OB01: revised by the model; PASSED" in line for line in snap["log"])
    done = answer(env, {"approve": True})
    assert done["status"] == "done" and done["export_blockers"] == []
    assert (store.deal_dir(env.deal) / "exports" / "r1" / "our_business.docx").exists()


# --------------------------------------------------------------------------- edits, versions, re-checks
def test_banker_edit_is_rechecked_and_restore_brings_the_old_text_back(env):
    to_review(env)
    snap = answer(env, {"edit": [{"section": "our_business", "item_id": "OB01", "text": "We are a large AMC."}]})
    ob = items(snap, "our_business")["OB01"]
    assert ob["status"] == "FAILED" and ob["source"] == "banker edit"
    assert any("No company page is cited" in i["message"] for i in ob["issues"])
    assert answer(env, {"approve": True})["pending"]["notice"].startswith("Approval refused: OB01 FAILED")
    snap = answer(env, {"restore": [{"section": "our_business", "item_id": "OB01", "index": 0}]})
    ob = items(snap, "our_business")["OB01"]
    assert ob["status"] == "PASSED" and "₹2,54,015.32" in ob["text"] and ob["versions"] == 2
    assert answer(env, {"approve": True})["status"] == "done"


def test_comparable_citation_in_an_edit_fails(env):
    to_review(env)
    snap = answer(env, {"edit": [{"section": "our_business", "item_id": "OB01",
                                   "text": "We are an asset management company [C1 p.2]."}]})
    ob = items(snap, "our_business")["OB01"]
    assert ob["status"] == "FAILED" and any("Comparable page cited" in i["message"] for i in ob["issues"])


def test_invalid_and_unknown_changes(env):
    to_review(env)
    with pytest.raises(ValidationError, match="approve without changes"):
        answer(env, {"approve": True, "recheck": [{"section": "our_business", "item_id": "OB01"}]})
    with pytest.raises(ValidationError, match="send changes, or approve"):
        answer(env, {})
    assert G.pending(env.graph, "r1", S)["kind"] == "checkpoint_review"           # nothing saved
    snap = answer(env, {"recheck": [{"section": "our_business", "item_id": "OB07"}],
                        "not_required": [{"key": "nope", "reason": "not a real trigger key"}]})
    assert snap["pending"]["notice"] == ("Not applied: OB07: no such item in our_business; "
                                         "'nope' is not a required risk")


def test_revise_reply_without_the_item_leaves_text_unchanged(env):
    to_review(env)
    env.fix.answer = lambda i: "### OB09 Something else\nText [D1 p.5]."
    before = replies.load_draft(env.deal, "our_business").items[0].text
    snap = answer(env, {"revise": [{"section": "our_business", "item_id": "OB01", "instructions": "Shorter please."}]})
    assert "reply had no '### OB01 ...' item; text unchanged" in snap["pending"]["notice"]
    assert replies.load_draft(env.deal, "our_business").items[0].text == before


# --------------------------------------------------------------------------- missing items
def test_missing_required_risk_blocks_until_added(env):
    snap = to_review(env, draft=lambda i: {"OB01": OB_REPLY, "RF-Internal": RF_OTHER,
                                           "RF-Internal-GAP": RF_OTHER.replace("RF01", "RF02")}[i])
    assert snap["pending"]["missing_required"] == [{"key": "legal", "title": "Outstanding legal proceedings"}]
    assert answer(env, {"approve": True})["pending"]["notice"] == \
        "Approval refused: Required risk 'legal' is not covered"
    snap = answer(env, {"add_risk": [{"group": "Internal", "title": "We are involved in outstanding legal proceedings",
                                      "text": "There are outstanding legal proceedings against us [D1 p.6]."}]})
    assert snap["missing_required"] == [] and snap["pending"]["blockers"] == []
    rf = items(snap, "risk_factors")
    assert list(rf) == ["RF01", "RF02", "RF03"] and rf["RF03"]["status"] == "PASSED"
    assert rf["RF03"]["title"] == "We are involved in outstanding legal proceedings"
    assert answer(env, {"approve": True})["status"] == "done"


def test_required_risk_marked_not_applicable_with_a_reason(env):
    to_review(env, draft=lambda i: {"OB01": OB_REPLY, "RF-Internal": RF_OTHER,
                                    "RF-Internal-GAP": RF_OTHER.replace("RF01", "RF02")}[i])
    snap = answer(env, {"not_required": [{"key": "legal", "reason": "Only one consumer case, below materiality."}]})
    assert snap["missing_required"] == [] and snap["not_required"] == {
        "legal": "Only one consumer case, below materiality."}
    done = answer(env, {"approve": True})
    rows = list(csv.reader(open(store.deal_dir(env.deal) / done["exports"]["checks.csv"], encoding="utf-8")))
    assert any("required risk 'legal' marked not applicable: Only one consumer case" in r[2] for r in rows)


def test_missing_subsection_written_by_the_banker_or_left_out(env):
    bad = "### OB09 Wrong heading\nWe are an AMC [D1 p.5]."
    snap = to_review(env, draft=lambda i: bad if i == "OB01" else RF_REPLY, fix=lambda i: bad)
    assert snap["pending"]["missing_ob"] == [{"item_id": "OB01", "title": "Overview"}]
    assert "Our Business subsection OB01 is missing" in snap["pending"]["blockers"]
    with pytest.raises(ValidationError):
        answer(env, {"leave_out": ["OB01"], "approve": True})
    snap = answer(env, {"edit": [{"section": "our_business", "item_id": "OB01",
                                   "text": "We are an asset management company [D1 p.5]."}]})
    assert snap["missing_ob"] == [] and items(snap, "our_business")["OB01"]["status"] == "PASSED"
    assert [i.id for i in replies.load_draft(env.deal, "our_business").items] == ["OB01"]
    assert answer(env, {"approve": True})["status"] == "done"


def test_leave_out_missing_subsection(env):
    bad = "### OB09 Wrong heading\nWe are an AMC [D1 p.5]."
    to_review(env, draft=lambda i: bad if i == "OB01" else RF_REPLY, fix=lambda i: bad)
    snap = answer(env, {"leave_out": ["OB01"]})
    assert snap["missing_ob"] == [] and snap["pending"]["blockers"] == []
    assert not bpmod.load(env.deal).our_business.subsections[0].include
    assert answer(env, {"approve": True})["status"] == "done"


def test_no_draft_text_in_traces(env):
    from tests_agent.test_graph_a import _values
    to_review(env)
    answer(env, {"edit": [{"section": "our_business", "item_id": "OB01",
                           "text": "We are an asset management company [D1 p.5]."}]})
    spans = env.exp.get_finished_spans()
    assert any(s.name == "node.checkpoint_review" for s in spans)
    assert any(s.name == "node.apply_review" for s in spans)
    dump = _values(spans)
    assert "asset management company" not in dump and "2,54,015.32" not in dump
