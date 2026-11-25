"""Shared Streamlit helpers."""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path
from typing import Optional

import streamlit as st

from . import store

ASSETS = Path(__file__).resolve().parent.parent / "assets"
def show_image(img, **kw) -> None:
    """Full-width image on any Streamlit version (1.40 renamed use_column_width)."""
    try:
        st.image(img, use_container_width=True, **kw)
    except TypeError:
        st.image(img, use_column_width=True, **kw)


LOGO_NAMES = ["citi_logo.svg", "citi_logo.png", "citi_logo.jpg", "logo.svg", "logo.png"]


def _logo_html(height: int = 44) -> str:
    """Official logo from assets/ if present, else a neutral text badge."""
    for name in LOGO_NAMES:
        f = ASSETS / name
        if f.exists():
            mime = "image/svg+xml" if f.suffix == ".svg" else f"image/{f.suffix.lstrip('.').replace('jpg', 'jpeg')}"
            b64 = base64.b64encode(f.read_bytes()).decode()
            return f'<img src="data:{mime};base64,{b64}" style="height:{height}px" alt="logo">'
    return ('<span style="font-family:Arial,Helvetica,sans-serif;font-weight:700;font-size:30px;'
            'letter-spacing:1px;color:#056DAE">citi</span>')


def require_access() -> None:
    """Password gate for a shared deployment (called first on every page).

    APP_PASSWORD set: ask for it once per browser session. Not set: allowed only when DEPLOYMENT_ENVIRONMENT
    is unset or "local" (the office VDI, a laptop); anywhere else the app refuses to show anything.
    """
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected.strip():
        where = os.environ.get("DEPLOYMENT_ENVIRONMENT", "").strip().lower()
        if where not in ("", "local"):
            st.error("APP_PASSWORD is not set. On a shared deployment the app must have a password.")
            st.stop()
        return
    if st.session_state.get("app_auth"):
        return
    with st.form("app_login"):
        given = st.text_input("App password", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
                st.session_state["app_auth"] = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


def page_header(title: str, caption: Optional[str] = None) -> None:
    """Page title on the left, logo on the right (same on every page)."""
    left, right = st.columns([6, 1])
    with left:
        st.title(title)
        if caption:
            st.caption(caption)
    with right:
        st.markdown(f'<div style="text-align:right;padding-top:18px">{_logo_html()}</div>',
                    unsafe_allow_html=True)


def deal_sidebar() -> Optional[str]:
    """Deal picker in the sidebar. Returns the selected deal_id (or None)."""
    deals = store.list_deals()
    with st.sidebar:
        st.markdown("### Deal")
        if not deals:
            st.info("No deal yet. Create one on **Deal Setup**.")
            return None
        ids = [d["deal_id"] for d in deals]
        labels = {d["deal_id"]: f'{d["name"]} ({d["deal_id"]})' for d in deals}
        current = st.session_state.get("deal_id")
        idx = ids.index(current) if current in ids else 0
        deal_id = st.selectbox("Active deal", ids, index=idx, format_func=labels.get,
                               label_visibility="collapsed")
        st.session_state["deal_id"] = deal_id
        meta = store.load_meta(deal_id)
        n_c = sum(d["kind"] == "comparable" for d in meta["docs"])
        n_d = sum(d["kind"] == "company" for d in meta["docs"])
        st.caption(f'Sector: {meta.get("sector") or "-"} · FY: {meta.get("fy") or "-"}')
        st.caption(f"{n_d} company document(s) · {n_c} comparable(s)")
    return deal_id
