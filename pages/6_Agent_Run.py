"""6 · Agent Run: the LangGraph agent with OpenAI drafts both sections and stops for the banker.

Needs Python 3.11 and requirements-agent.txt. Pages 1-5 (Stylus workflow) don't depend on this page.
"""
import sys

import pandas as pd
import streamlit as st

from core import store
from core.ui import deal_sidebar, page_header, require_access

st.set_page_config(page_title="Agent Run", page_icon="🧭", layout="wide")
require_access()
page_header("6 · Agent Run",
            "The agent drafts Our Business and Risk Factors with OpenAI. It stops for you twice: to approve the "
            "facts (checkpoint 1) and to review the draft (checkpoint 2). Nothing is exported until you approve.")

if sys.version_info < (3, 11):
    st.error("This page needs Python 3.11 and requirements-agent.txt. Pages 1-5 work as before.")
    st.stop()
try:
    from pydantic import ValidationError

    from agent import app_runtime as RT
    from agent import backup as B
    from agent import graph as G
    from agent import report as R
    from agent import tools
    from agent.config import ConfigError
except ImportError as e:  # pragma: no cover - depends on the install
    st.error(f"This page needs requirements-agent.txt installed ({e}).")
    st.stop()

# ----------------------------------------------------------------- settings and access
try:
    deps = RT.runtime()
except ConfigError as e:
    st.error("The agent's settings are incomplete, so no run can start.")
    st.code(str(e), language=None)
    st.stop()
settings = deps.settings

if settings.deployment_environment == "render":
    st.warning("Free demo: deals are deleted when the app sleeps. Download a deal backup before you leave, "
               "and restore it when you come back.")

STEP_LABEL = {
    "ingest_documents": "Documents checked", "extract_comparable": "Comparable sections found",
    "check_extraction": "Extraction checked", "check_style_coverage": "Style examples ready",
    "build_company_context": "Company pages, figures and risk triggers read",
    "facts_approved": "Facts approved", "generate_blueprint": "Blueprint written by the model",
    "plan_work": "Work planned", "relevance_gate": "Pages checked for relevance",
    "draft_part": "Drafted", "check_part": "Checked", "evaluate_part": "Scored against cited pages",
    "fix_part": "Fixed", "finish_part": "Part finished", "assemble_draft": "Draft assembled",
    "drafted": "Draft ready", "apply_review": "Your changes applied and re-checked", "export": "Exported",
}
ICON = {"PASSED": "✅", "FAILED": "❌", None: "⚪"}


def gen() -> int:
    """Form generation: bumped after every action, so the forms show the new state, not what was typed before."""
    return st.session_state.get("agent_gen", 0)


def flash(kind: str, msg: str) -> None:
    st.session_state["agent_flash"] = (kind, msg)


def _errors(e: ValidationError) -> str:
    return "; ".join(err["msg"].replace("Value error, ", "") for err in e.errors())


def act(label: str, fn) -> None:
    """Run one agent action with live progress, then redraw the page."""
    with st.status(label, expanded=True) as box:
        def on_step(node: str) -> None:
            box.write(f"✓ {STEP_LABEL.get(node, node)}")
        try:
            snap = fn(on_step)
        except ValidationError as e:
            box.update(label="Not sent: the reply is incomplete", state="error")
            flash("error", "Not sent: " + _errors(e))
        except (ValueError, tools.ToolError) as e:
            box.update(label="Not done", state="error")
            flash("error", str(e))
        else:
            box.update(label="Done", state="complete")
            if snap.get("error"):
                flash("error", snap["error"])
    st.session_state["agent_gen"] = gen() + 1
    st.rerun()


