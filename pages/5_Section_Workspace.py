import re

import pandas as pd
import streamlit as st
from rapidfuzz import fuzz

from core import context, packer, replies, store, validator, workspace
from core.company_docs import find_unit
from core.pdf_reader import render_page, render_unit
from core.replies import CITE_BLOCK_RX, CITE_RX
from core.tasks import PAGE_LIMIT, list_tasks, task_dir, zip_pack
from core.ui import deal_sidebar, page_header, require_access, show_image

st.set_page_config(page_title="Section Workspace", page_icon="✍️", layout="wide")
require_access()
page_header("5 · Section Workspace",
            "Review and edit each drafted subsection with its source pages, the comparables and automatic checks "
            "side by side. Drafted by Stylus (Sonnet 5.5); every edit is versioned.")

deal_id = deal_sidebar()
if not deal_id:
    st.stop()

drafts = {s: replies.load_draft(deal_id, s) for s in workspace.SECTION_TITLES}
available = [s for s, d in drafts.items() if d and d.items]
if not available:
    st.info("No drafts yet. Run IPO-02 / IPO-03 on **LLM Tasks** and upload the Stylus replies first.")
    st.stop()

docs = {d.doc_id: d for d in store.documents(deal_id)}


@st.cache_data(show_spinner=False)
def _units(deal_id: str, key: tuple):
    return context.load_units(deal_id)


units = _units(deal_id, tuple((d.doc_id, d.path) for d in docs.values()))
ind = context.load_indicators(deal_id)

# ----------------------------------------------------------------- selectors
c1, c2, c3 = st.columns([1.2, 3, 1.6])
section = c1.radio("Section", available, format_func=lambda s: workspace.SECTION_TITLES[s], horizontal=True)
draft = drafts[section]
comp_sents = validator.comparable_sentences(deal_id, section)


@st.cache_data(show_spinner=False)
def _issues(deal_id, section, item_id, title, text, n_units):
    item = replies.DraftItem(id=item_id, title=title, text=text)
    return [i.model_dump() for i in validator.check_item(section, item, units, ind, comp_sents)]


def issues_for(item):
    return [validator.Issue(**d) for d in _issues(deal_id, section, item.id, item.title, item.text, len(units))]


all_issues = {i.id: issues_for(i) for i in draft.items}
SEV = {"high": "🔴", "medium": "🟠", "low": "⚪"}


def label(i):
    iss = all_issues[i.id]
    n_hi = sum(x.severity == "high" for x in iss)
    mark = "🔴" if n_hi else ("🟠" if iss else "🟢")
    edited = " ✎" if i.source == "edit" or i.versions else ""
    return f"{mark} {i.id} {i.title[:70]}{edited}"


ids = [i.id for i in draft.items]
view = c3.radio("View", ["Subsection", "Full section"], horizontal=True)
item_id = c2.selectbox("Subsection", ids, format_func=lambda x: label(workspace.get_item(draft, x)),
                       disabled=view == "Full section", key=f"sel_{section}")
item = workspace.get_item(draft, item_id)

# overall status line + downloads
n_hi = sum(x.severity == "high" for v in all_issues.values() for x in v)
n_all = sum(len(v) for v in all_issues.values())
n_tbd = sum(len(replies.TBD_RX.findall(i.text)) for i in draft.items)
d1, d2, d3, d4 = st.columns([3, 1, 1, 1])
d1.caption(f"Draft from **{draft.task_id}** · {len(draft.items)} items · {n_hi} high / {n_all} total check issues · "
           f"{n_tbd} [TBD] · {len(draft.gaps)} gaps")
d2.download_button("⬇ Word (.docx)", workspace.section_docx(deal_id, section),
                   file_name=f"{deal_id}_{section}.docx", use_container_width=True)
d3.download_button("⬇ Markdown", workspace.section_markdown(deal_id, section).encode("utf-8"),
                   file_name=f"{deal_id}_{section}.md", use_container_width=True)
