"""Per-run ledger of model calls: `data/deals/<deal>/runs/<run>/calls.jsonl`, one line per call.

Holds counts only (item, call type, model, tokens, seconds, cost); never prompt or reply text.
It feeds the run report and the model comparison (build step 6). The same numbers also go to telemetry.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from agent.config import Settings
from agent.telemetry import Telemetry
from core import store

CALLS_FILE = "calls.jsonl"
_lock = threading.Lock()

# (model, tokens_in, tokens_out, latency_s) -> None
UsageCallback = Callable[[str, Optional[int], Optional[int], float], None]


def calls_path(deal_id: str, run_id: str) -> Path:
    return store.deal_dir(deal_id) / "runs" / (run_id or "run") / CALLS_FILE


def record(deal_id: str, run_id: str, *, item_id: str, call: str, model: str, tokens_in: Optional[int],
           tokens_out: Optional[int], latency_s: float, cost_usd: Optional[float],
           tokens_estimate: Optional[int] = None, part: Optional[str] = None) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "part": part or item_id, "item_id": item_id, "call": call,
           "model": model,
           "tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_estimate": tokens_estimate,
           "latency_s": round(latency_s, 3), "cost_usd": cost_usd}
    p = calls_path(deal_id, run_id)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def load(deal_id: str, run_id: str) -> List[dict]:
    p = calls_path(deal_id, run_id)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def judge_callback(deal_id: str, run_id: str, item_id: str, call: str, settings: Settings,
                   tel: Telemetry) -> UsageCallback:
    """For the quality judge: each OpenAI request Ragas makes is recorded here and in telemetry."""
    def on_usage(model: str, tokens_in: Optional[int], tokens_out: Optional[int], latency_s: float) -> None:
        cost = settings.cost_usd(model, tokens_in, tokens_out)
        tel.record_llm_call(call=call, model=model, tokens_in=tokens_in or 0, tokens_out=tokens_out or 0,
                            latency_s=latency_s, cost_usd=cost)
        record(deal_id, run_id, item_id=item_id, call=call, model=model, tokens_in=tokens_in,
               tokens_out=tokens_out, latency_s=latency_s, cost_usd=cost)
    return on_usage
