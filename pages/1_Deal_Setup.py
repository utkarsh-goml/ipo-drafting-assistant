import pandas as pd
import streamlit as st

from core import store
from core.pdf_reader import page_count
from core.ui import deal_sidebar, page_header, require_access

st.set_page_config(page_title="Deal Setup", page_icon="📁", layout="wide")
require_access()
page_header("1 · Deal Setup")

with st.expander("Create a new deal", expanded=not store.list_deals()):
    with st.form("new_deal", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1, 1])
        name = c1.text_input("Company / deal name", placeholder="e.g. SBI Funds Management Limited")
        sector = c2.text_input("Sector", placeholder="e.g. Asset Management")
        fy = c3.text_input("Latest FY", placeholder="FY2025")
        if st.form_submit_button("Create deal", type="primary") and name.strip():
            st.session_state["deal_id"] = store.create_deal(name.strip(), sector.strip(), fy.strip())
            st.rerun()

deal_id = deal_sidebar()
if not deal_id:
    st.stop()

meta = store.load_meta(deal_id)
st.subheader(meta["name"])

DOC_TYPES = {
    "ANNUAL_REPORT": "Annual report",
    "FINANCIALS": "Audited financial statements",
    "INVESTOR_PPT": "Investor / company presentation",
    "MGMT_QA": "Management Q&A / notes",
    "OTHER": "Other company document",
}


def _save_uploads(files, kind, doc_type=None):
    existing = {d.name for d in store.documents(deal_id, kind)}
    added = 0
    for f in files or []:
        if f.name in existing:
            continue
        data = f.getvalue()
        doc = store.add_document(deal_id, kind=kind, file_name=f.name, data=data, doc_type=doc_type)
        try:
            n = page_count(doc.path)
        except Exception as e:  # unreadable PDF
            store.remove_document(deal_id, doc.doc_id)
            st.error(f"Could not read {f.name}: {e}")
            continue
        m = store.load_meta(deal_id)
        for d in m["docs"]:
            if d["doc_id"] == doc.doc_id:
                d["page_count"] = n
        store.save_meta(deal_id, m)
        added += 1
    return added


left, right = st.columns(2)
with left:
    st.markdown("#### Company documents (sources of facts)")
    st.caption("Annual report plus any extra company-provided documents. IDs: D1, D2, …")
    dtype = st.selectbox("Document type", list(DOC_TYPES), format_func=DOC_TYPES.get)
    files = st.file_uploader("Upload company PDFs", type=["pdf"], accept_multiple_files=True,
                             key=f"company_{dtype}")
    if st.button("Add company documents", disabled=not files):
        n = _save_uploads(files, "company", dtype)
        st.success(f"Added {n} document(s).")
        st.rerun()

with right:
    st.markdown("#### Comparable DRHPs / RHPs (style references)")
    st.caption("Prospectuses of comparable companies. IDs: C1, C2, …")
    files = st.file_uploader("Upload comparable prospectus PDFs", type=["pdf"],
                             accept_multiple_files=True, key="comparables")
    if st.button("Add comparables", disabled=not files):
        n = _save_uploads(files, "comparable")
        st.success(f"Added {n} document(s).")
        st.rerun()

st.divider()
docs = store.documents(deal_id)
if not docs:
    st.info("No documents uploaded yet.")
    st.stop()

st.markdown("#### Documents in this deal")
df = pd.DataFrame([{
    "ID": d.doc_id,
    "Role": "Company" if d.kind == "company" else "Comparable",
    "Type": DOC_TYPES.get(d.doc_type, "DRHP / RHP") if d.kind == "company" else "DRHP / RHP",
    "File": d.name,
    "Pages": d.page_count,
} for d in docs])
st.dataframe(df, hide_index=True, use_container_width=True)

c1, c2 = st.columns([3, 1])
to_remove = c1.selectbox("Remove a document", ["—"] + [d.doc_id for d in docs])
if c2.button("Remove", disabled=to_remove == "—"):
    store.remove_document(deal_id, to_remove)
    st.rerun()
