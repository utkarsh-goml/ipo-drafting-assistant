"""What the graph needs from outside: settings, telemetry, chat models and the quality scorer.

`AgentDeps.from_settings` builds the real ones (OpenAI, Ragas). Tests build their own with a mock
model and a stub scorer; nothing in the app can select those.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from langchain_core.language_models import BaseChatModel

from agent.config import Settings
from agent.evals import Scorer
from agent.telemetry import Telemetry


@dataclass
class AgentDeps:
    settings: Settings
    tel: Telemetry
    models: Dict[str, BaseChatModel]       # "blueprint", "draft", "fix"
    scorer: Scorer

    @classmethod
    def from_settings(cls, settings: Settings, tel: Telemetry) -> "AgentDeps":
        from agent.evals import RagasScorer
        from agent.llm.client import make_chat_model
        return cls(settings=settings, tel=tel,
                   models={c: make_chat_model(settings, c) for c in ("blueprint", "draft", "fix")},
                   scorer=RagasScorer(settings))

    def model(self, call: str) -> BaseChatModel:
        if call not in self.models:
            raise KeyError(f"no chat model configured for {call!r}")
        return self.models[call]
