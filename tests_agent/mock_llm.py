"""Test-only chat model. Lives in tests_agent/, so a deployed app can't select it.

Replies come from a list, one per call. Each call's messages are kept in `calls` so tests can check
what was sent. Structured output behaves like ChatOpenAI's `include_raw=True`: the reply text is parsed
against the schema, and a mismatch comes back as `parsing_error` (no repair).
"""
from __future__ import annotations

import json
from typing import Any, List, Optional, Union

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError


class MockChatModel(BaseChatModel):
    replies: List[Union[str, Exception]]
    calls: List[List[BaseMessage]] = []
    model_name: str = "mock-model"
    tokens_in: int = 1000
    tokens_out: int = 200

    @property
    def _llm_type(self) -> str:
        return "mock"

    def _next(self, messages: List[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("MockChatModel: no reply left for this call")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return AIMessage(content=r, usage_metadata={"input_tokens": self.tokens_in, "output_tokens": self.tokens_out,
                                                    "total_tokens": self.tokens_in + self.tokens_out})

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def with_structured_output(self, schema, *, method: str = "json_schema", include_raw: bool = False,
                               strict: Optional[bool] = None, **kwargs):
        assert method == "json_schema" and strict is True and include_raw is True, "agent must ask for strict JSON"

        def run(messages):
            msgs = messages.to_messages() if hasattr(messages, "to_messages") else messages
            raw = self._next(msgs)
            try:
                return {"raw": raw, "parsed": schema.model_validate(json.loads(raw.content)), "parsing_error": None}
            except (ValueError, ValidationError) as e:
                return {"raw": raw, "parsed": None, "parsing_error": e}
        return RunnableLambda(run)
