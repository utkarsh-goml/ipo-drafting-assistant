import pandas as pd
import streamlit as st

from core import blueprint as bpmod
from core import context, packer, replies, store
from core.tasks import (PAGE_BUDGET, PAGE_LIMIT, PRESETS, SIZE_BUDGET_MB, SIZE_LIMIT_MB, list_tasks, preset_text,
                        task_dir, zip_pack)
from core.ui import deal_sidebar, page_header, require_access

st.set_page_config(page_title="LLM Tasks", page_icon="🤖", layout="wide")
require_access()
page_header("4 · LLM Tasks (Stylus)",
            "Each step makes a pack for one Stylus preset. Download it, upload all its files to that preset "
            "in Stylus, download the reply, then upload the reply here.")

deal_id = deal_sidebar()
if not deal_id:
    st.stop()

# ----------------------------------------------------------------- prerequisites
has_ext = bool(store.load_extractions(deal_id))
has_ctx = context.load_indicators(deal_id) is not None
c1, c2 = st.columns(2)
c1.markdown(("✅" if has_ext else "⚠️") + " Comparable sections extracted (page 2)")
c2.markdown(("✅" if has_ctx else "⚠️") + " Company context processed (page 3)")
if not has_ctx:
    st.info("Process the company documents on **Company Context** first.")
    st.stop()

with st.expander("Stylus presets: register once in Stylus Workspace (shared, fixed prompts)"):
    st.caption("Create one preset per entry below, using exactly the name and text shown. The text never changes "
               "per deal. Everything deal-specific travels in the Task Sheet PDF.")
    for kind, p in PRESETS.items():
        st.markdown(f"**{p['name']}**")
        st.code(preset_text(kind), language=None)
        st.download_button(f"Download {kind} preset text", preset_text(kind).encode("utf-8"),
                           file_name=p["file"], key=f"dl_preset_{kind}")


