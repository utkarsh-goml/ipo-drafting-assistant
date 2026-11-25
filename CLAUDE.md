# CLAUDE.md – IPO Drafting Assistant (PoC)

Context for Claude Code. Read this first, then `PLAN.md` (design) and `README.md` (run/install). `USER_GUIDE.md` is the plain-language user guide.

## What this is
A Streamlit proof of concept for an investment-bank ECM team (Citi, internal). It drafts the **"Our Business"** and **"Risk Factors"** sections of an Indian IPO DRHP.
- **Facts** come only from the company's own documents: annual report, financials, presentations (IDs `D1, D2, …`).
- **Structure and style** come from comparable DRHPs/RHPs (IDs `C1, C2, …`). Python extracts their sections.
- **Writing** is done by the bank's GenAI tool **Stylus Workspace**:
  - a chat UI with fixed shared *presets*;
  - **no API key, no programmatic access**.
- So the tool builds **packs** (PDFs) that the banker uploads to Stylus, then reads the reply file back in.
- Every fact in a draft must carry a page citation like `[D1 p.145]` (printed page, not PDF page).

## Hard constraints (do not break)
- **Stylus limits per run:**
  - 100 pages and **10 MB**, across all files of the run. 10 MB is treated as combined; the user never confirmed whether it is per file.
  - The packer budget is **≤ 90 pages and ≤ 9 MB** (`PAGE_BUDGET`, `SIZE_BUDGET_MB` in `core/tasks.py`).
  - Order of measures when a pack is too big:
    1. Trim pages by priority.
    2. Downsample images in steps (`IMAGE_LEVELS`).
    3. Drop the lowest-priority source pages, and list them in the Task Sheet.
  - Text is never altered.
- **Office VDI runs Python 3.8/3.9 with pandas 2.0.3.**
  - Keep code 3.8-compatible: `typing.List/Dict/Optional/Tuple/Set/Union`, not `list[str]` or `X | None`. Pydantic evaluates annotations at runtime.
  - reportlab must stay `<4.3` on 3.8.
  - Streamlit 1.40 on the VDI: use `core.ui.show_image` (handles `use_container_width` vs `use_column_width`).
  - pypdf 5.9 vs 7.x: call `writer.compress_identical_objects()` with no kwargs.
- **The app needs no internet while running.** Use no external APIs. Everything is offline Python.
- **Comparable pages are STYLE ONLY.** Never let facts from `C…` documents into drafts; the validator flags them.

## Section extraction rule (from the user)
- Named DRHP sections are **always in the table of contents** and **always start with a bold, capital-letter heading**.
- Extraction order: TOC first (with page offset), then PDF bookmarks, then heading scan (the last two only as fallbacks, with a warning).
- Start and end are verified against bold capital lines (`locate_bold_heading`, `pdf_reader.bold_caps_lines`).
- Flags `start_verified` / `end_verified` lower confidence when not confirmed.
- Known-good results:
  - C1 `1521183392638.pdf`: OB PDF p166–190, RF p21–54.
  - C2 ICICI Prudential AMC DRHP `1752842148576.pdf`: OB p160–184, RF p35–65.

## Workflow (app pages)
1. **Deal Setup:** create a deal, upload company PDFs (D…) and comparables (C…).
2. **Section Extraction:** find OB/RF in comparables; banker corrects ranges.
3. **Company Context:**
   - printed-page units (spreads split);
   - part tags;
   - financials (consolidated preferred);
   - risk triggers with evidence;
   - BM25 page mapping per subsection/theme.
   - All editable.
4. **LLM Tasks:**
   - blueprint (default, or from IPO-01);
   - build IPO-01/02/03 packs (zip);
   - upload the Stylus reply, matched by `TASK_ID`.
5. **Section Workspace:**
   - edit per subsection, with version history;
   - References / Comparables / Checks tabs;
   - IPO-04 revise-one-subsection pack;
   - export Word/Markdown/CSV.

## Packs and presets
- Each pack contains:
  - `00_TASK_SHEET_<id>.pdf`: instructions, blueprint, verified figures.
  - `01_SOURCE_<id>.pdf`: company pages stamped `[D1 p.N]`, Helvetica-Bold, not embedded.
  - `02_STYLE_REF_<id>.pdf`: comparable pages stamped STYLE ONLY.
