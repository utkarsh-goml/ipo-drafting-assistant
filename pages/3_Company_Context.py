import pandas as pd
import streamlit as st

from core import context, store
from core.company_docs import part_ranges
from core.financials import DERIVED_LABELS, SPECS
from core.pdf_reader import render_unit
from core.ui import deal_sidebar, page_header, require_access, show_image

st.set_page_config(page_title="Company Context", page_icon="🏢", layout="wide")
require_access()
page_header("3 · Company Context",
            "What Python found in the company documents: parts, key financials, risk triggers and the "
           "pages relevant to each subsection. Everything here is editable and feeds the drafting packs.")

deal_id = deal_sidebar()
if not deal_id:
    st.stop()
docs = store.documents(deal_id, "company")
if not docs:
    st.info("Upload company documents (annual report, etc.) on **Deal Setup** first.")
    st.stop()
doc_by_id = {d.doc_id: d for d in docs}


@st.cache_data(show_spinner=False)
def _units(deal_id: str, doc_key: tuple):
    return context.load_units(deal_id)


doc_key = tuple((d.doc_id, d.path) for d in docs)
ind = context.load_indicators(deal_id)

c1, c2 = st.columns([1, 3])
label = "Process company documents" if ind is None else "Re-process (discards edits on this page)"
if c1.button(label, type="primary" if ind is None else "secondary"):
    with st.spinner("Reading pages (spreads are split into printed pages), tagging parts, "
                    "extracting financials, finding risk triggers… first run can take a minute per document"):
        _units.clear()
        context.process(deal_id)
    st.rerun()
if ind is None:
    st.stop()

units = _units(deal_id, doc_key)
lookup = context.unit_lookup(units)
triggers = context.load_triggers(deal_id)
mapping = context.load_mapping(deal_id)
c2.caption(f"{len(units)} printed pages across {len(docs)} document(s) · "
           f"sector pack: **{mapping.get('sector_pack') or 'generic'}**")


def show_unit(u, height=520):
    a, b = st.columns([1, 1])
    with a:
        st.markdown(f"**{u.cite}** · {u.location} · *{u.part}*")
        with st.container(height=height):
            st.text(u.text or "(no text on this page)")
    with b:
        show_image(render_unit(doc_by_id[u.doc_id].path, u.pdf_page, u.side))


tab_p, tab_f, tab_t, tab_m = st.tabs(["Document parts", "Financial indicators", "Risk triggers",
                                      "Pages per subsection"])

# ----------------------------------------------------------------- parts
with tab_p:
    rows = [{"Doc": r["doc_id"], "Part": r["part"], "Printed pages": f'{r["first"]}–{r["last"]}',
             "Count": r["pages"]} for r in part_ranges(units)]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.markdown("##### Look at a page")
    labels = {f"{u.cite} · {u.part}": u for u in units}
    pick = st.selectbox("Page", list(labels), index=min(3, len(labels) - 1), key="part_page")
    show_unit(labels[pick])

# ----------------------------------------------------------------- financials
with tab_f:
    yrs = ind.years or ["Current", "Previous"]
    st.markdown(f"Basis: **{ind.basis or '—'}** · Unit: **₹ in {ind.unit or '—'}** · Years: **{' vs '.join(yrs)}**")
    for n in ind.notes:
        st.caption(f"ℹ️ {n}")
    rows = []
    for key, lab, _, _ in SPECS:
        f = ind.figures.get(key)
        rows.append({"key": key, "label": lab,
                     "current": f.values[0] if f else None, "previous": f.values[1] if f else None,
                     "source": f.cite if f else "", "source line": f.source_line if f else ""})
    df = pd.DataFrame(rows)
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key="figs",
        column_order=["label", "current", "previous", "source", "source line"],
        disabled=["label", "source", "source line"],
        column_config={"label": "Figure", "current": st.column_config.NumberColumn(yrs[0], format="%.2f"),
                       "previous": st.column_config.NumberColumn(yrs[1] if len(yrs) > 1 else "Previous", format="%.2f")},
    )
    if st.button("Save figures"):
        context.update_figures(deal_id, ind, edited.to_dict("records"))
        st.success("Saved. Ratios and financial risk triggers recalculated.")
        st.rerun()

    st.markdown("##### Derived ratios")

    def fmt(v, kind):
        if v is None:
            return "—"
        return f"{v:.1%}" if kind == "pct" else (f"{v:.2f}x" if kind == "x" else f"{v:,.2f}")

    st.dataframe(pd.DataFrame([{"Ratio": lab, yrs[0]: fmt(ind.derived.get(k, [None, None])[0], kind),
                                (yrs[1] if len(yrs) > 1 else "Previous"): fmt(ind.derived.get(k, [None, None])[1], kind)}
                               for k, (lab, kind) in DERIVED_LABELS.items()]),
                 hide_index=True, use_container_width=True)