# ----------------------------------------------------------------- helpers
def task_card(task):
    status = {"pack_ready": "🟡 waiting for Stylus reply", "reply_parsed": "✅ reply read",
              "reply_error": "❌ reply could not be read"}[task.status]
    with st.container(border=True):
        st.markdown(f"**{task.task_id}** · preset **{task.preset}** · {status}")
        a, b = st.columns([3, 2])
        with a:
            st.dataframe(pd.DataFrame([{"File": f.name, "Role": f.role, "PDF pages": f.pages,
                                        "Size (MB)": round(f.size_bytes / 2 ** 20, 2)} for f in task.files]),
                         hide_index=True, use_container_width=True)
            used = task.total_pages
            p1, p2 = st.columns(2)
            p1.progress(min(1.0, used / PAGE_LIMIT),
                        text=f"{used} of {PAGE_LIMIT} Stylus pages (budget {PAGE_BUDGET})")
            mb = task.total_mb
            if mb:
                p2.progress(min(1.0, mb / SIZE_LIMIT_MB),
                            text=f"{mb:.1f} of {SIZE_LIMIT_MB:.0f} MB (budget {SIZE_BUDGET_MB:.0f})")
            else:
                p2.caption("Size not recorded (pack made before the size check) - create the pack again.")
            if task.dropped_cites:
                st.caption(f"{len(task.dropped_cites)} relevant page(s) left out for budget: "
                           + ", ".join(task.dropped_cites[:15]) + ("…" if len(task.dropped_cites) > 15 else ""))
            for n in task.notes:
                st.warning(n)
        with b:
            z = zip_pack(deal_id, task)
            st.download_button("⬇ Download pack (.zip)", z.read_bytes(), file_name=z.name,
                               key=f"zip_{task.task_id}", type="primary", use_container_width=True)
            sheet = next(f for f in task.files if f.role == "sheet")
            st.download_button("Task Sheet only (.pdf)", (task_dir(deal_id, task.task_id) / sheet.name).read_bytes(),
                               file_name=sheet.name, key=f"sheet_{task.task_id}", use_container_width=True)
            st.markdown(f"1. Unzip, open preset **{task.preset}** in Stylus\n"
                        f"2. Upload all {len(task.files)} PDFs and run\n"
                        "3. Download the reply (Word / text) and upload it below")
        up = st.file_uploader("Stylus reply file", type=["docx", "md", "txt", "pdf"], key=f"up_{task.task_id}")
        if up and st.button("Read reply", key=f"read_{task.task_id}"):
            try:
                _, result = replies.ingest_reply(deal_id, up.getvalue(), up.name, expected_task=task.task_id)
                st.success("Reply read and saved.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        for e in task.errors:
            st.error(e)
        if task.kind == "IPO-01" and task.status == "reply_parsed":
            b = bpmod.load(deal_id)
            if b is not None and b.source == task.task_id:
                st.success(f"Blueprint read: {len(b.our_business.subsections)} Our Business subsections and "
                           f"{len(b.risk_factors.themes)} risk themes. Check them under *Review / edit the blueprint*.")
        reply_text = task_dir(deal_id, task.task_id) / "reply_text.md"
        if reply_text.exists():
            with st.expander("Text the tool read from the reply file"):
                st.caption("If something is missing here, the file lost it (for example a PDF export). "
                           "Upload Stylus's reply as .txt, .md or Word instead.")
                st.code(reply_text.read_text(encoding="utf-8")[:30000], language=None)


def latest(kind):
    ts = list_tasks(deal_id, kind)
    return ts[0] if ts else None


def build(kind):
    with st.spinner("Selecting pages, stamping labels and writing the Task Sheet…"):
        try:
            packer.BUILDERS[kind](deal_id)
        except ValueError as e:
            st.error(str(e))
            return
    st.rerun()


# ----------------------------------------------------------------- step 1 blueprint
st.subheader("Step 1 · Blueprint (structure & style)")
with st.expander("What is the blueprint, and how do I use it?", expanded=False):
    st.markdown(
        """
The **blueprint** is the outline Stylus follows when drafting: which *Our Business* subsections to
write, in what order, what each covers and how long it is; and which *Risk Factors* groups and
themes to cover. It is printed in the Task Sheet of the IPO-02 and IPO-03 packs.

**Two ways to get one**
- **Use default blueprint** - instant, no Stylus run: 15 standard DRHP subsections plus risk themes
  from the risk triggers found on page 3. Generic, but fine to start.
- **Create IPO-01 pack** - one Stylus run that learns the outline from your comparables. The pack holds
  the full heading list of each comparable section, a few company overview pages, and selected comparable
  pages. Upload it to preset *IPO-01* in Stylus, then upload the reply below: it replaces the blueprint.

**Editing** (*Review / edit the blueprint*): untick a subsection or theme to leave it out (its pages are
then not picked, freeing page budget); change a title, purpose, target words or risk group. Click **Save
blueprint**. Edits only affect packs you create **afterwards** - existing drafts do not change. A changed
title becomes the heading Stylus writes; the pages picked for that subsection stay the same.

**Order:** blueprint → *Create IPO-02 pack* (Our Business) → *Create IPO-03 pack* (Risk Factors).
"""
    )
bp = bpmod.load(deal_id)
c1, c2, c3 = st.columns([1, 1, 2])
if c1.button("Create IPO-01 pack", disabled=not has_ext, help="Learn structure and style from the comparables in Stylus"):
    build("IPO-01")
if c2.button("Use default blueprint", help="Start without IPO-01: a generic DRHP blueprint plus this deal's risk triggers"):
    bpmod.save(deal_id, bpmod.default_blueprint(deal_id))
    st.rerun()
c3.caption(f"Current blueprint: **{bp.source if bp else 'none'}**")
t1 = latest("IPO-01")
if t1:
    task_card(t1)

if bp:
    with st.expander("Review / edit the blueprint", expanded=False):
        st.caption("Untick entries you don't want drafted; edit titles or purposes. Saved to this deal.")
        obdf = pd.DataFrame([{"include": s.include, "id": s.id, "title": s.title, "purpose": s.purpose,
                              "target words": s.target_words} for s in bp.our_business.subsections])
        ob_ed = st.data_editor(obdf, hide_index=True, use_container_width=True, key="bp_ob",
                               disabled=["id"], num_rows="fixed")
        rfdf = pd.DataFrame([{"include": t.include, "id": t.id, "group": t.group, "theme": t.theme}
                             for t in bp.risk_factors.themes])
        rf_ed = st.data_editor(rfdf, hide_index=True, use_container_width=True, key="bp_rf",
                               disabled=["id"], num_rows="fixed",
                               column_config={"group": st.column_config.SelectboxColumn(options=bp.risk_factors.groups)})
        if st.button("Save blueprint"):
            for s, r in zip(bp.our_business.subsections, ob_ed.to_dict("records")):
                s.include, s.title, s.purpose = bool(r["include"]), r["title"], r["purpose"]
                tw = r.get("target words")
                s.target_words = int(tw) if tw == tw and tw is not None else None
            for t, r in zip(bp.risk_factors.themes, rf_ed.to_dict("records")):
                t.include, t.group, t.theme = bool(r["include"]), r["group"], r["theme"]
            bpmod.save(deal_id, bp)
            st.success("Blueprint saved.")


# ----------------------------------------------------------------- steps 2 & 3
def draft_summary(section):
    d = replies.load_draft(deal_id, section)
    if not d:
        return
    n_c = sum(len(i.citations) for i in d.items)
    bad = [c for i in d.items for c in i.unresolved]
    comp = [c for i in d.items for c in i.comparable_cites]
    outside = [c for i in d.items for c in i.not_in_pack]
    tbd = sum(i.tbd for i in d.items)
    st.markdown(f"**Latest draft** (from {d.task_id}): {len(d.items)} items · {n_c} citations · {tbd} [TBD] · "
                f"{len(d.gaps)} gaps listed")
    if bad:
        st.error(f"Citations that match no page: {', '.join(sorted(set(bad)))}")
    if comp:
        st.warning(f"Comparable pages cited as sources (not allowed): {', '.join(sorted(set(comp)))}")
    if outside:
        st.warning(f"Real pages cited that were not in the pack (verify): {', '.join(sorted(set(outside)))}")
    if d.missing_ids:
        st.warning(f"Blueprint subsections missing from the reply: {', '.join(d.missing_ids)}")
    with st.expander("Preview draft (editing comes in the Section Workspace)"):
        for i in d.items:
            st.markdown(f"#### {i.id} {('[' + i.group + '] ') if i.group else ''}{i.title}")
            st.markdown(i.text)
        if d.gaps:
            st.markdown("#### Gaps to obtain from the company")
            st.markdown("\n".join(f"- {g}" for g in d.gaps))


for kind, section, label in (("IPO-02", "our_business", "Step 2 · Draft Our Business"),
                             ("IPO-03", "risk_factors", "Step 3 · Draft Risk Factors")):
    st.subheader(label)
    if st.button(f"Create {kind} pack", disabled=bp is None, key=f"build_{kind}",
                 help=None if bp else "Create a blueprint first"):
        build(kind)
    t = latest(kind)
    if t:
        task_card(t)
    draft_summary(section)
