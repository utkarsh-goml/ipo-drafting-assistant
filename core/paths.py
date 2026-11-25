"""Where the app keeps its files.

IPO_DATA_DIR  root folder for deals/ and cache/ (default: <project>/data, as before)
IPO_CACHE_DIR text cache only (default: <IPO_DATA_DIR>/cache)

Read once at import. Set the variables before starting the app (e.g. on Render: /tmp/ipo/data).
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name, "").strip()
    return Path(v).expanduser().resolve() if v else default


DATA_ROOT = _env_path("IPO_DATA_DIR", PROJECT_DIR / "data")
DEALS_DIR = DATA_ROOT / "deals"
CACHE_DIR = _env_path("IPO_CACHE_DIR", DATA_ROOT / "cache")