# ----------------------------------------------------------------- sidebar: deal, backup, restore
deal_id = deal_sidebar()
with st.sidebar:
    st.markdown("### Backup")
    if deal_id:
        if st.button("Prepare deal backup", use_container_width=True):
            st.session_state["agent_backup"] = (deal_id, B.backup_deal(deal_id))
        bk = st.session_state.get("agent_backup")
        if bk and bk[0] == deal_id:
            st.download_button(f"Download backup ({len(bk[1]) / 2 ** 20:.1f} MB)", bk[1],
                               file_name=f"{deal_id}-backup.zip", mime="application/zip", use_container_width=True)
    up = st.file_uploader("Restore a deal backup (.zip)", type=["zip"], key="agent_restore_file")
    replace = st.checkbox("Replace the deal if it already exists", key="agent_restore_replace")
    if up is not None and st.button("Restore deal", use_container_width=True):
        try:
            restored = B.restore_deal(up.getvalue(), replace=replace)
        except B.BackupError as e:
            st.error(str(e))
        else:
            st.session_state["deal_id"] = restored
            st.session_state.pop("agent_run", None)
            flash("success", f"Deal {restored} restored. Its runs are listed below.")
            st.rerun()

f = st.session_state.pop("agent_flash", None)
if f:
    getattr(st, f[0])(f[1])

if not deal_id:
    st.info("Create a deal and upload its documents on **1 · Deal Setup**, or restore a deal backup (sidebar).")
    st.stop()

docs = tools.list_documents(deal_id)["documents"]
st.caption("Documents: " + (", ".join(f"{d['doc_id']} {d['name']}" for d in docs) or "none yet"))
if not any(d["kind"] == "company" for d in docs) or not any(d["kind"] == "comparable" for d in docs):
    st.info("The deal needs at least one company document and one comparable DRHP/RHP (upload on **1 · Deal Setup**).")
    st.stop()


# ----------------------------------------------------------------- forms for each pause
def form_review_extraction(g, run_id, p):
    st.subheader("Check the comparable sections")
    st.caption("These sections were not found cleanly through the table of contents. Decide each one.")
    with st.form("f_review_extraction"):
        decisions = []
        for i, s in enumerate(p["sections"]):
            pages = s.get("pdf_pages")
            st.markdown(f"**{s['doc_id']} {s.get('title', s['section'])}** · "
                        + (f"PDF p{pages[0]}-{pages[1]} · " if pages else "") + s["reason"])
            options = (["confirm"] if s.get("found") else []) + ["set_range", "drop"]
            a, b, c = st.columns([2, 1, 1])
            action = a.radio("Decision", options, key=f"rx{gen()}_a{i}", horizontal=True,
                             format_func={"confirm": "Use these pages", "set_range": "Set PDF pages",
                                          "drop": "Don't use"}.get)
            start = b.number_input("Start page", min_value=1, value=(pages or [1, 1])[0], key=f"rx{gen()}_s{i}")
            end = c.number_input("End page", min_value=1, value=(pages or [1, 1])[1], key=f"rx{gen()}_e{i}")
            d = {"doc_id": s["doc_id"], "section": s["section"], "action": action}
            if action == "set_range":
                d.update(start_page=int(start), end_page=int(end))
            decisions.append(d)
        if st.form_submit_button("Send decisions", type="primary"):
            act("Applying your decisions", lambda cb: G.resume(g, run_id, {"decisions": decisions}, settings,
                                                               deps.tel, on_step=cb))


def _page_choices(groups: dict, prefix: str):
    """Multiselects for page groups; returns PageChoice dicts for the changes."""
    changes = []
    for gk, v in groups.items():
        kind, key = v.get("kind"), v.get("key")
        if kind is None:
            kind, key = gk.split(":", 1) if ":" in gk else (None, gk)
        cands = [c["cite"] for c in v["candidates"]]
        scores = {c["cite"]: c["score"] for c in v["candidates"]}
        chosen = st.multiselect(f"{v.get('title', key)} - pages to use", cands, default=v["selected"],
                                key=f"{prefix}{gk}", format_func=lambda c: f"{c} (match {scores.get(c, 0):.2f})")
        extra = st.text_input("Add other pages (e.g. D1 p.45, D1 p.46)", key=f"{prefix}x{gk}")
        for c in cands:
            if (c in chosen) != (c in v["selected"]):
                changes.append({"kind": kind, "key": key, "cite": c, "selected": c in chosen})
        for c in [x.strip() for x in extra.split(",") if x.strip()]:
            changes.append({"kind": kind, "key": key, "cite": c, "selected": True})
    return changes