# ----------------------------------------------------------------- triggers
with tab_t:
    st.caption("**M** = mandatory under SEBI ICDR practice when the fact exists. Untick a trigger to keep it "
               "out of the Risk Factors draft.")
    tdf = pd.DataFrame([{"Include": t.selected, "M": "M" if t.mandatory else "", "Category": t.category,
                         "Suggested risk factor": t.title, "Why": t.reason,
                         "Evidence": ", ".join(e.cite for e in t.evidence), "key": t.key} for t in triggers])
    tedit = st.data_editor(tdf, hide_index=True, use_container_width=True, key="trig",
                           column_order=["Include", "M", "Category", "Suggested risk factor", "Why", "Evidence"],
                           disabled=["M", "Category", "Suggested risk factor", "Why", "Evidence"])
    if st.button("Save trigger selection"):
        sel = dict(zip(tedit["key"], tedit["Include"]))
        for t in triggers:
            t.selected = bool(sel.get(t.key, t.selected))
        context.save_triggers(deal_id, triggers)
        st.success("Saved.")
    with st.expander("Evidence snippets"):
        for t in triggers:
            if t.evidence:
                st.markdown(f"**{t.title}**")
                for e in t.evidence:
                    st.markdown(f"- `{e.cite}` ({e.location}): {e.snippet}")

# ----------------------------------------------------------------- mapping
with tab_m:
    kind = st.radio("For", ["our_business", "risk_themes"], horizontal=True,
                    format_func={"our_business": "Our Business subsections",
                                 "risk_themes": "Risk-factor themes"}.get)
    subs = mapping[kind]
    selected_units = {(h["doc_id"], h["unit_no"]) for s in subs.values() for h in s["hits"] if h["selected"]}
    st.caption(f"**{len(selected_units)} unique pages** currently selected across all "
               f"{'subsections' if kind == 'our_business' else 'themes'}. Each drafting run can carry about "
               f"60 source pages (90-page Stylus budget minus task sheet and style examples).")
    key = st.selectbox("Subsection", list(subs), format_func=lambda k: subs[k]["title"])
    hits = subs[key]["hits"]
    hdf = pd.DataFrame([{"Include": h["selected"], "Page": h["cite"], "Part": h["part"],
                         "Location": h["location"], "Relevance": h["score"]} for h in hits])
    hedit = st.data_editor(hdf, hide_index=True, use_container_width=True, key=f"map_{kind}_{key}",
                           disabled=["Page", "Part", "Location", "Relevance"],
                           column_config={"Relevance": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f")})
    if st.button("Save page selection"):
        for h, inc in zip(hits, hedit["Include"]):
            h["selected"] = bool(inc)
        context.save_mapping(deal_id, mapping)
        st.success("Saved.")
        st.rerun()
    if hits:
        pick = st.selectbox("Preview", [h["cite"] for h in hits], key=f"prev_{kind}_{key}")
        h = next(h for h in hits if h["cite"] == pick)
        show_unit(lookup[(h["doc_id"], h["unit_no"])], height=420)