- Presets live in `core/prompts/presets/` and are registered once in Stylus. Names carry versions (`core/tasks.py` `PRESETS`).
  - **IPO-01 Style & Blueprint v1.1** returns JSON. Its 3 files include 01_SOURCE overview pages.
  - **IPO-02 / IPO-03 v1.0** return a `TASK_ID:` line, then `### <ID> [Group] <Title>` items, then `### GAPS`.
  - **IPO-04 v1.0** revises one subsection; the reply replaces only that item.
- If you change a preset's text, bump its version in `PRESETS`. The user must update Stylus manually.

## Reply reading (`core/replies.py`)
- `read_reply` handles .docx/.md/.txt/.pdf. `find_task_id` tolerates `**TASK\_ID**:`.
- `parse_blueprint_json` is deliberately tolerant. It handles:
  - curly quotes, whether they appear only in prose or as the JSON's own quotes (Word AutoFormat), via `_curly_to_straight`;
  - unescaped inner quotes;
  - NBSP indents;
  - PDF line wraps;
  - `//` comments and trailing commas;
  - wrapper keys or Title Case keys;
  - `~~~` fences;
  - no fence at all.
- `_escape_stray_quotes` is a last resort only. It once produced valid-but-wrong JSON silently.
- `blueprint._int_or_none` reads "800-1000" as 900. `attach_map_keys` matches by title, then by keywords; Offer-group risks stay unmapped.
- Regression suite: `tests/test_ipo01_reply.py`, 13 output variants built from `tests/fixtures/ipo01_sample_reply.md`.

## Code map
- `app.py`: home page. `pages/1_…5_*.py`: the five steps.
- `core/`:
  - `pdf_reader.py`: text cache, bold lines, page images.
  - `section_extractor.py`: TOC, bold-heading verification, outline.
  - `company_docs.py`: printed-page units.
  - `financials.py`, `risk_triggers.py`, `retrieval.py` (BM25).
  - `context.py`: runs Phase 2 per deal.
  - `blueprint.py`, `packer.py` (page selection, stamping, size budget, builders), `tasks.py`, `replies.py`, `validator.py`, `workspace.py`, `store.py`.
- `config/sections.yaml` (heading variants, thresholds). `config/company_docs.yaml` (subsection and risk-theme terms, sector packs such as asset_management).
- `data/` holds per-deal storage and a cache. It is git-ignored; never commit it.

## Commands
```bash
pip install -r requirements.txt
streamlit run app.py              # or: python -m streamlit run app.py --server.port 8501 --server.headless true
python -m pytest tests -q         # 110 passed, 2 skipped (Nov 2026), on 3.10 and 3.8
```
- Real-document tests use env vars:
  - `IPO_SAMPLE_DRHP`: ICICI Pru AMC DRHP.
  - `IPO_SAMPLE_AR`: SBI Funds Management AR 2024-25, 206 printed pages, consolidated PAT ₹2,54,015.32 lakhs.
- `.claude/launch.json` has the `ipo-draft` preview config (port 8501).
- A Python 3.8 venv is useful for compatibility checks. One can be built with uv's cpython 3.8 plus `python -m venv`.

## Status (as of 2026-11-20)
- Phases 1–4 built and tested with mock replies.
- **Not yet run against live Stylus.** Phase 5 is in PLAN.md §8.
- Target-state idea, shown in the decks but not built: an agent orchestrator calling Stylus through an API (parallel drafting, check-and-fix loop, two banker checkpoints). It depends on Stylus API access, which is unconfirmed.
- Deliverables in this folder:
  - `USER_GUIDE.md` / `.docx`;
  - `IPO Drafting Assistant.pptx` (management deck);
  - `IPO Drafting Assistant – Hackathon.pptx` (hackathon deck, with the appendix of samples).
- The online versions of the decks belong to the original Claude account and won't open from another account. Use the .pptx files.
- `data/deals/e2e-ipo-01-test` is an end-to-end test deal (SBI AR plus two comparables). Delete it if not needed.
- Git: `IPO_DRAFT` is its own repository (created 2026-11-25, branch `langgraph`; first commits 2026-11-25; no remote yet). The Desktop
  folder above it also has an empty git repository; never run git from the Desktop or add files outside `IPO_DRAFT`.
  `.gitignore` keeps out deal data, `.env`, venvs, PDFs, decks, Word files, zips and Stylus reply samples. Commit only when the user asks.

## Working preferences
- The user is a banker, not a developer. Explain in plain language, and keep docs and slides simple.
- Ask before big design changes. Show results with real numbers from the test documents.
- Never invent statistics for slides. Use `[__]` placeholders and say so.
- Keep personal details out of files and out of anything sent to external services.
