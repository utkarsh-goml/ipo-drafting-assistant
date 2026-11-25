"""Per-deal local storage: data/deals/<deal_id>/{meta.json, uploads/, extracted/}."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import DocumentMeta, ExtractedSection
from .paths import DEALS_DIR as DATA_DIR


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return s[:50] or "deal"


def deal_dir(deal_id: str) -> Path:
    return DATA_DIR / deal_id


def list_deals() -> List[dict]:
    if not DATA_DIR.exists():
        return []
    out = []
    for d in sorted(DATA_DIR.iterdir()):
        if (d / "meta.json").exists():
            out.append(load_meta(d.name))
    return out


def create_deal(name: str, sector: str = "", fy: str = "") -> str:
    base = _slug(name)
    deal_id, i = base, 2
    while deal_dir(deal_id).exists():
        deal_id, i = f"{base}-{i}", i + 1
    (deal_dir(deal_id) / "uploads").mkdir(parents=True)
    save_meta(deal_id, {"deal_id": deal_id, "name": name, "sector": sector, "fy": fy,
                        "created": datetime.now().isoformat(timespec="seconds"), "docs": []})
    return deal_id


def load_meta(deal_id: str) -> dict:
    return json.loads((deal_dir(deal_id) / "meta.json").read_text(encoding="utf-8"))


def save_meta(deal_id: str, meta: dict) -> None:
    (deal_dir(deal_id) / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def documents(deal_id: str, kind: Optional[str] = None) -> List[DocumentMeta]:
    docs = [DocumentMeta(**d) for d in load_meta(deal_id)["docs"]]
    return [d for d in docs if kind is None or d.kind == kind]


def add_document(deal_id: str, *, kind: str, file_name: str, data: bytes,
                 doc_type: Optional[str] = None, page_count: int = 0) -> DocumentMeta:
    meta = load_meta(deal_id)
    prefix = "C" if kind == "comparable" else "D"
    used = [int(d["doc_id"][1:]) for d in meta["docs"] if d["doc_id"].startswith(prefix)]
    doc_id = f"{prefix}{max(used, default=0) + 1}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file_name).name)
    path = deal_dir(deal_id) / "uploads" / f"{doc_id}_{safe}"
    path.write_bytes(data)
    doc = DocumentMeta(doc_id=doc_id, kind=kind, name=file_name, path=str(path),
                       doc_type=doc_type, page_count=page_count)
    meta["docs"].append(doc.model_dump())
    save_meta(deal_id, meta)
    return doc


def remove_document(deal_id: str, doc_id: str) -> None:
    meta = load_meta(deal_id)
    for d in meta["docs"]:
        if d["doc_id"] == doc_id:
            Path(d["path"]).unlink(missing_ok=True)
    meta["docs"] = [d for d in meta["docs"] if d["doc_id"] != doc_id]
    save_meta(deal_id, meta)
    ext = deal_dir(deal_id) / "extracted"
    if ext.exists():
        for f in ext.glob(f"{doc_id}__*.json"):
            f.unlink()


def save_extraction(deal_id: str, section: ExtractedSection) -> None:
    ext = deal_dir(deal_id) / "extracted"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / f"{section.doc_id}__{section.section_key}.json").write_text(
        section.model_dump_json(indent=1), encoding="utf-8")


def load_extractions(deal_id: str) -> Dict[Tuple[str, str], ExtractedSection]:
    ext = deal_dir(deal_id) / "extracted"
    out: Dict[Tuple[str, str], ExtractedSection] = {}
    if ext.exists():
        for f in sorted(ext.glob("*.json")):
            s = ExtractedSection.model_validate_json(f.read_text(encoding="utf-8"))
            out[(s.doc_id, s.section_key)] = s
    return out


def delete_deal(deal_id: str) -> None:
    shutil.rmtree(deal_dir(deal_id), ignore_errors=True)
