"""OpenAI chat models and the two kinds of call the agent makes.

- `make_chat_model(settings, call)` builds a ChatOpenAI for one call type, with the model from settings.
  The OpenAI SDK retries only transient errors (timeouts, connection errors, 408/409/429/5xx), 2 retries
  with backoff (3 tries). Anything else fails at once. There is no switch to another model or provider.
- `call_blueprint` (call 1): strict structured output checked against `BlueprintOut`; any mismatch is
  `BlueprintInvalid`. No repair, no default blueprint.
- `call_text` (calls 2-4): markdown items `### <ID> ...`; returns the text for `replies.split_items`.

Before every call the input is counted (tiktoken) and `ItemTooLarge` is raised over MAX_INPUT_TOKENS.
Tests inject their own chat model; nothing here can select a mock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.config import Settings
from agent.llm.prompts import preset_version, system_prompt
from agent.llm.schemas import BlueprintInvalid, BlueprintOut, to_blueprint
from agent.pack_text import ItemPack, check_budget
from agent.telemetry import Telemetry
from core.blueprint import Blueprint

CallType = Literal["blueprint", "draft", "fix", "revise", "eval"]
MAX_RETRIES = 2          # OpenAI SDK retries after the first try: 3 tries in all, transient errors only


class LLMError(RuntimeError):
    """The model call failed (after retries for transient errors). The run stops with this message."""


def model_for(settings: Settings, call: CallType) -> str:
    return {"blueprint": settings.openai_model_blueprint, "draft": settings.openai_model_draft,
            "fix": settings.openai_model_draft, "revise": settings.openai_model_draft,
            "eval": settings.openai_model_eval}[call]


def make_chat_model(settings: Settings, call: CallType) -> BaseChatModel:
    from langchain_openai import ChatOpenAI
    kwargs: dict = dict(model=model_for(settings, call), api_key=settings.openai_api_key,
                        timeout=settings.openai_timeout_s, max_retries=MAX_RETRIES)
    if settings.temperature_value is not None:
        kwargs["temperature"] = settings.temperature_value
    return ChatOpenAI(**kwargs)


@dataclass
class LLMResult:
    item_id: str
    call: str
    model: str
    text: str
    tokens_in_estimate: int          # tiktoken count before the call
    tokens_in: Optional[int]         # as reported by the API (None if it reported nothing)
    tokens_out: Optional[int]
    latency_s: float
    cost_usd: Optional[float] = None  # from OPENAI_PRICES; None = price not set


def _model_name(model: BaseChatModel) -> str:
    return str(getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__)


def _usage(msg: Any):
    u = getattr(msg, "usage_metadata", None) or {}
    return u.get("input_tokens"), u.get("output_tokens")


def _messages(kind: str, pack: ItemPack):
    return [SystemMessage(content=system_prompt(kind)), HumanMessage(content=pack.render())]


def _record(tel: Telemetry, res: LLMResult, settings: Settings) -> None:
    res.cost_usd = settings.cost_usd(res.model, res.tokens_in, res.tokens_out)
    tel.record_llm_call(call=res.call, model=res.model, tokens_in=res.tokens_in or 0,
                        tokens_out=res.tokens_out or 0, latency_s=res.latency_s, cost_usd=res.cost_usd)


def _invoke(runnable, messages, *, what: str):
    try:
        return runnable.invoke(messages)
    except (BlueprintInvalid, LLMError):
        raise
    except Exception as e:                     # openai.APIError & co: already retried if transient
        raise LLMError(f"{what}: model call failed: {type(e).__name__}: {e}") from e


def call_text(model: BaseChatModel, pack: ItemPack, *, call: CallType, settings: Settings,
              tel: Telemetry, deal_id: str = "") -> LLMResult:
    """Calls 2-4: returns the model's markdown text (not parsed here)."""
    if pack.kind == "IPO-01":
        raise ValueError("use call_blueprint for IPO-01")
    msgs = _messages(pack.kind, pack)
    n = check_budget(pack.item_id, msgs[0].content, msgs[1].content, settings.openai_tokenizer,
                     settings.max_input_tokens)
    with tel.span(f"llm.{call}", deal_id=deal_id or None, work_item_id=pack.item_id,
                  llm_preset=preset_version(pack.kind), pages_sent=len(pack.source),
                  tokens_estimate=n) as sp:
        t0 = time.perf_counter()
        msg = _invoke(model, msgs, what=pack.item_id)
        tin, tout = _usage(msg)
        text = msg.content if isinstance(msg, AIMessage) else str(getattr(msg, "content", msg))
        if not isinstance(text, str) or not text.strip():
            raise LLMError(f"{pack.item_id}: the model returned no text")
        res = LLMResult(pack.item_id, call, _model_name(model), text, n, tin, tout, time.perf_counter() - t0)
        _record(tel, res, settings)
        sp.set_attribute("latency.s", res.latency_s)
    return res


def call_blueprint(model: BaseChatModel, pack: ItemPack, *, settings: Settings, tel: Telemetry,
                   deal_id: str = "", source: str = "agent") -> "tuple[Blueprint, LLMResult]":
    """Call 1: strict JSON blueprint. Raises BlueprintInvalid (schema or content rules) or LLMError."""
    if pack.kind != "IPO-01":
        raise ValueError("call_blueprint needs an IPO-01 pack")
    msgs = _messages("IPO-01", pack)
    n = check_budget(pack.item_id, msgs[0].content, msgs[1].content, settings.openai_tokenizer,
                     settings.max_input_tokens)
    structured = model.with_structured_output(BlueprintOut, method="json_schema", strict=True, include_raw=True)
    with tel.span("llm.blueprint", deal_id=deal_id or None, work_item_id=pack.item_id,
                  llm_preset=preset_version("IPO-01"), pages_sent=len(pack.source) + len(pack.style),
                  tokens_estimate=n) as sp:
        t0 = time.perf_counter()
        out = _invoke(structured, msgs, what="BLUEPRINT")
        raw, parsed, err = out.get("raw"), out.get("parsed"), out.get("parsing_error")
        tin, tout = _usage(raw)
        res = LLMResult("BLUEPRINT", "blueprint", _model_name(model),
                        raw.content if isinstance(getattr(raw, "content", None), str) else "",
                        n, tin, tout, time.perf_counter() - t0)
        _record(tel, res, settings)
        sp.set_attribute("latency.s", res.latency_s)
        refusal = (getattr(raw, "additional_kwargs", None) or {}).get("refusal")
        if refusal:
            raise BlueprintInvalid(f"The model refused: {refusal}")
        if err is not None or parsed is None:
            raise BlueprintInvalid(f"Blueprint reply does not match the schema: {err or 'nothing parsed'}")
        if not isinstance(parsed, BlueprintOut):
            parsed = BlueprintOut.model_validate(parsed)
        bp = to_blueprint(parsed, source=source)
        sp.set_attributes({"blueprint.subsections": len(bp.our_business.subsections),
                           "blueprint.themes": len(bp.risk_factors.themes)})
    return bp, res