def form_checkpoint_facts(g, run_id, p):
    st.subheader("Checkpoint 1 · Approve the facts")
    st.caption(f"Figures: {p['basis'] or 'basis unknown'}, in {p['unit'] or 'unit unknown'} "
               f"({', '.join(p['years'] or [])}). Every figure shows the page it was read from.")
    for b in p["blockers"]:
        st.warning(b)
    years = p["years"] or ["Current", "Previous"]
    fig = pd.DataFrame([{"key": x["key"], "Figure": x["label"],
                         "Current": x["values"][0] if x["values"] else None,
                         "Previous": x["values"][1] if len(x["values"]) > 1 else None,
                         "Page": x["cite"]} for x in p["figures"]])
    trig = pd.DataFrame([{"key": t["key"], "Use": t["selected"], "Risk": t["title"], "Group": t["category"],
                          "Required": t["required"], "Why": t.get("reason", "")} for t in p["triggers"]])
    with st.form("f_facts"):
        st.markdown("**Financial figures** (edit Current / Previous if a number is wrong)")
        fig_ed = st.data_editor(fig, hide_index=True, use_container_width=True, disabled=["key", "Figure", "Page"],
                                column_config={"key": None, "Current": st.column_config.NumberColumn(years[0]),
                                               "Previous": st.column_config.NumberColumn(
                                                   years[1] if len(years) > 1 else "Previous")},
                                key=f"facts_fig{gen()}")
        st.markdown("**Risk triggers** (untick a risk the documents don't support)")
        trig_ed = st.data_editor(trig, hide_index=True, use_container_width=True,
                                 disabled=["key", "Risk", "Group", "Required", "Why"], column_config={"key": None},
                                 key=f"facts_trig{gen()}")
        st.markdown("**Pages per subsection and risk theme**")
        groups = {f"{kind}:{key}": {**v, "kind": kind, "key": key}
                  for kind, d in p["pages"].items() for key, v in d.items()}
        page_changes = _page_choices(groups, f"fp{gen()}_")
        a, b = st.columns(2)
        save = a.form_submit_button("Save changes")
        approve = b.form_submit_button("Approve facts", type="primary")
    if not (save or approve):
        return
    edits = []
    for (_, old), (_, new) in zip(fig.iterrows(), fig_ed.iterrows()):
        if (pd.isna(old["Current"]) != pd.isna(new["Current"]) or pd.isna(old["Previous"]) != pd.isna(new["Previous"])
                or (not pd.isna(new["Current"]) and old["Current"] != new["Current"])
                or (not pd.isna(new["Previous"]) and old["Previous"] != new["Previous"])):
            edits.append({"key": new["key"], "label": new["Figure"],
                          "current": None if pd.isna(new["Current"]) else float(new["Current"]),
                          "previous": None if pd.isna(new["Previous"]) else float(new["Previous"])})
    tsel = {new["key"]: bool(new["Use"]) for (_, old), (_, new) in zip(trig.iterrows(), trig_ed.iterrows())
            if bool(old["Use"]) != bool(new["Use"])}
    changed = bool(edits or tsel or page_changes)
    if approve and changed:
        flash("error", "You changed something: press **Save changes** first, look at the result, then approve.")
        st.rerun()
    if save and not changed:
        flash("info", "Nothing changed.")
        st.rerun()
    reply = {"approved": True} if approve else {"approved": False, "figure_edits": edits,
                                                "trigger_selection": tsel, "page_selection": page_changes}
    act("Approving the facts and drafting" if approve else "Saving your changes",
        lambda cb: G.resume(g, run_id, reply, settings, deps.tel, on_step=cb))


