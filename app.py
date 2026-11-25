"""IPO Drafting Assistant - home page.

Run:  streamlit run app.py
"""
import streamlit as st

from core.ui import deal_sidebar, page_header, require_access

st.set_page_config(page_title="IPO Drafting Assistant", page_icon="📄", layout="wide")
require_access()
deal_sidebar()

page_header("IPO Drafting Assistant", "Proof of concept · drafts the *Our Business* and *Risk Factors* sections of a DRHP")

st.markdown(
    """
**Workflow**

| Step | Page | What happens | Status |
|---|---|---|---|
| 1 | **Deal Setup** | Create a deal, upload company documents (annual report, etc.) and comparable DRHPs/RHPs | ✅ Available |
| 2 | **Section Extraction** | Python finds *Our Business* and *Risk Factors* in each comparable; you review and correct page ranges | ✅ Available |
| 3 | **Company Context** | Annual-report parts, key financials and ratios, risk triggers, pages per subsection | ✅ Available |
| 4 | **LLM Tasks** | Download packs → run Stylus presets → upload replies (blueprint, Our Business, Risk Factors) | ✅ Available |
| 5 | **Section Workspace** | Edit each subsection with its cited pages, comparables and automatic checks; version history; IPO-04 revise; Word / Markdown export | ✅ Available |

Use the pages in the left sidebar to move between steps.
"""
)
