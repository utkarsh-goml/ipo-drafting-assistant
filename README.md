# IPO Drafting Assistant (PoC)

A Streamlit tool that drafts the **Our Business** and **Risk Factors** sections of an Indian DRHP. Facts come from company documents, and structure and style come from comparable DRHPs/RHPs. The full design is in [PLAN.md](PLAN.md). New users should start with [USER_GUIDE.md](USER_GUIDE.md), which walks through the whole process step by step.

## Run

```bash
pip install -r requirements.txt
```

```bash
streamlit run app.py
```

## Run on the office VDI (Python 3.8 or newer)
Tested on Python 3.8.20 and 3.10.5: 80 tests pass on both, including the two real documents. The app needs no internet access and no API key while it runs.

1. Copy the project folder to the VDI **without** `data/` (the app creates it).
2. Install the packages, using whichever route works on the VDI:
   - Internal PyPI mirror (Artifactory/Nexus):
     ```bash
     pip install -r requirements.txt --index-url <internal-mirror-url>
     ```
   - Offline: on a machine with internet access, download the exact versions tested on Python 3.8:
     ```bash
     pip download -r requirements-py38.txt -d wheels --platform win_amd64 --python-version 3.8 --only-binary=:all:
     ```
     Copy `wheels/` across, then on the VDI:
     ```bash
     pip install --no-index --find-links wheels -r requirements-py38.txt
     ```
   - No admin rights: add `--user`, or create a virtual environment first with `python -m venv .venv`.
3. Start the app, then open http://localhost:8501 in the VDI browser. Use another port if 8501 is blocked.
   ```bash
   python -m streamlit run app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
   ```
4. Register the four presets in Stylus once. Their texts are on the **LLM Tasks** page.

`requirements.txt` gives version ranges and works on Python 3.8+. On Python 3.8 it holds reportlab below 4.3, because 4.3+ needs Python 3.9. `requirements-py38.txt` pins the exact versions tested on 3.8.

## Status
- **Phase 1 – done**
  - Deal setup with document uploads.
  - Section extraction from comparables: table of contents first (with page offset), each section's start and end
    checked against a bold, capital-letter heading; bookmarks and a heading scan only as fallbacks.
  - Review page with page images, outline, extracted text and manual page correction.
- **Phase 2 – done**
  - Company documents are read as printed pages. Annual-report two-page spreads are split, and each page gets a citation label like `D1 p.145`.
  - Pages are tagged by annual-report part.
  - Key financials and ratios are read from the statements (consolidated preferred over standalone).
  - Risk triggers are found, each with evidence.
  - BM25 maps relevant pages to each Our Business subsection and risk theme, using sector packs from `config/company_docs.yaml`.
  - Company Context page with editable figures, trigger selection and page selection.
- **Phase 3 – done**
  - Stylus preset texts in `core/prompts/presets/`. Register them once; they are fixed and work for any company.
  - Blueprint, either from IPO-01 or a Python default, editable on the page.
  - Pack builder:
    - Task Sheet PDF plus original pages stamped with citation labels.
    - Two-page spreads go in whole with both halves labelled, so one Stylus page carries two printed pages.
    - Each run stays at 90 pages and 9 MB or less (Stylus allows 100 pages and 10 MB). Large images are
      downsampled first; only then are the least important source pages dropped. Text is never changed.
  - Reply reader for .docx, .md, .txt and .pdf. It matches the Task ID, parses the blueprint or the draft, and checks citations.
  - LLM Tasks page.
- **Phase 4 – done** (tested with mock replies; waits on a real Stylus reply)
  - Section Workspace page: pick a subsection, edit heading and text, save. Every save keeps the earlier text as a version you can restore.
  - Right-hand tabs:
    - References: each citation opens the page text (numbers used in the draft in bold) or the page image.
    - Comparables: the comparable section opens at the heading closest to the current subsection; search and page images.
    - Checks: numbers not on the cited page, numbers without a citation, unknown or comparable citations, sentences copied from comparables, mitigation language and heading form for risk factors, [TBD]s, required risks not covered.
  - Full-section view and downloads: Word (.docx), Markdown, and a CSV check report.
  - IPO-04 "Revise Subsection" preset: a small pack (current text, banker instructions, check results, related pages); the reply replaces only that subsection.
- Phase 5 is described in PLAN.md §8.

Logo: put the official file in `assets/` (see `assets/README.md`).

## Tests

```bash
python -m pytest tests -q
```

To also check real documents:
- Set `IPO_SAMPLE_DRHP` to the ICICI Prudential AMC DRHP. It should give Risk Factors = PDF p35–65 and Our Business = PDF p160–184.
- Set `IPO_SAMPLE_AR` to the SBI Funds Management AR 2024-25. It should give 206 printed pages and consolidated PAT of ₹2,54,015.32 lakhs.

## Layout
- `core/pdf_reader.py`: page text (cached in `data/cache`), bookmarks, bold headings, page images
- `core/section_extractor.py`: start/end detection as (page, line) and subheading outline
- `core/company_docs.py`: printed-page units, spread splitting, running-header removal, part tagging
- `core/financials.py`: statement row parser, figures and derived ratios
- `core/risk_triggers.py`: financial and text-based triggers with evidence
- `core/retrieval.py`: offline BM25 and subsection/theme page mapping
- `core/context.py`: runs Phase 2 and saves context per deal (`data/deals/<deal>/context/`)
- `core/blueprint.py`: blueprint model, default blueprint, IPO-01 JSON import
- `core/packer.py`: page selection within the budget, page stamping, Task Sheet PDF, IPO-01/02/03/04 pack builders
- `core/tasks.py`: task model, storage and pack zip
- `core/replies.py`: reply reading, Task ID matching, blueprint/draft parsing, citation checks, IPO-04 merge
- `core/validator.py`: draft checks (numbers vs cited pages, citations, copying, risk-factor rules)
- `core/workspace.py`: edits with version history, Word / Markdown export
- `core/store.py`: per-deal storage in `data/deals/<deal>/` (git-ignored)
- `config/sections.yaml`: heading variants, fallback end markers, matching thresholds
