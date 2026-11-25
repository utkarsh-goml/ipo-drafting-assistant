"""Step 8: page 6 (Streamlit AppTest, mock model) and deal backup / restore.

Done when: both checkpoints are usable from the page; restoring a backup resumes a paused run.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agent import app_runtime as RT
from agent import backup as B
from agent import tools
from agent.deps import AgentDeps
from core import store
from tests_agent.test_config import GOOD
from tests_agent.test_graph_a import _pdf
from tests_agent.test_graph_b import env  # noqa: F401  (fixture)

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "6_Agent_Run.py")


@pytest.fixture
def ui(env, monkeypatch, tmp_path):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)                                   # no developer .env is read
    env.make(stop=None)
    # the page's own settings (read from the environment), with the mock model and stub scorer
    monkeypatch.setattr(RT, "DEPS_FACTORY", lambda s, t: AgentDeps(settings=s, tel=env.tel, models=env.deps.models,
                                                                    scorer=env.deps.scorer))
    RT.reset()
    tools.add_document(env.deal, _pdf(tmp_path / "ar.pdf"), "company", "ANNUAL_REPORT")
    tools.add_document(env.deal, _pdf(tmp_path / "c1.pdf"), "comparable")
    yield env
    RT.reset()


def page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def click(at: AppTest, label: str) -> AppTest:
    btn = next((b for b in at.button if b.label == label), None)
    assert btn is not None, f"no button {label!r}; have {[b.label for b in at.button]}"
    btn.click().run()
    assert not at.exception, at.exception
    return at


def texts(at: AppTest) -> str:
    out = [m.value for m in at.markdown] + [m.value for m in at.caption]
    for kind in ("success", "info", "warning", "error"):
        out += [str(m.value) for m in getattr(at, kind)]
    return "\n".join(out)


def download_labels(at: AppTest):
    return sorted(d.proto.label for d in at.get("download_button"))


# --------------------------------------------------------------------------- both checkpoints from the page
def test_page_runs_both_checkpoints_and_offers_downloads(ui):
    at = page()
    assert "No run yet for this deal." in texts(at)
    click(at, "Start a new run")
    assert [s.value for s in at.subheader] == ["Checkpoint 1 · Approve the facts"]
    assert any(b.label == "Approve facts" for b in at.button)
    click(at, "Approve facts")
    assert [s.value for s in at.subheader] == ["Checkpoint 2 · Review the draft"]
    assert "Nothing blocks export" in texts(at)
    click(at, "Approve and export")
    assert "Approved and exported." in texts(at)
    assert download_labels(at) == [
        "checks.csv", "our_business.docx", "our_business.md", "risk_factors.docx", "risk_factors.md"]


def test_page_review_changes_then_approve(ui):
    at = page()
    click(at, "Start a new run")
    click(at, "Approve facts")
    # revise OB01 through the form
    box = next(t for t in at.text_input if t.label == "Ask the model to revise (instructions)")
    box.input("Make it shorter, keep the citation.")
    ui.fix.answer = lambda i: "### OB01 Overview\nWe are an asset management company [D1 p.5]."
    click(at, "Apply changes")
    assert ui.fix.seen == ["REV-OB01"]
    ob = next(t for t in at.text_area if t.label == "Text")
    assert ob.value == "We are an asset management company [D1 p.5]."
    click(at, "Approve and export")
    assert "Approved and exported." in texts(at)


def test_approve_with_unsaved_changes_is_stopped_on_the_page(ui):
    at = page()
    click(at, "Start a new run")
    click(at, "Approve facts")
    next(t for t in at.text_area if t.label == "Text").input("We are a large AMC [D1 p.5].")
    click(at, "Approve and export")
    assert "press **Apply changes** first" in "\n".join(str(e.value) for e in at.error)
    assert [s.value for s in at.subheader] == ["Checkpoint 2 · Review the draft"]


def test_missing_settings_and_password(ui, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    RT.reset()
    at = page()
    assert "settings are incomplete" in "\n".join(str(e.value) for e in at.error)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("APP_PASSWORD", "s3cret-pass")
    RT.reset()
    at = page()
    assert [t.label for t in at.text_input] == ["App password"]
    at.text_input[0].input("wrong")
    click(at, "Enter")
    assert "Wrong password." in "\n".join(str(e.value) for e in at.error)
    at.text_input[0].input("s3cret-pass")
    click(at, "Enter")
    assert "No run yet for this deal." in texts(at)


def test_shared_deployment_without_password_is_refused(ui, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "render")
    RT.reset()
    at = page()
    assert "APP_PASSWORD is not set" in "\n".join(str(e.value) for e in at.error)


# --------------------------------------------------------------------------- backup / restore
def test_restore_of_a_backup_resumes_the_paused_run(ui, monkeypatch, tmp_path):
    at = page()
    click(at, "Start a new run")
    assert [s.value for s in at.subheader] == ["Checkpoint 1 · Approve the facts"]
    data = B.backup_deal(ui.deal)
    # the server is wiped: a fresh, empty data folder (as on Render after sleeping)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "fresh" / "deals")
    assert store.list_deals() == []
    assert B.restore_deal(data) == ui.deal
    meta = store.load_meta(ui.deal)
    assert all(Path(d["path"]).parent == store.deal_dir(ui.deal) / "uploads" and Path(d["path"]).exists()
               for d in meta["docs"])
    at = page()
    assert [s.value for s in at.subheader] == ["Checkpoint 1 · Approve the facts"]     # the paused run is back
    click(at, "Approve facts")
    click(at, "Approve and export")
    assert "Approved and exported." in texts(at)
    assert len(download_labels(at)) == 5


def test_backup_contains_run_state_and_checksums(ui):
    page()
    data = B.backup_deal(ui.deal)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        m = json.loads(z.read("manifest.json"))
    assert {"manifest.json", "meta.json", "runs.sqlite"} <= names
    assert any(n.startswith("uploads/") for n in names)
    assert m["format"] == "ipo-deal-backup" and m["deal_id"] == ui.deal
    assert set(m["files"]) == names - {"manifest.json"}


def _rezip(data: bytes, change) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    files = {n: src.read(n) for n in src.namelist()}
    change(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return out.getvalue()


def test_restore_refuses_bad_files_and_changes_nothing(ui):
    page()
    data = B.backup_deal(ui.deal)
    before = sorted(p.name for p in store.DATA_DIR.iterdir())
    with pytest.raises(B.BackupError, match="already exists"):
        B.restore_deal(data)
    with pytest.raises(B.BackupError, match="not a deal backup"):
        B.restore_deal(b"not a zip")
    with pytest.raises(B.BackupError, match="damaged"):
        B.restore_deal(_rezip(data, lambda f: f.__setitem__("meta.json", f["meta.json"] + b" ")), replace=True)
    with pytest.raises(B.BackupError, match="don't match its manifest"):
        B.restore_deal(_rezip(data, lambda f: f.__setitem__("../evil.txt", b"x")), replace=True)

    def unsafe(f):
        m = json.loads(f["manifest.json"])
        m["files"]["../evil.txt"] = {"sha256": B._sha256(b"x"), "bytes": 1}
        f["manifest.json"] = json.dumps(m).encode()
        f["../evil.txt"] = b"x"
    with pytest.raises(B.BackupError, match="unsafe file path"):
        B.restore_deal(_rezip(data, unsafe), replace=True)
    assert sorted(p.name for p in store.DATA_DIR.iterdir()) == before
    assert not (store.DATA_DIR.parent / "evil.txt").exists()
    assert B.restore_deal(data, replace=True) == ui.deal                 # the real backup still restores
