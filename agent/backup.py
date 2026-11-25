"""Deal backup and restore (LANGGRAPH_PLAN.md §10).

On Render free the disk is wiped when the app sleeps. A backup is one .zip of the deal folder: documents,
extracted sections, company context, drafts, exports, and the run state (`runs.sqlite`), so a paused run
resumes after a restore. Nothing is saved anywhere else.

    data = backup_deal(deal_id)            # bytes of the .zip
    deal_id = restore_deal(data)           # refuses a damaged or unknown file; never half-restores

The zip holds `manifest.json` with a SHA-256 for every file. Restore checks every one before writing anything.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Dict

from core import store

FORMAT = "ipo-deal-backup"
VERSION = 1
MANIFEST = "manifest.json"
RUNS_DB = "runs.sqlite"
MAX_RESTORE_BYTES = 2 * 1024 ** 3          # refuse archives that unpack to more than 2 GB


class BackupError(ValueError):
    """The backup can't be made or restored; the message says why. Nothing was changed."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sqlite_copy(src: Path) -> bytes:
    """A consistent copy of the run database, even while it is open (sqlite backup API)."""
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / RUNS_DB
        s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        return dst.read_bytes()


def backup_deal(deal_id: str) -> bytes:
    root = store.deal_dir(deal_id)
    if not (root / "meta.json").exists():
        raise BackupError(f"Unknown deal {deal_id!r}.")
    files: Dict[str, bytes] = {}
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if rel.startswith(RUNS_DB):                      # runs.sqlite, plus -wal / -shm while open
            continue
        files[rel] = f.read_bytes()
    if (root / RUNS_DB).exists():
        files[RUNS_DB] = _sqlite_copy(root / RUNS_DB)
    manifest = {"format": FORMAT, "version": VERSION, "deal_id": deal_id,
                "deal_name": store.load_meta(deal_id)["name"],
                "created": datetime.now().isoformat(timespec="seconds"),
                "files": {k: {"sha256": _sha256(v), "bytes": len(v)} for k, v in files.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def read_manifest(data: bytes) -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BackupError("This is not a deal backup (.zip) file.") from None
    with z:
        if MANIFEST not in z.namelist():
            raise BackupError("This zip is not a deal backup: manifest.json is missing.")
        m = json.loads(z.read(MANIFEST))
    if m.get("format") != FORMAT:
        raise BackupError("This zip is not a deal backup made by this app.")
    if m.get("version") != VERSION:
        raise BackupError(f"Backup version {m.get('version')} is not supported (this app reads version {VERSION}).")
    return m


def _safe(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and ":" not in name and "\\" not in name


def restore_deal(data: bytes, *, replace: bool = False) -> str:
    """Restore a deal from its backup. Same deal id as before. If the deal exists, `replace=True` is needed."""
    m = read_manifest(data)
    deal_id = m["deal_id"]
    if not _safe(deal_id) or "/" in deal_id:
        raise BackupError(f"Backup has an invalid deal id {deal_id!r}.")
    files = m.get("files") or {}
    if "meta.json" not in files:
        raise BackupError("Backup has no meta.json.")
    if sum(v["bytes"] for v in files.values()) > MAX_RESTORE_BYTES:
        raise BackupError("Backup is larger than 2 GB unpacked; refused.")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist()) - {MANIFEST}
        if names != set(files):
            extra, missing = sorted(names - set(files)), sorted(set(files) - names)
            raise BackupError("Backup contents don't match its manifest"
                              + (f"; not listed: {', '.join(extra[:5])}" if extra else "")
                              + (f"; missing: {', '.join(missing[:5])}" if missing else ""))
        content = {}
        for name, info in files.items():
            if not _safe(name):
                raise BackupError(f"Backup has an unsafe file path {name!r}.")
            b = z.read(name)
            if len(b) != info["bytes"] or _sha256(b) != info["sha256"]:
                raise BackupError(f"Backup file {name} is damaged (checksum does not match).")
            content[name] = b

    target = store.deal_dir(deal_id)
    if target.exists():
        if not replace:
            raise BackupError(f"Deal {deal_id!r} already exists here. Delete it first, or choose to replace it.")
    staging = target.parent / f".restore-{deal_id}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for name, b in content.items():
            p = staging / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b)
        # document paths are stored as absolute paths: point them at this machine's deal folder
        meta = json.loads((staging / "meta.json").read_text(encoding="utf-8"))
        for d in meta.get("docs", []):
            d["path"] = str(target / "uploads" / Path(d["path"]).name)
        (staging / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return deal_id


__all__ = ["backup_deal", "restore_deal", "read_manifest", "BackupError"]
