"""Step 0: settings must be complete; nothing is filled in by default."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.config import ConfigError, Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent

GOOD = {
    "OPENAI_API_KEY": "sk-test",
    "OPENAI_MODEL_BLUEPRINT": "gpt-6-luna",
    "OPENAI_MODEL_DRAFT": "gpt-6-luna",
    "OPENAI_MODEL_EVAL": "gpt-6-luna",
    "OPENAI_TOKENIZER": "o200k_base",
    "OPENAI_TIMEOUT_S": "120",
    "OPENAI_TEMPERATURE": "0",
    "MAX_FIX_ROUNDS": "2",
    "MAX_GAP_ROUNDS": "1",
    "MAX_INPUT_TOKENS": "200000",
    "MAX_CONCURRENCY": "2",
    "FAITHFULNESS_MIN": "0.9",
    "RELEVANCE_MIN": "0.5",
    "OTEL_ENABLED": "false",
    "OTEL_SERVICE_NAME": "ipo-agent",
    "DEPLOYMENT_ENVIRONMENT": "test",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_CAPTURE_CONTENT": "false",
}


@pytest.fixture
def env(monkeypatch):
    for k in list(GOOD) + ["OTEL_EXPORTER_OTLP_ENDPOINT", "PHOENIX_API_KEY"]:
        monkeypatch.delenv(k, raising=False)

    def apply(values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
    return apply


def load():
    return load_settings(_env_file=None)     # never read a developer's real .env in tests


def test_complete_settings_load(env):
    env(GOOD)
    s = load()
    assert s.openai_model_draft == "gpt-6-luna"
    assert s.max_fix_rounds == 2 and s.faithfulness_min == 0.9
    assert s.openai_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(s)                  # key never printed


def test_nothing_set_lists_every_required_variable(env):
    with pytest.raises(ConfigError) as e:
        load()
    msg = str(e.value)
    for name in GOOD:
        assert f"{name}: missing" in msg


@pytest.mark.parametrize("name", sorted(GOOD))
def test_each_required_variable_has_no_default(env, name):
    env({k: v for k, v in GOOD.items() if k != name})
    with pytest.raises(ConfigError, match=f"{name}: missing"):
        load()


@pytest.mark.parametrize("name,value", [
    ("OPENAI_API_KEY", ""),
    ("OPENAI_MODEL_DRAFT", ""),
    ("FAITHFULNESS_MIN", "1.5"),
    ("RELEVANCE_MIN", "0"),
    ("MAX_FIX_ROUNDS", "-1"),
    ("MAX_INPUT_TOKENS", "0"),
    ("MAX_CONCURRENCY", "0"),
    ("OTEL_ENABLED", "maybe"),
    ("OTEL_METRICS_EXPORTER", "prometheus"),
    ("OPENAI_TOKENIZER", "gpt6_base"),
    ("OPENAI_TIMEOUT_S", "0"),
    ("OPENAI_TEMPERATURE", "warm"),
    ("OPENAI_TEMPERATURE", "3"),
    ("DEPLOYMENT_ENVIRONMENT", ""),
])
def test_invalid_values_are_rejected(env, name, value):
    env({**GOOD, name: value})
    with pytest.raises(ConfigError, match=name):
        load()


def test_telemetry_on_needs_endpoint(env):
    env({**GOOD, "OTEL_ENABLED": "true"})
    with pytest.raises(ConfigError, match="OTEL_EXPORTER_OTLP_ENDPOINT is required"):
        load()
    env({**GOOD, "OTEL_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"})
    assert load().otel_exporter_otlp_endpoint == "http://localhost:6006"


def test_temperature_omit_or_number(env):
    env({**GOOD, "OPENAI_TEMPERATURE": "omit"})
    assert load().temperature_value is None
    env({**GOOD, "OPENAI_TEMPERATURE": "0.2"})
    assert load().temperature_value == 0.2


def test_phoenix_cloud_needs_api_key(env):
    cloud = {**GOOD, "OTEL_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://app.phoenix.arize.com/s/demo"}
    env(cloud)
    with pytest.raises(ConfigError, match="PHOENIX_API_KEY is required"):
        load()
    env({**cloud, "PHOENIX_API_KEY": "px-test"})
    assert load().phoenix_api_key.get_secret_value() == "px-test"


def test_settings_class_declares_no_defaults_for_required_fields():
    required = {n for n, f in Settings.model_fields.items() if f.is_required()}
    assert required == {k.lower() for k in GOOD}


def _paths_in_subprocess(env_vars):
    code = ("from core import paths, store, pdf_reader;"
            "print(paths.DEALS_DIR); print(store.DATA_DIR); print(pdf_reader.CACHE_DIR)")
    env = {k: v for k, v in os.environ.items() if k not in ("IPO_DATA_DIR", "IPO_CACHE_DIR")}
    env.update(env_vars)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_data_dirs_default_to_project_data():
    deals, store_dir, cache = _paths_in_subprocess({})
    assert deals == store_dir == str(ROOT / "data" / "deals")
    assert cache == str(ROOT / "data" / "cache")


def test_data_dirs_follow_env(tmp_path):
    deals, store_dir, cache = _paths_in_subprocess({"IPO_DATA_DIR": str(tmp_path)})
    assert deals == store_dir == str(tmp_path.resolve() / "deals")
    assert cache == str(tmp_path.resolve() / "cache")
    _, _, cache2 = _paths_in_subprocess({"IPO_DATA_DIR": str(tmp_path), "IPO_CACHE_DIR": str(tmp_path / "c")})
    assert cache2 == str((tmp_path / "c").resolve())
