"""Quality scores with Ragas (LANGGRAPH_PLAN.md §7).

- Context relevance (before drafting): are the pages chosen for a work item about what it must cover?
- Faithfulness (after each draft or fix): is every claim backed by the pages it cites? The claims the
  judge finds unsupported are returned too, so the fix step is told exactly what to correct.

Ragas sends usage statistics to its makers by default; this module switches that off before Ragas loads.
Scores are never averaged away: a low score sends the part to fixing and, if that fails, the part is FAILED.
Tests inject their own `Scorer`; the Ragas one needs the OpenAI key and is used only in a live run.
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol

os.environ["RAGAS_DO_NOT_TRACK"] = "true"          # no usage data to Ragas' servers (set before any ragas import)

from agent.config import Settings  # noqa: E402


class EvalError(RuntimeError):
    """The judge call failed. The run stops; no score is assumed."""


@dataclass
class FaithResult:
    score: Optional[float]                     # None = the judge found no factual statements to check
    statements: int
    unsupported: List[str] = field(default_factory=list)    # "claim - reason"


# (model, tokens_in, tokens_out, latency_s) -> None; called once per OpenAI request the judge makes
OnUsage = Optional[Callable[[str, Optional[int], Optional[int], float], None]]


class Scorer(Protocol):
    def relevance(self, brief: str, contexts: List[str], *, on_usage: OnUsage = None) -> float: ...

    def faithfulness(self, brief: str, response: str, contexts: List[str], *,
                     on_usage: OnUsage = None) -> FaithResult: ...


def usage_from_response(body: Any) -> "tuple[Optional[str], Optional[int], Optional[int]]":
    """(model, prompt tokens, completion tokens) from an OpenAI chat-completions JSON body."""
    if not isinstance(body, dict):
        return None, None, None
    u = body.get("usage") or {}
    return body.get("model"), u.get("prompt_tokens"), u.get("completion_tokens")


def usage_hooks(on_usage, default_model: str) -> dict:
    """httpx event hooks that report each chat-completions response's token counts and duration.
    Ragas doesn't return token counts, so they are read from the OpenAI responses themselves."""
    import time

    async def on_request(request):
        request.extensions["ipo_t0"] = time.perf_counter()

    async def on_response(response):
        if not response.request.url.path.endswith("/chat/completions") or response.status_code != 200:
            return
        await response.aread()
        t0 = response.request.extensions.get("ipo_t0", time.perf_counter())
        try:
            body = response.json()
        except ValueError:
            body = None
        model, tin, tout = usage_from_response(body)
        on_usage(model or default_model, tin, tout, time.perf_counter() - t0)
    return {"request": [on_request], "response": [on_response]}


class RagasScorer:
    """Ragas ContextRelevance and Faithfulness with the configured OpenAI judge model."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _llm(self, on_usage: OnUsage = None):
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient
        from ragas.llms import llm_factory
        hooks = usage_hooks(on_usage, self.settings.openai_model_eval) if on_usage is not None else {}
        client = AsyncOpenAI(api_key=self.settings.openai_api_key.get_secret_value(),
                             timeout=self.settings.openai_timeout_s, max_retries=2,
                             http_client=DefaultAsyncHttpxClient(event_hooks=hooks))
        return llm_factory(self.settings.openai_model_eval, client=client)

    @staticmethod
    def _run(coro):
        try:
            return asyncio.run(coro)
        except Exception as e:
            raise EvalError(f"Ragas judge failed: {type(e).__name__}: {e}") from e

    def relevance(self, brief: str, contexts: List[str], *, on_usage: OnUsage = None) -> float:
        from ragas.metrics.collections import ContextRelevance
        if not contexts:
            raise EvalError("no pages to score")

        async def go():
            return await ContextRelevance(llm=self._llm(on_usage)).ascore(user_input=brief, retrieved_contexts=contexts)
        v = float(self._run(go()).value)
        if math.isnan(v):
            raise EvalError("context relevance came back empty (NaN)")
        return v

    def faithfulness(self, brief: str, response: str, contexts: List[str], *,
                     on_usage: OnUsage = None) -> FaithResult:
        from ragas.metrics.collections import Faithfulness
        if not contexts:
            raise EvalError("no cited pages to check the text against")

        async def go():
            m = Faithfulness(llm=self._llm(on_usage))
            # Same steps as Faithfulness.ascore, kept apart so the unsupported claims can be returned.
            statements = await m._create_statements(brief, response)
            if not statements:
                return FaithResult(score=None, statements=0)
            verdicts = await m._create_verdicts(statements, "\n".join(contexts))
            items = verdicts.statements
            if not items:
                return FaithResult(score=None, statements=0)
            ok = sum(1 for s in items if s.verdict)
            return FaithResult(score=ok / len(items), statements=len(items),
                               unsupported=[f"{s.statement} - {s.reason}" for s in items if not s.verdict])
        return self._run(go())


__all__ = ["Scorer", "RagasScorer", "FaithResult", "EvalError"]