def form_choose_pages(g, run_id, p):
    st.subheader("Pages needed")
    st.caption("These items have no pages, or pages that don't look relevant to what they must cover.")
    with st.form("f_choose"):
        changes, proceed, exclude = [], [], []
        for it in p["items"]:
            st.markdown(f"**{it['item_id']} {it['title']}** · {it['reason']}")
            changes += _page_choices(it["pages"], f"cp{gen()}_{it['item_id']}_")
            if not it["pages"]:
                st.caption("This item has no page list to choose from. Leave it out, or start a new run and pick "
                           "pages for its risk themes at checkpoint 1.")
            if it["reason"].startswith("low relevance") and st.checkbox("Draft it anyway with these pages",
                                                                        key=f"cp{gen()}_go_{it['item_id']}"):
                proceed.append(it["item_id"])
            if it["item_id"].startswith("OB") and st.checkbox("Leave this subsection out",
                                                              key=f"cp{gen()}_out_{it['item_id']}"):
                exclude.append(it["item_id"])
        if st.form_submit_button("Send", type="primary"):
            reply = {"page_selection": changes, "proceed_anyway": proceed, "exclude_subsections": exclude}
            act("Applying and drafting", lambda cb: G.resume(g, run_id, reply, settings, deps.tel, on_step=cb))


def form_checkpoint_review(g, run_id, p):
    st.subheader("Checkpoint 2 · Review the draft")
    if p["blockers"]:
        st.warning("Export is blocked until these are resolved:\n\n" + "\n".join(f"- {b}" for b in p["blockers"]))
    else:
        st.success("Nothing blocks export. Review the text, then approve.")
    names = {"our_business": "Our Business", "risk_factors": "Risk Factors"}
    with st.form("f_review"):
        edit, revise, restore, recheck = [], [], [], []
        tabs = st.tabs([names[s] for s in p["sections"]])
        for tab, (sec, data) in zip(tabs, p["sections"].items()):
            with tab:
                for it in data["items"]:
                    k = f"rv{gen()}_{sec}_{it['item_id']}"
                    f_ = it["faithfulness"]
                    head = (f"{ICON.get(it['status'], '⚪')} {it['item_id']} "
                            + (f"[{it['group']}] " if it["group"] else "") + it["title"]
                            + (f" · faithfulness {f_:.2f}" if f_ is not None else ""))
                    with st.expander(head, expanded=it["status"] == "FAILED"):
                        for i in it["issues"]:
                            (st.error if i["severity"] == "high" else st.caption)(f"{i['severity']}: {i['message']}")
                        for u in it["unsupported"]:
                            st.caption(f"Not supported by the cited page: {u}")
                        st.caption(f"Source: {it['source']} · {it['versions']} earlier version(s)")
                        text = st.text_area("Text", it["text"], height=220, key=f"{k}_t")
                        if text.strip() != it["text"].strip():
                            edit.append({"section": sec, "item_id": it["item_id"], "text": text})
                        ins = st.text_input("Ask the model to revise (instructions)", key=f"{k}_r")
                        if ins.strip():
                            revise.append({"section": sec, "item_id": it["item_id"], "instructions": ins.strip()})
                        a, b = st.columns(2)
                        if it["versions"]:
                            v = a.selectbox("Restore an earlier version", [None] + list(range(it["versions"])),
                                            key=f"{k}_v", format_func=lambda x: "-" if x is None else f"version {x}")
                            if v is not None:
                                restore.append({"section": sec, "item_id": it["item_id"], "index": v})
                        if b.checkbox("Check and score again", key=f"{k}_c"):
                            recheck.append({"section": sec, "item_id": it["item_id"]})
                if data["gaps"]:
                    st.markdown("**Gaps to obtain from the company**")
                    st.markdown("\n".join(f"- {x}" for x in data["gaps"]))
        leave_out, add_risk, not_required = [], [], []
        for m in p["missing_ob"]:
            st.markdown(f"**Missing subsection {m['item_id']} {m['title']}**")
            t = st.text_area("Write it here (cite pages as [D1 p.N])", key=f"mo{gen()}_{m['item_id']}_t")
            if t.strip():
                edit.append({"section": "our_business", "item_id": m["item_id"], "text": t, "title": m["title"]})
            if st.checkbox("Leave this subsection out", key=f"mo{gen()}_{m['item_id']}_x"):
                leave_out.append(m["item_id"])
        for m in p["missing_required"]:
            st.markdown(f"**Required risk not covered: {m['title']}**")
            a, b = st.columns([1, 3])
            grp = a.selectbox("Group", ["Internal", "External", "Offer"], key=f"mr{gen()}_{m['key']}_g")
            title = b.text_input("Risk heading", key=f"mr{gen()}_{m['key']}_h")
            t = st.text_area("Risk text (cite pages as [D1 p.N])", key=f"mr{gen()}_{m['key']}_t")
            if title.strip() and t.strip():
                add_risk.append({"group": grp, "title": title, "text": t})
            why = st.text_input("Or: why it does not apply (kept in the record)", key=f"mr{gen()}_{m['key']}_n")
            if why.strip():
                not_required.append({"key": m["key"], "reason": why.strip()})
        with st.expander("Add another risk"):
            a, b = st.columns([1, 3])
            grp = a.selectbox("Group", ["Internal", "External", "Offer"], key=f"ar{gen()}_g")
            title = b.text_input("Risk heading", key=f"ar{gen()}_h")
            t = st.text_area("Risk text", key=f"ar{gen()}_t")
            if title.strip() and t.strip():
                add_risk.append({"group": grp, "title": title, "text": t})
        a, b = st.columns(2)
        apply_ = a.form_submit_button("Apply changes")
        approve = b.form_submit_button("Approve and export", type="primary")
    changes = {"edit": edit, "revise": revise, "restore": restore, "recheck": recheck, "add_risk": add_risk,
               "leave_out": leave_out, "not_required": not_required}
    changed = any(changes.values())
    if approve and changed:
        flash("error", "You made changes: press **Apply changes** first, look at the result, then approve.")
        st.rerun()
    if apply_ and not changed:
        flash("info", "Nothing changed.")
        st.rerun()
    if apply_ or approve:
        reply = {"approve": True} if approve else changes
        act("Exporting" if approve else "Applying and re-checking your changes",
            lambda cb: G.resume(g, run_id, reply, settings, deps.tel, on_step=cb))


