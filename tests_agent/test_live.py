"""Live tests against the OpenAI API (build step 6). Skipped unless IPO_LIVE_LLM=1 and settings are in .env.

Public documents only (IPO_SAMPLE_AR = SBI Funds Management AR 2024-25, IPO_SAMPLE_DRHP = ICICI Pru AMC DRHP).
They check format, citations and scores against the thresholds; never exact wording.
"""
import os
import re
from pathlib import Path

import pytest

LIVE = os.environ.get("IPO_LIVE_LLM") == "1"
AR, DRHP = os.environ.get("IPO_SAMPLE_AR"), os.environ.get("IPO_SAMPLE_DRHP")
pytestmark = pytest.mark.skipif(not LIVE, reason="set IPO_LIVE_LLM=1 (calls the OpenAI API, costs money)")

PAT_PAGE = ("Statement of Profit and Loss. Profit for the year attributable to owners: "
            "2,54,015.32 lakhs (FY2025) and 2,07,278.35 lakhs (FY2024).")


@pytest.fixture(scope="module")
def settings():
    from agent.config import load_settings
    return load_settings()


def test_judge_scores_supported_text_high_and_invented_text_low(settings):
    from agent.evals import RagasScorer
    seen = []
    sc = RagasScorer(settings)
    good = sc.faithfulness("Financial performance", "Our profit for the year was ₹2,54,015.32 lakhs in FY2025, "
                           "up from ₹2,07,278.35 lakhs in FY2024 [D1 p.145].", [PAT_PAGE],
                           on_usage=lambda *a: seen.append(a))
    bad = sc.faithfulness("Financial performance", "Our profit for the year was ₹9,99,999 lakhs in FY2025 and we "
                          "are the largest bank in Asia [D1 p.145].", [PAT_PAGE])
    assert good.score is not None and good.score >= settings.faithfulness_min, good
    assert bad.score is not None and bad.score < 0.5 and bad.unsupported, bad
    assert seen and all(tin and tout for _, tin, tout, _ in seen)               # judge tokens are counted
    rel = sc.relevance("Financial performance: profit for the year", [PAT_PAGE])
    assert rel >= settings.relevance_min


@pytest.mark.skipif(not (AR and DRHP and Path(AR).exists() and Path(DRHP).exists()),
                    reason="IPO_SAMPLE_AR and IPO_SAMPLE_DRHP not set")
def test_full_live_run_on_public_test_deal(settings, tmp_path, monkeypatch):
    from agent import live_run as L
    from agent import report as R
    from core import replies, store
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "deals")
    code = L.main(["run", "--public-documents", "--company", AR, "--comparable", DRHP, "--run-id", "live-1",
                   "--deal-name", "SBI Funds (public test)", "--approve-facts", "--exclude-unmapped",
                   "--proceed-low-relevance"])
    deal = next(p.name for p in store.DATA_DIR.iterdir())
    rep = R.load(deal, "live-1")
    assert code == 0 and rep["waiting_for"] == "checkpoint_review", rep.get("error")
    ob, rf = replies.load_draft(deal, "our_business"), replies.load_draft(deal, "risk_factors")
    assert ob.items and rf.items
    for it in ob.items:
        assert re.search(r"\[D1 p\.\d+", it.text), f"{it.id} has no company citation"
    for it in ob.items + rf.items:
        assert not re.search(r"\[C\d+ p\.", it.text), f"{it.id} cites a comparable"
        assert not it.unresolved, f"{it.id} cites pages that don't exist: {it.unresolved}"
    assert [i.id for i in rf.items] == [f"RF{n:02d}" for n in range(1, len(rf.items) + 1)]
    assert rep["totals"]["tokens_in"] and rep["tokenizer_check"]
    print(R.to_markdown(rep))