d4.download_button("⬇ Check report", pd.DataFrame(
    [{"item": k, "severity": x.severity, "kind": x.kind, "message": x.message, "excerpt": x.excerpt}
     for k, v in all_issues.items() for x in v]).to_csv(index=False).encode("utf-8"),
    file_name=f"{deal_id}_{section}_checks.csv", use_container_width=True)

if section == "risk_factors":
    req = [(t.key, t.title) for t in context.load_triggers(deal_id) if t.selected and t.mandatory]
    miss = validator.uncovered_required([i.title for i in draft.items], [i.text for i in draft.items], req)
    if miss:
        st.error(f"**{len(miss)} required risk(s) not covered** — re-run IPO-03 on LLM Tasks or write them here:\n"
                 + "\n".join(f"- {x.message.split(': ', 1)[1]}" for x in miss))
if draft.missing_ids:
    st.warning(f"Blueprint subsections missing from the draft: {', '.join(draft.missing_ids)}")

st.divider()

# ----------------------------------------------------------------- full section view
if view == "Full section":
    with st.container(height=900):
        md = workspace.section_markdown(deal_id, section, with_ids=True)
        st.markdown(re.sub(r"(?m)^(#{1,4}) ", lambda m: "#" * (len(m.group(1)) + 2) + " ", md))
    st.stop()


# ----------------------------------------------------------------- helpers
def cites_in(text):
    out = []
    for blk in CITE_BLOCK_RX.findall(text):
        for d, k, n in CITE_RX.findall(blk):
            c = f"{d} {k}.{n}"
            if c not in out:
                out.append(c)
    return out


def highlight(text, needles):
    """Return markdown with matching numbers in bold (for the reference page text)."""
    esc = text.replace("*", "∗").replace("_", "\\_").replace("#", "\\#")
    for n in sorted(set(needles), key=len, reverse=True):
        if len(n) >= 3:
            esc = esc.replace(n, f"**{n}**")
    return esc


left, right = st.columns([1.15, 1])