FORMS = {"review_extraction": form_review_extraction, "checkpoint_facts": form_checkpoint_facts,
         "choose_pages": form_choose_pages, "checkpoint_review": form_checkpoint_review}


# ----------------------------------------------------------------- the run
with G.deal_graph(deal_id, deps) as g:
    runs = G.list_runs(g)
    c1, c2 = st.columns([3, 1])
    if c2.button("Start a new run", type="primary" if not runs else "secondary", use_container_width=True):
        from datetime import datetime
        new_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        st.session_state["agent_run"] = new_id
        act("Agent working", lambda cb: G.start(g, deal_id, new_id, settings, deps.tel, on_step=cb))
    if not runs:
        c1.info("No run yet for this deal.")
        st.stop()
    current = st.session_state.get("agent_run")
    run_id = c1.selectbox("Run", runs, index=runs.index(current) if current in runs else 0)
    st.session_state["agent_run"] = run_id
    snap = G.snapshot(g, run_id, settings)

    status = snap["status"]
    if status == "error":
        st.error(f"The run stopped: {snap['error']}. Fix the cause, then start a new run.")
    elif status == "done":
        st.success("Approved and exported.")
    elif snap["can_continue"]:
        st.info("This run was cut off between steps (for example the page was closed). It can carry on from the "
                "last saved step.")
        if st.button("Continue run", type="primary"):
            act("Agent working", lambda cb: G.continue_run(g, run_id, settings, deps.tel, on_step=cb))
    if snap["pending"]:
        if snap["pending"].get("notice"):
            st.warning(snap["pending"]["notice"])
        FORMS[snap["pending"]["kind"]](g, run_id, snap["pending"])

    if snap["exports"]:
        st.subheader("Downloads")
        cols = st.columns(len(snap["exports"]))
        for col, (name, path) in zip(cols, sorted(snap["exports"].items())):
            fp = store.deal_dir(deal_id) / path
            if fp.exists():
                col.download_button(name, fp.read_bytes(), file_name=name, key=f"dl_{name}")
            else:
                col.caption(f"{name}: file not on this server any more (restore a backup taken after export)")

    with st.expander("Run log"):
        st.text("\n".join(snap["log"]) or "-")
    if snap["parts"]:
        with st.expander("Calls, tokens and cost"):
            st.markdown(R.to_markdown(R.run_report(deal_id, run_id, snap, settings, None)))
