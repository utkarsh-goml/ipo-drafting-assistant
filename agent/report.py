"""Run report and model comparison (build step 6).

`run_report` joins the run's final state with its call ledger (`calls.jsonl`): per part, its status,
fix rounds, faithfulness per round, relevance, tokens, cost and model time; per call type, totals.
Cost is a sum only when every call has a price (OPENAI_PRICES); otherwise it shows "price not set".
`compare` puts several runs side by side (e.g. Luna vs Sol on the same deal).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agent import usage
from agent.config import Settings
from core import store


def _sum(rows: List[dict], key: str) -> Optional[int]:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) if vals else None


def _cost(rows: List[dict]) -> Optional[float]:
    if not rows or any(r.get("cost_usd") is None for r in rows):
        return None
    return round(sum(r["cost_usd"] for r in rows), 4)


def _totals(rows: List[dict]) -> dict:
    return {"calls": len(rows), "tokens_in": _sum(rows, "tokens_in"), "tokens_out": _sum(rows, "tokens_out"),
            "cost_usd": _cost(rows), "model_seconds": round(sum(r["latency_s"] for r in rows), 1)}


def report_dir(deal_id: str, run_id: str) -> Path:
    return store.deal_dir(deal_id) / "runs" / run_id


def run_report(deal_id: str, run_id: str, snap: dict, settings: Settings, wall_s: Optional[float]) -> dict:
    calls = usage.load(deal_id, run_id)
    by_part: Dict[str, List[dict]] = defaultdict(list)
    by_call: Dict[str, List[dict]] = defaultdict(list)
    for c in calls:
        by_part[c["part"]].append(c)
        by_call[c["call"]].append(c)
    parts = []
    for p in snap.get("parts", []):
        rows = by_part.get(p["item_id"], [])
        parts.append({"item_id": p["item_id"], "title": p["title"], "status": p["status"], "rounds": p["rounds"],
                      "faithfulness": p["faithfulness"], "faithfulness_by_round": p.get("faithfulness_by_round", []),
                      "relevance": p.get("relevance"), "high": p["high"], "low": p["low"],
                      "unsupported": len(p.get("unsupported") or []), "gaps": len(p.get("gaps") or []),
                      **_totals(rows)})
    est = [c for c in calls if c.get("tokens_estimate") and c.get("tokens_in")]
    tok = None
    if est:
        e, r = sum(c["tokens_estimate"] for c in est), sum(c["tokens_in"] for c in est)
        tok = {"calls": len(est), "estimate": e, "reported": r, "reported_over_estimate": round(r / e, 3)}
    unpriced = sorted({c["model"] for c in calls if c.get("cost_usd") is None})
    return {
        "deal_id": deal_id, "run_id": run_id, "status": snap.get("status"), "error": snap.get("error"),
        "waiting_for": snap.get("waiting_for"),
        "models": {"blueprint": settings.openai_model_blueprint, "draft_and_fix": settings.openai_model_draft,
                   "judge": settings.openai_model_eval},
        "thresholds": {"faithfulness_min": settings.faithfulness_min, "relevance_min": settings.relevance_min,
                       "max_fix_rounds": settings.max_fix_rounds, "max_gap_rounds": settings.max_gap_rounds},
        "wall_seconds": None if wall_s is None else round(wall_s, 1),
        "parts": parts,
        "by_call": {k: _totals(v) for k, v in sorted(by_call.items())},
        "totals": {**_totals(calls), "unpriced_models": unpriced,
                   "passed": sum(1 for p in parts if p["status"] == "PASSED"),
                   "failed": sum(1 for p in parts if p["status"] == "FAILED"),
                   "fix_calls": len(by_call.get("fix", [])),
                   "export_blockers": snap.get("export_blockers", []),
                   "missing_ob": snap.get("missing_ob", []), "missing_required": snap.get("missing_required", [])},
        "tokenizer_check": tok,
    }


def save(report: dict) -> Path:
    d = report_dir(report["deal_id"], report["run_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (d / "report.md").write_text(to_markdown(report), encoding="utf-8")
    return d / "report.md"


def load(deal_id: str, run_id: str) -> dict:
    f = report_dir(deal_id, run_id) / "report.json"
    if not f.exists():
        raise FileNotFoundError(f"no report for run {run_id!r} of deal {deal_id!r}")
    return json.loads(f.read_text(encoding="utf-8"))


def _n(v, fmt="{:,}") -> str:
    return "-" if v is None else fmt.format(v)


def _usd(v) -> str:
    return "price not set" if v is None else f"${v:,.4f}"


def status_label(r: dict) -> str:
    if r.get("waiting_for") == "checkpoint_review":
        return "draft ready, waiting at checkpoint 2"
    if r.get("waiting_for"):
        return f"waiting for {r['waiting_for']}"
    return r["status"]


def to_markdown(r: dict) -> str:
    t = r["totals"]
    out = [f"# Run {r['run_id']} ({status_label(r)})", "",
           f"Models: blueprint `{r['models']['blueprint']}`, drafting and fixing `{r['models']['draft_and_fix']}`, "
           f"judge `{r['models']['judge']}`.",
           f"Thresholds: faithfulness >= {r['thresholds']['faithfulness_min']}, relevance >= "
           f"{r['thresholds']['relevance_min']}, up to {r['thresholds']['max_fix_rounds']} fix rounds.", ""]
    if r.get("error"):
        out += [f"**Error:** {r['error']}", ""]
    out += ["| Part | Status | Fix rounds | Faithfulness by round | Relevance | High / low issues | Tokens in / out | Cost | Model time (s) |",
            "|---|---|---|---|---|---|---|---|---|"]
    for p in r["parts"]:
        fh = " → ".join(_n(x, "{:.2f}") for x in p["faithfulness_by_round"]) or _n(p["faithfulness"], "{:.2f}")
        name = p['title'] if p['title'].startswith(p['item_id']) else f"{p['item_id']} {p['title']}"
        out.append(f"| {name[:45]} | {p['status']} | {p['rounds']} | {fh} | "
                   f"{_n(p['relevance'], '{:.2f}')} | {p['high']} / {p['low']} | "
                   f"{_n(p['tokens_in'])} / {_n(p['tokens_out'])} | {_usd(p['cost_usd'])} | {p['model_seconds']} |")
    out += ["", "| Call type | Calls | Tokens in | Tokens out | Cost | Model time (s) |", "|---|---|---|---|---|---|"]
    for k, v in r["by_call"].items():
        out.append(f"| {k} | {v['calls']} | {_n(v['tokens_in'])} | {_n(v['tokens_out'])} | {_usd(v['cost_usd'])} | "
                   f"{v['model_seconds']} |")
    out += ["", f"**Total:** {t['calls']} calls, {_n(t['tokens_in'])} tokens in, {_n(t['tokens_out'])} out, "
                f"cost {_usd(t['cost_usd'])}, run time {_n(r['wall_seconds'], '{:,.0f}')} s. "
                f"Parts PASSED {t['passed']}, FAILED {t['failed']}; {t['fix_calls']} fix calls."]
    if t["unpriced_models"]:
        out.append(f"No price in OPENAI_PRICES for: {', '.join(t['unpriced_models'])}.")
    tc = r.get("tokenizer_check")
    if tc:
        out.append(f"Token count check: OpenAI reported {tc['reported']:,} input tokens vs {tc['estimate']:,} counted "
                   f"before sending ({tc['reported_over_estimate']}x, {tc['calls']} calls).")
    out.append("Export blockers: " + ("; ".join(t["export_blockers"]) if t["export_blockers"] else "none"))
    return "\n".join(out) + "\n"


ROWS = [
    ("Drafting model", lambda r: f"`{r['models']['draft_and_fix']}`"),
    ("Status", status_label),
    ("Parts PASSED / FAILED", lambda r: f"{r['totals']['passed']} / {r['totals']['failed']}"),
    ("Fix calls", lambda r: str(r["totals"]["fix_calls"])),
    ("Faithfulness, first draft (median)", lambda r: _median([p["faithfulness_by_round"][0] for p in r["parts"]
                                                             if p["faithfulness_by_round"]
                                                             and p["faithfulness_by_round"][0] is not None])),
    ("Faithfulness, final (median)", lambda r: _median([p["faithfulness"] for p in r["parts"]
                                                       if p["faithfulness"] is not None])),
    ("High issues left", lambda r: str(sum(p["high"] for p in r["parts"]))),
    ("Missing subsections / required risks", lambda r: f"{len(r['totals']['missing_ob'])} / "
                                                       f"{len(r['totals']['missing_required'])}"),
    ("Tokens in / out", lambda r: f"{_n(r['totals']['tokens_in'])} / {_n(r['totals']['tokens_out'])}"),
    ("Cost", lambda r: _usd(r["totals"]["cost_usd"])),
    ("Run time (s)", lambda r: _n(r["wall_seconds"], "{:,.0f}")),
]


def _median(xs: Sequence[float]) -> str:
    if not xs:
        return "-"
    s = sorted(xs)
    m = len(s) // 2
    v = s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2
    return f"{v:.2f}"


def compare(reports: Sequence[dict]) -> str:
    head = "| | " + " | ".join(r["run_id"] for r in reports) + " |"
    out = [head, "|---" * (len(reports) + 1) + "|"]
    for label, fn in ROWS:
        out.append(f"| {label} | " + " | ".join(fn(r) for r in reports) + " |")
    return "\n".join(out) + "\n"