# ----------------------------------------------------------------- editor
with left:
    st.markdown(f"**{item.id}** · {'group ' + item.group + ' · ' if item.group else ''}source: `{item.source or draft.task_id}`"
                f"{' · ' + str(len(item.versions)) + ' earlier version(s)' if item.versions else ''}")
    key = f"{section}_{item.id}_{len(item.versions)}"
    groups = ["Internal", "External", "Offer"]
    if section == "risk_factors":
        g = st.selectbox("Group", groups, index=groups.index(item.group) if item.group in groups else 0, key=f"g_{key}")
    else:
        g = None
    new_title = st.text_input("Heading", item.title, key=f"t_{key}")
    new_text = st.text_area("Text (markdown; keep citations like [D1 p.145])", item.text, height=520, key=f"x_{key}")
    with st.expander("Preview (as formatted text)"):
        st.markdown(f"**{new_title}**\n\n{new_text}")
    b1, b2, b3 = st.columns([1, 1, 2])
    if b1.button("💾 Save", type="primary", use_container_width=True):
        if workspace.save_item_edit(deal_id, section, item.id, new_title, new_text, g):
            st.toast("Saved. Checks re-run.")
        else:
            st.toast("No changes.")
        st.rerun()
    if b2.button("↺ Discard", use_container_width=True, help="Throw away unsaved changes"):
        for k in (f"t_{key}", f"x_{key}", f"g_{key}"):
            st.session_state.pop(k, None)
        st.rerun()
    changed = new_title.strip() != item.title or new_text.strip() != item.text.strip()
    b3.caption("⚠ unsaved changes" if changed else "")

    if item.versions:
        with st.expander(f"Version history ({len(item.versions)})"):
            for vi in range(len(item.versions) - 1, -1, -1):
                v = item.versions[vi]
                cA, cB = st.columns([4, 1])
                cA.markdown(f"**v{vi + 1}** · {v.at} · from `{v.source}` · {len(v.text.split())} words")
                if cB.button("Restore", key=f"rv_{key}_{vi}"):
                    workspace.restore_version(deal_id, section, item.id, vi)
                    st.rerun()
                with st.container(height=160):
                    st.markdown(f"**{v.title}**\n\n{v.text}")

    # --- IPO-04 revise with Stylus
    with st.expander("🤖 Revise this subsection with Stylus (preset IPO-04)"):
        st.caption("Builds a small pack: the current text, your instructions, the check results, the pages this "
                   "subsection cites or is mapped to, and a few comparable pages. The reply replaces this subsection "
                   "(the current text is kept as a version).")
        instr = st.text_area("Instructions for the revision", key=f"instr_{key}",
                             placeholder="e.g. Add the AUM by product table; shorten to ~400 words; fix flagged numbers.")
        inc = st.checkbox("Include the check results", value=True, key=f"inc_{key}")
        if st.button("Create IPO-04 pack", key=f"b4_{key}"):
            msgs = [f"{x.severity.upper()}: {x.message}" for x in all_issues[item.id]] if inc else None
            try:
                with st.spinner("Selecting pages and writing the Task Sheet…"):
                    packer.build_ipo04(deal_id, section, item.id, instr, msgs)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        mine = [t for t in list_tasks(deal_id, "IPO-04")
                if (t.target or {}).get("section") == section and (t.target or {}).get("item_id") == item.id]
        if mine:
            t = mine[0]
            st.markdown(f"**{t.task_id}** · {t.total_pages}/{PAGE_LIMIT} pages · {t.total_mb:.1f}/10 MB · "
                        + {"pack_ready": "🟡 waiting for reply", "reply_parsed": "✅ reply merged",
                           "reply_error": "❌ reply error"}[t.status])
            z = zip_pack(deal_id, t)
            st.download_button("⬇ Download IPO-04 pack (.zip)", z.read_bytes(), file_name=z.name, key=f"z4_{t.task_id}")
            if t.status == "pack_ready":
                up = st.file_uploader("Stylus reply", type=["docx", "md", "txt", "pdf"], key=f"u4_{t.task_id}")
                if up and st.button("Read reply", key=f"r4_{t.task_id}"):
                    try:
                        replies.ingest_reply(deal_id, up.getvalue(), up.name, expected_task=t.task_id)
                        st.success("Revision merged.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
            for e in t.errors:
                st.error(e)

# ----------------------------------------------------------------- right-hand tabs
with right:
    iss = issues_for(replies.DraftItem(id=item.id, title=new_title, text=new_text)) if changed else all_issues[item.id]
    n_hi_item = sum(x.severity == "high" for x in iss)
    tab_ref, tab_comp, tab_chk = st.tabs([f"📄 References ({len(cites_in(new_text))})", "📚 Comparables",
                                          f"✅ Checks ({len(iss)}{' · ' + str(n_hi_item) + ' high' if n_hi_item else ''})"])

    with tab_ref:
        cites = cites_in(new_text)
        if not cites:
            st.info("This text has no citations.")
        else:
            cite = st.selectbox("Citation", cites, key=f"rc_{key}")
            if cite.startswith("C"):
                st.error("A comparable page is cited as a source of facts. Replace it with a company page.")
            u = find_unit(units, cite)
            if not u:
                st.error(f"[{cite}] matches no page in the company documents.")
            else:
                # numbers from the paragraphs citing this page
                paras = [p for p in re.split(r"\n\s*\n", new_text) if cite in " ".join(
                    f"{d} {k}.{n}" for blk in CITE_BLOCK_RX.findall(p) for d, k, n in CITE_RX.findall(blk))]
                nums = [n.strip("()% ") for p in paras for n in validator.numbers_to_check(p)]
                st.markdown(f"**{u.cite}** · {docs[u.doc_id].name} · {u.location} · *{u.part}*")
                if paras:
                    with st.expander(f"Draft text citing this page ({len(paras)} paragraph(s))"):
                        for p in paras:
                            st.markdown(f"> {p[:600]}")
                t_text, t_img = st.tabs(["Text (numbers used in the draft in bold)", "Page image"])
                with t_text:
                    with st.container(height=560):
                        st.markdown(highlight(u.text, nums).replace("\n", "  \n") or "(no text)")
                with t_img:
                    show_image(render_unit(docs[u.doc_id].path, u.pdf_page, u.side, resolution=110))

    with tab_comp:
        ext = {k: v for k, v in store.load_extractions(deal_id).items() if k[1] == section}
        if not ext:
            st.info("No comparable sections extracted for this section.")
        else:
            keys = sorted(ext)
            ck = st.selectbox("Comparable", keys, format_func=lambda k: f"{k[0]} · {ext[k].doc_name}", key=f"cc_{key}")
            sec = ext[ck]
            # subheading closest to this subsection's title
            heads = sec.subheadings
            if heads:
                scores = [fuzz.token_set_ratio(new_title, h.text) for h in heads]
                best = max(range(len(heads)), key=lambda i: scores[i])
                opts = list(range(len(heads)))
                hi = st.selectbox("Jump to heading", opts, index=best,
                                  format_func=lambda i: f"p.{heads[i].page_no} · {heads[i].text[:90]}"
                                                        + (" ← closest" if i == best else ""), key=f"ch_{key}_{ck}")
                start_page = heads[hi].page_no
            else:
                start_page = sec.start_page
            q = st.text_input("Search this section", key=f"cq_{key}", placeholder="word or phrase")
            pages = {p.page_no: p for p in sec.pages}
            if q:
                hits = [p for p in sec.pages if q.lower() in " ".join(p.lines).lower()]
                st.caption(f"{len(hits)} page(s) contain '{q}'")
                if hits:
                    start_page = st.selectbox("Matching pages", [p.page_no for p in hits], key=f"cqp_{key}")
            show = st.radio("Show", ["Text", "Page image"], horizontal=True, key=f"cs_{key}",
                            label_visibility="collapsed")
            pn = st.number_input("PDF page", sec.start_page, sec.end_page, int(start_page), key=f"cp_{key}_{ck}_{start_page}")
            st.caption(f"[{sec.doc_id} p.{pn}] · style reference only — never a source of facts")
            with st.container(height=560):
                if show == "Text":
                    p = pages.get(pn)
                    text = "\n".join(p.lines) if p else ""
                    if q:
                        text = re.sub(re.escape(q), lambda m: f"**{m.group(0)}**", text, flags=re.I)
                    st.markdown(text.replace("\n", "  \n") or "(no text)")
                else:
                    show_image(render_page(docs[sec.doc_id].path, int(pn), 100))

    with tab_chk:
        if changed:
            st.caption("Checks below reflect your unsaved text.")
        if not iss:
            st.success("No issues found.")
        for x in sorted(iss, key=lambda x: {"high": 0, "medium": 1, "low": 2}[x.severity]):
            with st.container(border=True):
                st.markdown(f"{SEV[x.severity]} **{x.kind}** · {x.message}")
                if x.excerpt:
                    st.caption(x.excerpt.replace("\n", "  \n"))
        with st.expander("What is checked"):
            st.markdown(
                "- **number**: every amount / percentage must appear on a page cited in the same paragraph "
                "(or table + its *Source* line), or be one of the verified figures / ratios\n"
                "- **citation**: numbers without a citation, citations to unknown pages, comparable pages cited\n"
                "- **copy**: sentences ≥85% similar to a comparable prospectus sentence\n"
                "- **rf_rule** (Risk Factors): mitigation language, heading is one full sentence stating the adverse effect\n"
                "- **completeness**: [TBD] placeholders, empty text, required risks not covered")

if draft.gaps:
    with st.expander(f"Gaps to obtain from the company ({len(draft.gaps)})"):
        st.markdown("\n".join(f"- {g}" for g in draft.gaps))
