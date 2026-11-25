"""Live run against the OpenAI API, for testing on PUBLIC documents only (build step 6).

    # first run: create the deal, run with the models in .env
    python -m agent.live_run run --public-documents --company AR.pdf --comparable DRHP.pdf --run-id luna-1 \
        --approve-facts --exclude-unmapped --proceed-low-relevance
    # same deal, Sol for drafting and fixing
    python -m agent.live_run run --public-documents --deal <deal-id> --run-id sol-1 --draft-model <sol model id> \
        --approve-facts --exclude-unmapped --proceed-low-relevance
    # side-by-side table
    python -m agent.live_run compare --deal <deal-id> luna-1 sol-1

What this sends to OpenAI: the selected pages of the company documents, and the Our Business / Risk Factors
pages of the comparables (as style examples). `--public-documents` is required to confirm they are public.

Banker pauses are answered only as the flags say; any other pause stops the script and shows what is
waiting. The run is saved in the deal folder, so it can be continued from the app later.
  --approve-facts          approve checkpoint 1 as read (no edits). Refused by the graph while blockers exist.
  --exclude-unmapped       leave out Our Business subsections that have no pages mapped.
  --proceed-low-relevance  draft items whose pages scored below RELEVANCE_MIN as they are.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from agent import graph as G
from agent import report as R
from agent.config import ConfigError, load_settings
from agent.deps import AgentDeps
from agent.telemetry import setup_telemetry
from core import store
from core.packer import fmt_inr


class Stop(Exception):
    """A pause the flags don't answer."""


def _reply(pending: dict, args) -> dict:
    kind = pending["kind"]
    if kind == "checkpoint_facts":
        figs = {f["key"]: f for f in pending["figures"]}
        pat = figs.get("pat")
        print(f"  Checkpoint 1: basis {pending['basis']}, unit {pending['unit']}, {len(pending['figures'])} figures, "
              f"{len(pending['triggers'])} risk triggers"
              + (f"; PAT {fmt_inr(pat['values'][0])} [{pat['cite']}]" if pat else ""))
        if pending.get("blockers"):
            raise Stop("checkpoint 1 has blockers: " + " ".join(pending["blockers"]))
        if not args.approve_facts:
            raise Stop("checkpoint 1 is waiting (use --approve-facts to approve as read)")
        return {"approved": True}
    if kind == "choose_pages":
        exclude, proceed, other = [], [], []
        for it in pending["items"]:
            print(f"  Pages needed: {it['item_id']} {it['title']} - {it['reason']}")
            if it["reason"] == "no pages selected" and it["item_id"].startswith("OB") and args.exclude_unmapped:
                exclude.append(it["item_id"])
            elif it["reason"].startswith("low relevance") and args.proceed_low_relevance:
                proceed.append(it["item_id"])
            else:
                other.append(it["item_id"])
        if other:
            raise Stop("pages are needed for " + ", ".join(other))
        return {"exclude_subsections": exclude, "proceed_anyway": proceed}
    raise Stop(f"the run is waiting for '{kind}'")


def cmd_run(args) -> int:
    if not args.public_documents:
        print("Refusing: add --public-documents to confirm every document of this deal is public.", file=sys.stderr)
        return 2
    overrides = {"openai_model_draft": args.draft_model} if args.draft_model else {}
    try:
        settings = load_settings(**overrides)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    tel = setup_telemetry(settings)
    deps = AgentDeps.from_settings(settings, tel)
    add: List[dict] = []
    if args.deal:
        deal_id = args.deal
        if args.company or args.comparable:
            print("Use --deal alone (its documents are already stored), or --company/--comparable for a new deal.",
                  file=sys.stderr)
            return 2
    else:
        if not (args.company and args.comparable):
            print("A new deal needs --company and --comparable.", file=sys.stderr)
            return 2
        deal_id = store.create_deal(args.deal_name, args.sector, args.fy)
        add = [{"path": p, "kind": "company", "doc_type": "ANNUAL_REPORT"} for p in args.company]
        add += [{"path": p, "kind": "comparable"} for p in args.comparable]
        print(f"Created deal {deal_id}")
    print(f"Run {args.run_id}: blueprint {settings.openai_model_blueprint}, drafting {settings.openai_model_draft}, "
          f"judge {settings.openai_model_eval}")
    t0 = time.perf_counter()
    code = 0
    try:
        with G.deal_graph(deal_id, deps) as g:
            snap = G.start(g, deal_id, args.run_id, settings, tel, add)
            seen = 0
            while True:
                for line in snap["log"][seen:]:
                    print("  " + line)
                seen = len(snap["log"])
                if not snap["pending"]:
                    break
                if snap["pending"]["kind"] == "checkpoint_review":
                    print("Draft ready; the run is waiting at checkpoint 2 (review in the app, then approve to export).")
                    break
                try:
                    reply = _reply(snap["pending"], args)
                except Stop as e:
                    print(f"Stopped: {e}. The run is saved; answer the pause in the app.")
                    return 3
                snap = G.resume(g, args.run_id, reply, settings, tel)
        wall = time.perf_counter() - t0
        rep = R.run_report(deal_id, args.run_id, snap, settings, wall)
        path = R.save(rep)
        print()
        print(R.to_markdown(rep))
        print(f"Report: {path}")
        code = 0 if snap["status"] == "drafted" or snap["waiting_for"] == "checkpoint_review" else 1
    finally:
        tel.force_flush()
        tel.shutdown()
    return code


def cmd_compare(args) -> int:
    reps = [R.load(args.deal, r) for r in args.runs]
    table = R.compare(reps)
    print(table)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(table)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.live_run", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the agent live on a public test deal")
    r.add_argument("--public-documents", action="store_true", help="confirm all documents are public")
    r.add_argument("--deal", help="existing deal id (reuse its documents)")
    r.add_argument("--company", action="append", help="company document (annual report); repeatable")
    r.add_argument("--comparable", action="append", help="comparable DRHP/RHP; repeatable")
    r.add_argument("--deal-name", default="Public test deal")
    r.add_argument("--sector", default="Asset Management")
    r.add_argument("--fy", default="FY2025")
    r.add_argument("--run-id", required=True)
    r.add_argument("--draft-model", help="override OPENAI_MODEL_DRAFT for this run (drafting and fixing)")
    r.add_argument("--approve-facts", action="store_true")
    r.add_argument("--exclude-unmapped", action="store_true")
    r.add_argument("--proceed-low-relevance", action="store_true")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("compare", help="side-by-side table of finished runs")
    c.add_argument("--deal", required=True)
    c.add_argument("runs", nargs="+")
    c.add_argument("--out", help="also write the table to this file")
    c.set_defaults(fn=cmd_compare)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
