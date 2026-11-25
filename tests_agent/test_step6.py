"""Step 6 plumbing, without the API: prices -> cost, call ledger, judge token hook, run report,
comparison table, and the live-run script end to end with the mock model."""
import asyncio
import json

import httpx
import pytest

from agent import live_run as L
from agent import report as R
from agent import usage
from agent.evals import usage_from_response, usage_hooks
from agent.config import ConfigError, load_settings
from tests_agent.test_config import GOOD
from tests_agent.test_graph_a import settings
from tests_agent.test_graph_b import StubScorer, env  # noqa: F401  (fixture)

PRICED = settings(openai_prices={"mock-model": (0.10, 0.50)})


# --------------------------------------------------------------------------- prices
def test_cost_only_from_configured_prices():
    assert PRICED.cost_usd("mock-model", 1_000_000, 100_000) == pytest.approx(0.15)
    assert PRICED.cost_usd("other-model", 1000, 10) is None              # not priced: no cost, never a guess
    assert PRICED.cost_usd("mock-model", None, 10) is None               # tokens unknown: no cost
    assert settings().openai_prices == {}


def test_prices_read_from_env_and_checked(monkeypatch):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OPENAI_PRICES", '{"gpt-6-luna": [0.1, 0.5]}')
    assert load_settings(_env_file=None).openai_prices == {"gpt-6-luna": (0.1, 0.5)}
    monkeypatch.setenv("OPENAI_PRICES", '{"gpt-6-luna": [-1, 0.5]}')
    with pytest.raises(ConfigError, match="negative price"):
        load_settings(_env_file=None)


# --------------------------------------------------------------------------- judge token hook
def test_judge_usage_is_read_from_openai_responses():
    got = []
    hooks = usage_hooks(lambda *a: got.append(a), "default-judge")
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"model": "gpt-6-luna-2026", "usage": {"prompt_tokens": 1200, "completion_tokens": 80}}

    async def go():
        await hooks["request"][0](req)
        await hooks["response"][0](httpx.Response(200, json=body, request=req))
        await hooks["response"][0](httpx.Response(500, json={}, request=req))          # failed call: not counted
        other = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
        await hooks["response"][0](httpx.Response(200, json=body, request=other))      # not a chat call
    asyncio.run(go())
    assert [(m, i, o) for m, i, o, _ in got] == [("gpt-6-luna-2026", 1200, 80)]
    assert usage_from_response("not json") == (None, None, None)


# --------------------------------------------------------------------------- ledger + report
def _snap(env, scorer=None):
    env.make(scorer=scorer or StubScorer(faith={"profit for the year": 0.5}), settings_=PRICED)
    return env.to_drafting()


def test_every_model_call_is_in_the_ledger_with_cost(env):
    snap = _snap(env)
    rows = usage.load(env.deal, "r1")
    calls = sorted((r["part"], r["call"]) for r in rows if not r["call"].startswith("eval"))
    assert calls == [("BLUEPRINT", "blueprint"), ("OB01", "draft"), ("OB01", "fix"), ("OB01", "fix"),
                     ("RF-Internal", "draft")]
    fix = [r for r in rows if r["call"] == "fix"]
    assert {r["item_id"] for r in fix} == {"REV-OB01"}
    # mock reports 1000 in / 200 out per call: 1000*0.10/1M + 200*0.50/1M
    assert all(r["cost_usd"] == pytest.approx(0.0002) for r in rows if r["call"] != "blueprint")
    assert all("text" not in r and "We are" not in json.dumps(r) for r in rows)      # counts only, no content
    assert snap["status"] == "drafted"


def test_run_report_and_markdown(env):
    snap = _snap(env)
    rep = R.run_report(env.deal, "r1", snap, PRICED, wall_s=12.3)
    ob = next(p for p in rep["parts"] if p["item_id"] == "OB01")
    assert ob["status"] == "FAILED" and ob["faithfulness_by_round"] == [0.5, 0.5, 0.5] and ob["calls"] == 3
    assert rep["totals"]["failed"] == 1 and rep["totals"]["fix_calls"] == 2
    assert rep["totals"]["cost_usd"] == pytest.approx(0.0002 * 4 + rep["by_call"]["blueprint"]["cost_usd"])
    assert rep["tokenizer_check"]["reported"] == 5000
    md = R.to_markdown(rep)
    assert "| OB01 Overview | FAILED | 2 | 0.50 → 0.50 → 0.50 |" in md
    assert "Export blockers: OB01 FAILED" in md
    path = R.save(rep)
    assert R.load(env.deal, "r1")["run_id"] == "r1" and path.exists()


def test_unpriced_model_shows_price_not_set(env):
    env.make()
    snap = env.to_drafting()
    rep = R.run_report(env.deal, "r1", snap, settings(), wall_s=None)
    assert rep["totals"]["cost_usd"] is None and rep["totals"]["unpriced_models"] == ["mock-model"]
    assert "price not set" in R.to_markdown(rep)


def test_compare_table():
    def rep(run, model, faith, failed, cost):
        return {"run_id": run, "status": "drafted", "models": {"draft_and_fix": model}, "wall_seconds": 100,
                "parts": [{"faithfulness": faith, "faithfulness_by_round": [faith - 0.1, faith], "high": 0}],
                "totals": {"passed": 1 - failed, "failed": failed, "fix_calls": 1, "missing_ob": [],
                           "missing_required": [], "tokens_in": 1000, "tokens_out": 10, "cost_usd": cost}}
    t = R.compare([rep("luna-1", "luna", 0.8, 1, 0.01), rep("sol-1", "sol", 0.95, 0, None)])
    assert "| | luna-1 | sol-1 |" in t
    assert "| Parts PASSED / FAILED | 0 / 1 | 1 / 0 |" in t
    assert "| Faithfulness, first draft (median) | 0.70 | 0.85 |" in t
    assert "| Cost | $0.0100 | price not set |" in t


# --------------------------------------------------------------------------- the script, with the mock model
def test_live_run_script_end_to_end_with_mock(env, monkeypatch, tmp_path, capsys):
    env.make(settings_=PRICED)
    monkeypatch.setattr(L, "load_settings", lambda **kw: PRICED)
    monkeypatch.setattr(L, "setup_telemetry", lambda s: env.tel)
    monkeypatch.setattr(L.AgentDeps, "from_settings", classmethod(lambda cls, s, t: env.deps))
    monkeypatch.setattr(env.tel, "shutdown", lambda: None)
    from tests_agent.test_graph_a import _pdf
    a, c = _pdf(tmp_path / "a.pdf"), _pdf(tmp_path / "c.pdf")
    assert L.main(["run", "--company", a, "--comparable", c, "--run-id", "x"]) == 2          # not confirmed public
    assert L.main(["run", "--public-documents", "--company", a, "--comparable", c, "--run-id", "x"]) == 3
    out = capsys.readouterr().out
    assert "Stopped: checkpoint 1 is waiting" in out
    deal = out.split("Created deal ")[1].split()[0]
    assert deal != env.deal
    # continue on the same deal with a new run, approving the facts
    code = L.main(["run", "--public-documents", "--deal", deal, "--run-id", "y", "--approve-facts"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PAT 2,54,015.32 [D1 p.145]" in out and "# Run y (draft ready, waiting at checkpoint 2)" in out
    assert L.main(["compare", "--deal", deal, "y", "--out", str(tmp_path / "cmp.md")]) == 0
    assert "| Parts PASSED / FAILED | 2 / 0 |" in (tmp_path / "cmp.md").read_text(encoding="utf-8")
