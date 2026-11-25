"""What page 6 needs once per app process: settings, telemetry and the agent's dependencies.
The password gate is `core.ui.require_access`, on every page.

`DEPS_FACTORY` builds the real OpenAI / Ragas dependencies. Tests replace it with a mock; nothing in the app
can choose a mock.
"""
from __future__ import annotations

import threading
from typing import Callable

from agent.config import Settings, load_settings
from agent.deps import AgentDeps
from agent.telemetry import Telemetry, setup_telemetry

DEPS_FACTORY: Callable[[Settings, Telemetry], AgentDeps] = AgentDeps.from_settings
_lock = threading.Lock()
_cache: dict = {}


def runtime() -> AgentDeps:
    """Settings + telemetry + models, made once per process (raises ConfigError if settings are missing)."""
    with _lock:
        if "deps" not in _cache:
            settings = load_settings()
            tel = setup_telemetry(settings)
            _cache["deps"] = DEPS_FACTORY(settings, tel)
        return _cache["deps"]


def reset() -> None:
    """Forget the cached runtime (tests; or after the settings changed)."""
    with _lock:
        d = _cache.pop("deps", None)
    if d is not None:
        d.tel.force_flush()

