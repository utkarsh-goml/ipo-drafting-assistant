import pandas as pd
import streamlit as st

from core import store
from core.pdf_reader import read_pages, render_page
from core.section_extractor import extract_sections, load_config, manual_section, search_pages
from core.ui import deal_sidebar, page_header, require_access, show_image

st.set_page_config(page_title="Section Extraction", page_icon="✂️", layout="wide")
require_access()
page_header("2 · Section Extraction",
            "Finds *Our Business* and *Risk Factors* in each comparable prospectus from its table of contents, "
            "and checks that each starts at a bold capital-letter heading. "
           "Check the page ranges below and correct them if needed.")

deal_id = deal_sidebar()
if not deal_id:
    st.stop()

cfg = load_config()
SECTIONS = {k: v["title"] for k, v in cfg["sections"].items()}
comps = store.documents(deal_id, "comparable")
if not comps:
    st.info("Upload comparable DRHPs/RHPs on **Deal Setup** first.")
    st.stop()

results = store.load_extractions(deal_id)
pending = [d for d in comps if any((d.doc_id, k) not in results for k in SECTIONS)]

# ----------------------------------------------------------------- run extraction
c1, c2 = st.columns([1, 3])
rerun_all = c2.checkbox("Re-run for all comparables (discards manual corrections)")
targets = comps if rerun_all else pending
if c1.button(f"Extract sections ({len(targets)} document(s))", type="primary", disabled=not targets):
    with st.status("Extracting sections…", expanded=True) as status:
        for d in targets:
            status.write(f"**{d.doc_id}** · {d.name} ({d.page_count} pages) - reading text and headings…")
            res = extract_sections(d.path, doc_id=d.doc_id, doc_name=d.name)
            for key, sec in res.items():
                if sec:
                    store.save_extraction(deal_id, sec)
                    status.write(f"  {SECTIONS[key]}: PDF p{sec.start_page}-{sec.end_page} "
                                 f"({sec.method}, confidence {sec.confidence:.0%}) · "
                                 f"bold capital heading: start {'✓' if sec.start_verified else '✗'}, "
                                 f"end {'✓' if sec.end_verified else '✗'}")
                else:
                    status.write(f"  ⚠ {SECTIONS[key]}: not found - enter the pages manually below")
        status.update(label="Extraction finished", state="complete")
    st.rerun()

if not results:
    st.stop()

# ----------------------------------------------------------------- summary / overrides
st.subheader("Detected page ranges")
rows = []
for d in comps:
    for key, title in SECTIONS.items():
        s = results.get((d.doc_id, key))
        rows.append({
            "ID": d.doc_id, "Document": d.name, "Section": title, "_key": key,
            "Start page": s.start_page if s else None, "End page": s.end_page if s else None,
            "Pages": s.page_count if s else None,
            "Start line": s.start_line if s else None, "End line": s.end_line if s else None,
            "Next section": (s.next_section_title or "") if s else "",
            "Heading check": (("✓" if s.start_verified else "✗") + " start · "
                              + ("✓" if s.end_verified else "✗") + " end") if s else "",
            "Method": s.method if s else "not found",
            "Confidence": s.confidence if s else 0.0,
        })
df = pd.DataFrame(rows)
edited = st.data_editor(
    df, hide_index=True, use_container_width=True, key="ranges",
    column_order=["ID", "Document", "Section", "Start page", "End page", "Pages", "Start line",
                  "End line", "Next section", "Heading check", "Method", "Confidence"],
    disabled=["ID", "Document", "Section", "Pages", "Start line", "End line", "Next section",
              "Heading check", "Method", "Confidence"],
    column_config={
        "Start page": st.column_config.NumberColumn(min_value=1, step=1, help="PDF page (not printed page)"),
        "End page": st.column_config.NumberColumn(min_value=1, step=1, help="PDF page (not printed page)"),
        "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
        "Heading check": st.column_config.TextColumn(
            help="✓ = the page has a bold, capital-letter heading matching the section name (start) "
                 "or the next section's name (end)"),
    },
)
st.caption("Page numbers are **PDF page numbers** (what a PDF viewer shows), not the printed page numbers. "
           "Edit *Start page* / *End page* and click **Apply corrections** to re-extract that range.")

changed = edited[(edited["Start page"] != df["Start page"]) | (edited["End page"] != df["End page"])]
changed = changed.dropna(subset=["Start page", "End page"])
if st.button(f"Apply corrections ({len(changed)})", disabled=changed.empty):
    by_id = {d.doc_id: d for d in comps}
    for _, r in changed.iterrows():
        d = by_id[r["ID"]]
        sec = manual_section(d.path, doc_id=d.doc_id, doc_name=d.name, section_key=r["_key"],
                             start_page=int(r["Start page"]), end_page=int(r["End page"]))
        store.save_extraction(deal_id, sec)
    st.rerun()

