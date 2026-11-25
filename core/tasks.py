"""LLM tasks (one Stylus preset run each): model + storage under data/deals/<deal>/tasks/<task_id>/."""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from . import store

PRESETS = {
    "IPO-01": {"name": "IPO-01 Style & Blueprint v1.1", "file": "IPO-01_Style_and_Blueprint.md",
               "title": "Learn style & build blueprint"},
    "IPO-02": {"name": "IPO-02 Draft Our Business v1.0", "file": "IPO-02_Draft_Our_Business.md",
               "title": "Draft Our Business"},
    "IPO-03": {"name": "IPO-03 Draft Risk Factors v1.0", "file": "IPO-03_Draft_Risk_Factors.md",
               "title": "Draft Risk Factors"},
    "IPO-04": {"name": "IPO-04 Revise Subsection v1.0", "file": "IPO-04_Revise_Subsection.md",
               "title": "Revise one subsection"},
}
PRESET_DIR = Path(__file__).resolve().parent / "prompts" / "presets"
PAGE_LIMIT = 100       # Stylus limit across all files of one run
PAGE_BUDGET = 90       # what we allow ourselves (safety margin)
SIZE_LIMIT_MB = 10.0   # Stylus limit across all files of one run
SIZE_BUDGET_MB = 9.0   # what we allow ourselves (safety margin)


class TaskFile(BaseModel):
    name: str
    role: str            # sheet | source | style
    pages: int
    size_bytes: int = 0


class Task(BaseModel):
    task_id: str
    kind: str            # IPO-01 | IPO-02 | IPO-03 | IPO-04
    created: str
    status: str = "pack_ready"   # pack_ready | reply_parsed | reply_error
    files: List[TaskFile] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    source_cites: List[str] = Field(default_factory=list)
    dropped_cites: List[str] = Field(default_factory=list)
    reply_file: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
    target: Optional[dict] = None   # IPO-04: {"section", "item_id", "instructions"}

    @property
    def total_pages(self) -> int:
        return sum(f.pages for f in self.files)

    @property
    def total_mb(self) -> float:
        return sum(f.size_bytes for f in self.files) / 2 ** 20

    @property
    def preset(self) -> str:
        return PRESETS[self.kind]["name"]


def preset_text(kind: str) -> str:
    return (PRESET_DIR / PRESETS[kind]["file"]).read_text(encoding="utf-8")


def new_task_id(kind: str) -> str:
    return f"{kind.replace('-', '')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def tasks_root(deal_id: str) -> Path:
    d = store.deal_dir(deal_id) / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_dir(deal_id: str, task_id: str) -> Path:
    d = tasks_root(deal_id) / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_task(deal_id: str, task: Task) -> None:
    (task_dir(deal_id, task.task_id) / "task.json").write_text(task.model_dump_json(indent=1), encoding="utf-8")


def load_task(deal_id: str, task_id: str) -> Optional[Task]:
    f = tasks_root(deal_id) / task_id / "task.json"
    return Task.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


def list_tasks(deal_id: str, kind: Optional[str] = None) -> List[Task]:
    out = []
    for d in sorted(tasks_root(deal_id).iterdir(), reverse=True):
        t = load_task(deal_id, d.name)
        if t and (kind is None or t.kind == kind):
            out.append(t)
    return out


def zip_pack(deal_id: str, task: Task) -> Path:
    """Zip the PDFs of a pack for one-click download."""
    d = task_dir(deal_id, task.task_id)
    z = d / f"{task.task_id}_pack.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in task.files:
            zf.write(d / f.name, arcname=f.name)
    return z
