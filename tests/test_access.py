"""Password gate on every page (core.ui.require_access). Runs on Python 3.8 too."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["app.py"] + sorted(str(p.relative_to(ROOT)) for p in (ROOT / "pages").glob("[1-6]_*.py"))


@pytest.fixture
def at_for(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    from core import store
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "deals")
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)

    def make(page="app.py"):
        return AppTest.from_file(str(ROOT / page), default_timeout=60).run()
    return make


def test_every_page_calls_the_gate_first():
    for page in PAGES:
        lines = (ROOT / page).read_text(encoding="utf-8").splitlines()
        i = next(n for n, line in enumerate(lines) if line.startswith("st.set_page_config("))
        assert lines[i + 1] == "require_access()", page          # right after page config, before anything is shown


def test_local_without_password_is_open(at_for):
    at = at_for()
    assert not at.exception and [t.label for t in at.text_input] == []
    assert "IPO Drafting Assistant" in [t.value for t in at.title]


def test_password_asked_and_checked(at_for, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret-pass")
    at = at_for("pages/1_Deal_Setup.py")
    assert [t.label for t in at.text_input] == ["App password"] and len(at.title) == 0      # nothing else shown
    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert "Wrong password." in [e.value for e in at.error] and len(at.title) == 0
    at.text_input[0].input("s3cret-pass")
    at.button[0].click().run()
    assert "1 · Deal Setup" in [t.value for t in at.title]


def test_shared_deployment_without_password_refused(at_for, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "render")
    at = at_for()
    assert [e.value for e in at.error] == ["APP_PASSWORD is not set. On a shared deployment the app must have a password."]
    assert len(at.title) == 0
