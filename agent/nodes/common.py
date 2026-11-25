"""Shared node plumbing: a span per node, and errors turned into a stopped run (status=error)."""
from __future__ import annotations

import functools
import logging
from typing import Callable

from langgraph.errors import GraphBubbleUp, is_invalid_resume
from opentelemetry.trace import Status, StatusCode

from agent.state import DealState
from agent.telemetry import Telemetry

log = logging.getLogger(__name__)


def guarded(name: str, fn: Callable[[DealState], dict], tel: Telemetry) -> Callable[[DealState], dict]:
    """Run a node inside a telemetry span. Any error stops the run with a message naming the step.
    Interrupts (banker pauses) and invalid banker replies pass through untouched: an invalid reply is
    rejected by LangGraph without saving anything, so the banker can answer again."""
    @functools.wraps(fn)
    def run(state: DealState) -> dict:
        with tel.node(name, deal_id=state.get("deal_id"), run_id=state.get("run_id")) as sp:
            try:
                return fn(state)
            except GraphBubbleUp:
                raise
            except Exception as e:
                if is_invalid_resume(e):
                    raise
                log.exception("node %s failed", name)
                sp.record_exception(e)
                sp.set_attribute("run.error", True)
                sp.set_status(Status(StatusCode.ERROR, str(e)))
                tel.record_run_error(name)
                msg = f"{name}: {type(e).__name__}: {e}" if not isinstance(e, ValueError) else f"{name}: {e}"
                return {"status": "error", "error": msg, "log": [f"ERROR in {name}: {e}"]}
    return run