# ----------------------------------------------------------------- detail viewer
st.divider()
st.subheader("Review an extracted section")
available = [(d, k) for d in comps for k in SECTIONS if (d.doc_id, k) in results]
if not available:
    st.stop()
choice = st.selectbox("Section", available,
                      format_func=lambda x: f"{x[0].doc_id} · {SECTIONS[x[1]]} · {x[0].name}")
doc, key = choice
sec = results[(doc.doc_id, key)]

m1, m2, m3, m4 = st.columns(4)
m1.metric("PDF pages", f"{sec.start_page}–{sec.end_page}", f"{sec.page_count} pages", delta_color="off")
m2.metric("Words", f"{sec.word_count():,}")
m3.metric("Subheadings found", len(sec.subheadings))
m4.metric("Method", sec.method, f"confidence {sec.confidence:.0%}", delta_color="off")
if sec.start_verified and sec.end_verified:
    st.success(f"Starts at the bold capital heading **{sec.section_title.upper()}** and ends before the bold "
               f"capital heading **{sec.next_section_title}** (both listed in the table of contents).")
else:
    miss = [x for x, ok in (("start", sec.start_verified), ("end", sec.end_verified)) if not ok]
    st.warning(f"No bold capital heading found at the {' and '.join(miss)}. Check the page images below "
               "and correct the page range in the table above if needed.")
for n in sec.notes:
    st.caption(f"ℹ️ {n}")

tab_b, tab_o, tab_t = st.tabs(["Start & end pages", "Outline (subheadings)", "Extracted text"])

with tab_b:
    a, b = st.columns(2)
    first, last = sec.pages[0], sec.pages[-1]
    with a:
        st.markdown(f"**Starts** · PDF page {sec.start_page}, line {sec.start_line}")
        st.code(first.lines[0] if first.lines else "(empty)", language=None)
        show_image(render_page(doc.path, sec.start_page, 80))
    with b:
        st.markdown(f"**Ends** · PDF page {sec.end_page}, line {sec.end_line} · "
                    f"next section: *{sec.next_section_title or '—'}*")
        st.code(last.lines[-1] if last.lines else "(empty)", language=None)
        show_image(render_page(doc.path, sec.end_page, 80))
    with st.expander("Look at any other page of this document (to correct a range)"):
        # widget keys are per document, so switching documents doesn't carry a page or search over
        peek_key, hit_key = f"peek_{doc.doc_id}", f"peek_hit_{doc.doc_id}"
        s1, s2 = st.columns([3, 1])
        query = s1.text_input("Search the text of this document",
                              placeholder="e.g. OUR BUSINESS, KEY REGULATIONS AND POLICIES, Risk Factors",
                              key=f"peek_q_{doc.doc_id}")
        heads_only = s2.checkbox("Headings only", key=f"peek_h_{doc.doc_id}",
                                 help="Only lines in capital letters, such as section headings. "
                                      "A section starts at its bold capital heading.")
        if query.strip():
            hits = search_pages(read_pages(doc.path), query, headings_only=heads_only)
            if not hits:
                st.info("No page contains that text. Try fewer words, or untick *Headings only*.")
            else:
                more = " (first 200 shown)" if len(hits) >= 200 else ""
                st.caption(f"Found on {len({h['page'] for h in hits})} page(s){more}. "
                           "Pick a result to open that page below.")
                st.dataframe(pd.DataFrame([{
                    "PDF page": h["page"], "Printed page": h["printed"], "Line": h["line"],
                    "Heading?": "✓" if h["heading"] else "", "Text": h["text"][:160]} for h in hits]),
                    hide_index=True, use_container_width=True, height=min(400, 38 + 35 * len(hits)))
                labels = {f"PDF p.{h['page']} · line {h['line']} · {h['text'][:80]}": h["page"] for h in hits}

                def _open_hit():
                    picked = st.session_state.get(hit_key)
                    if picked in labels:
                        st.session_state[peek_key] = labels[picked]

                st.selectbox("Open a result", list(labels), index=None, key=hit_key,
                             placeholder="Choose a result to open its page", on_change=_open_hit)
        if peek_key not in st.session_state:          # first view: open at the section start
            st.session_state[peek_key] = sec.start_page
        p = st.number_input("PDF page", min_value=1, max_value=max(1, doc.page_count), key=peek_key)
        st.caption("Type this PDF page number into *Start page* or *End page* in the table above, "
                   "then click **Apply corrections**.")
        st.image(render_page(doc.path, int(p), 80), width=700)

with tab_o:
    if sec.subheadings:
        st.dataframe(pd.DataFrame([{"PDF page": h.page_no, "Heading": h.text} for h in sec.subheadings]),
                     hide_index=True, use_container_width=True, height=520)
    else:
        st.info("No bold subheadings detected.")

with tab_t:
    text = sec.text_with_markers()
    st.download_button("Download text (.txt)", text.encode("utf-8"),
                       file_name=f"{doc.doc_id}_{key}.txt", mime="text/plain")
    with st.container(height=600):
        st.text(text)
