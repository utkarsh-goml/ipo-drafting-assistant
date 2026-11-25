"""Agent settings, read from environment variables (and `.env` if present).

There are deliberately no in-code defaults for anything that affects what the agent writes or
how it is judged: a missing or invalid value stops the agent with an error naming the variable.
See `.env.example` for the full list with suggested starting values.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      case_sensitive=False)

    # OpenAI
    openai_api_key: SecretStr = Field(min_length=1)
    openai_model_blueprint: str = Field(min_length=1)
    openai_model_draft: str = Field(min_length=1)
    openai_model_eval: str = Field(min_length=1)
    # tiktoken encoding used to count tokens before a call (tiktoken can't map new model names itself)
    openai_tokenizer: str = Field(min_length=1)
    openai_timeout_s: float = Field(gt=0, le=600)
    # "omit" = don't send a temperature (for models that reject it), else a number 0-2
    openai_temperature: str = Field(min_length=1)
    # Optional. USD per 1M tokens, {"model": [input, output]}, copied from OpenAI's pricing page.
    # A model not listed gets no cost figure (shown as "price not set"); a cost is never guessed.
    openai_prices: Dict[str, Tuple[float, float]] = Field(default_factory=dict)

    # limits and quality gates
    max_fix_rounds: int = Field(ge=0, le=5)
    max_gap_rounds: int = Field(ge=0, le=3)
    max_input_tokens: int = Field(gt=0)
    max_concurrency: int = Field(ge=1, le=16)
    faithfulness_min: float = Field(gt=0, le=1)
    relevance_min: float = Field(gt=0, le=1)

    # telemetry
    otel_enabled: bool
    otel_service_name: str = Field(min_length=1)
    deployment_environment: str = Field(min_length=1)          # e.g. local, render
    otel_metrics_exporter: Literal["otlp", "none"]             # "none" on Render (Phoenix keeps traces only)
    otel_exporter_otlp_endpoint: Optional[str] = None
    phoenix_api_key: Optional[SecretStr] = None
    otel_capture_content: bool

    @property
    def temperature_value(self) -> Optional[float]:
        return None if self.openai_temperature.strip().lower() == "omit" else float(self.openai_temperature)

    def cost_usd(self, model: str, tokens_in: Optional[int], tokens_out: Optional[int]) -> Optional[float]:
        """Cost of one call from OPENAI_PRICES, or None if the model has no price or tokens are unknown."""
        price = self.openai_prices.get(model)
        if price is None or tokens_in is None or tokens_out is None:
            return None
        return (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000

    @model_validator(mode="after")
    def _openai_values(self) -> "Settings":
        bad = [m for m, (pin, pout) in self.openai_prices.items() if pin < 0 or pout < 0]
        if bad:
            raise ValueError(f"OPENAI_PRICES: negative price for {', '.join(bad)}")
        t = self.openai_temperature.strip().lower()
        if t != "omit":
            try:
                ok = 0 <= float(t) <= 2
            except ValueError:
                ok = False
            if not ok:
                raise ValueError("OPENAI_TEMPERATURE must be 'omit' or a number from 0 to 2")
        import tiktoken
        if self.openai_tokenizer not in tiktoken.list_encoding_names():
            raise ValueError(f"OPENAI_TOKENIZER must be one of {', '.join(tiktoken.list_encoding_names())}")
        return self

    @model_validator(mode="after")
    def _telemetry_endpoint(self) -> "Settings":
        if self.otel_enabled and not (self.otel_exporter_otlp_endpoint or "").strip():
            raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_ENABLED=true")
        endpoint = (self.otel_exporter_otlp_endpoint or "").lower()
        has_key = self.phoenix_api_key is not None and self.phoenix_api_key.get_secret_value().strip()
        if self.otel_enabled and "phoenix.arize.com" in endpoint and not has_key:
            raise ValueError("PHOENIX_API_KEY is required when sending traces to Phoenix Cloud")
        return self


def _describe(err: ValidationError) -> List[str]:
    out = []
    for e in err.errors():
        name = "_".join(str(p) for p in e.get("loc", ())).upper()
        if e.get("type") == "missing":
            out.append(f"{name}: missing")
        else:
            out.append(f"{name or 'settings'}: {e.get('msg')}")
    return out


def load_settings(**overrides) -> Settings:
    """Read settings now. Raises ConfigError listing every missing/invalid variable."""
    try:
        return Settings(**overrides)
    except ValidationError as e:
        raise ConfigError("Agent settings are incomplete or invalid:\n  - "
                          + "\n  - ".join(_describe(e))
                          + "\nSee .env.example.") from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings for the running app (read once)."""
    return load_settings()
