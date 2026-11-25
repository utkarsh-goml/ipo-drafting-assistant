# IPO Drafting Assistant (MVP / PoC) — Build Plan

Drafts the **"Our Business"** and **"Risk Factors"** sections of an Indian DRHP/RHP.

- **Facts** come from company documents: the annual report plus optional extras such as an investor presentation, management Q&A notes or audited financials.
- **Structure and style** are learned from comparable companies' DRHPs/RHPs.

Python does the extraction, retrieval, packing and validation. The Citi GenAI tool does the writing. The UI is Streamlit.

---

## 1. Constraints (confirmed)

| Constraint | Design response |
|---|---|
| Citi GenAI is a **chat UI only**, with no API | The app runs a **"Prompt Pack" workflow**. For each LLM task the app gives the banker (a) a prompt to copy and (b) a context file to upload. The banker pastes the reply back into the app, and the app parses and validates it. |
| The **100-page limit applies to all uploaded files in a run combined**. A run can take several files. | Every context file is built as a **PDF made of selected source pages**, so the page count is exact. A Python bin-packer keeps the **total across all files in a run at ≤ 90 pages** (10-page margin). Each pack is split into 2–3 clearly named files (see §2.1). |
| A run is also limited to **10 MB** (reported 2026-11-19; treated as combined across files, like the page limit). | Stamps use a standard non-embedded font, page content is recompressed, and large images are downsampled step by step (2200 → 1600 → 1200 → 900 px); text is never changed. Only if still too big are the lowest-priority source pages dropped and listed. Budget **≤ 9 MB** (1 MB margin). |
| Stylus output can be **downloaded as a file** and is **never cut off** (10+ pages seen) | The banker uploads the downloaded reply file into the app. The parser accepts `.docx`, `.md`, `.txt` and `.pdf`. Each section is drafted in **one call**. |
| Presets can be **created immediately** | The preset texts are written in Phase 3 and registered in Stylus straight away. |
| Company inputs vary | Multiple company documents are allowed, each with a type: `ANNUAL_REPORT`, `FINANCIALS`, `INVESTOR_PPT`, `MGMT_QA`, `OTHER`. Each gets an ID (`D1`, `D2`, …) that is used in citations. |
| Output is shown on screen and must be editable, with references and comparables in the same view | A **Section Workspace** screen (see §5). |
| Confidential data | No external calls besides the Citi tool. Retrieval is offline (BM25). Everything is stored locally per deal. |

### Why PDF page subsets instead of text files
- The Citi tool's page count for a `.txt` or `.docx` file is uncertain, while a PDF's page count is exact.
- Original pages keep their tables and layout, which matters for financial statements.
- Python stamps each page with a source label such as `[D1 p.45]`. The model can cite that label, and the app can map it back to the original page.

---

## 2. End-to-end workflow

```
[1] Create deal  →  [2] Upload docs  →  [3] Extract & review comparable sections
      →  [4] Process company docs  →  [5] Style learning (LLM)  →  [6] Blueprint (LLM)
      →  [7] Draft subsections (LLM)  →  [8] Validate  →  [9] Edit / review in Workspace
```

### 2.1 Citi Stylus Workspace presets (v2 design)

Stylus Workspace lets a team save standard prompts as **presets** on the backend. Presets are shared across teams and can't be changed by individual users. The user only uploads a file and gets the output. The model used is **Sonnet 5.5**.

**How the app uses presets: a small number of fixed presets plus a Task Sheet inside each pack.**

- The fixed rules sit in the preset, which is governed and cannot be edited by users. The deal-specific details are written by Python onto **page 1 of the pack PDF**, called the **Task Sheet**. It contains the blueprint, financial indicators, required risk factors, the list of subsections to write and the output format.
- The preset prompt says, in effect: *"Follow the TASK SHEET on page 1. Use only the pages labelled `D*` as sources of facts. Pages labelled `C*` are style references only."*
- **The banker never copies a prompt.** Each round trip is just: download the pack (a zip of 2–3 PDFs) → upload the files to the preset → download the reply file → upload it into the app.

**Files in each pack** (one run, **≤ 90 pages and ≤ 9 MB in total**):
| File | Contents | Typical pages |
|---|---|---|
| `00_TASK_SHEET_<TaskID>.pdf` | Instructions for this run, blueprint, indicators, required risks, output format | 2–4 |
| `01_SOURCE_<TaskID>.pdf` | Company-document pages, each stamped `[D1 p.45]` | 50–65 |
| `02_STYLE_REF_<TaskID>.pdf` | Short comparable examples, each stamped `[C2 p.212] STYLE ONLY` | 15–25 |

The packer divides the 90-page budget between these files and records the split on the Task Sheet.

| Preset | Input pack (≤ 90 pages) | Output |
|---|---|---|
| `IPO-01 Style & Blueprint` | Task Sheet + key pages from the comparables, for both sections | JSON: style profile + subsection blueprint + risk taxonomy |
| `IPO-02 Draft Our Business` | Task Sheet (blueprint, indicators) + company-document pages + 1–2 short comparable examples | Subsections in the fixed `### <ID> <Title>` format with citations |
| `IPO-03 Draft Risk Factors` | Task Sheet (taxonomy, required risks, indicators) + company-document pages | Risk factors in groups, quantified, with citations |
| `IPO-04 Revise Subsection` (optional) | Task Sheet (current draft + banker's instructions) + source pages | One revised subsection |

### 2.2 Reducing round trips

1. **One call learns the style for both sections.** Python already finds the subsection headings in the comparables from their bold headings, so the model doesn't need the full 60-page sections. The packer takes the **outline plus the first page or two of each subsection** from every comparable (about 40 pages per section). The style profile and the blueprint for both sections then come from one call.
2. **One drafting call per section.** Output is never cut off, so each section is drafted in a single call. The only limit is the 90 pages of input, so the packer ranks company-document pages by relevance and keeps the most relevant ones. The Task Sheet lists any relevant pages that were left out. If a section needs more source pages than fit, the packer splits it into two runs.
3. **Reuse style profiles across deals in the same sector.** Each blueprint is saved to a sector library, so the next deal in that sector can skip IPO-01.
4. **Bring the reply back as a file.** The banker uploads the downloaded Stylus reply. The app reads the Task ID that the preset tells the model to print first in its reply, and matches the reply to the right task.

| Task | Round trips |
|---|---|
| IPO-01 Style & Blueprint (skipped when the sector library has one) | 1 (or 0) |
| IPO-02 Draft Our Business | 1 |
| IPO-03 Draft Risk Factors | 1 |
| **Total per deal** | **3** (2 when the sector library has a blueprint) |

Each later revision of a single subsection (IPO-04) is one more round trip, used only when the banker asks for it.

---

## 3. Python components

### 3.1 Comparable section extractor (`core/section_extractor.py`)
Three methods are tried in order. The first that gives a valid result wins.

1. **PDF bookmarks** (`pypdf` outline). This is the most reliable when present.
2. **Table of contents**: scan the first ~15 pages for lines like `OUR BUSINESS ..... 187`, build the ordered list of sections with printed page numbers, and calculate the offset between printed and PDF page numbers (front pages use roman numerals).
3. **Heading scan**: `pdfplumber` characters with font size and boldness. A line counts as a heading if it is on its own, in upper case or a large/bold font, and fuzzy-matches (`rapidfuzz` ≥ 90) a known variant.

- **Start** = the page and line where the section heading appears.
- **End** = the line before the next section's heading (page and line number).
- **Ignored matches**: TOC entries, cross-references such as "see 'Our Business' on page…", and the Offer Document Summary.

Heading variants are kept in `config/sections.yaml`, for example:
- Our Business → next section: `KEY REGULATIONS AND POLICIES`, `KEY INDUSTRY REGULATIONS AND POLICIES`, `…IN INDIA`
- Risk Factors → next section: `SECTION III – INTRODUCTION`, `THE OFFER`, `SUMMARY OF THE OFFER`

Output: `ExtractedSection{doc_id, section, start_page, start_line, end_page, end_line, method, confidence, pages:[{page_no, text}]}`.
**The banker can override the start and end pages in the UI**, and the section is then re-extracted.

### 3.2 Company document processor (`core/company_docs.py`)
- Read pages from every company document. DOCX files are converted to PDF first, or read as text with a page count estimated at about 500 words per page.
- Tag the parts of the annual report by heading: Chairman's message, Directors' Report, MD&A, BRSR, Corporate Governance, Standalone/Consolidated financial statements, Notes.
- Treat each **page** as a retrieval unit with an ID `D{n}-p{page}`.
- **BM25 retrieval** for each blueprint subsection, using keyword lists from `config/keywords.yaml` (sector-aware).

### 3.3 Financial indicators and risk triggers (`core/financials.py`)
- Read key figures from the financial statement tables, with a manual-entry form as fallback: revenue, EBITDA, PAT, operating cash flow, borrowings, equity, contingent liabilities, related-party transaction totals, top-customer share, and segment/geography split.
- Calculate growth, margins, debt-to-equity and similar ratios. These numbers go to the LLM ready-made, so it doesn't do the arithmetic.
- **Mandatory risk triggers** (SEBI ICDR practice): negative operating cash flow, contingent liabilities, related-party transactions, customer or supplier concentration, high debt, pending litigation, dependence on few products or plants, and so on. Each trigger that fires becomes a **required risk factor** in the drafting prompt.

### 3.4 Context pack builder (`core/packer.py`)
- Takes the pages chosen for a task, groups them by priority, and fills packs of ≤ 90 pages.
- Writes a PDF with a source label stamped at the top of each page, plus a cover page listing the pack contents.
- Comparable example pages are labelled `[C2 p.212] STYLE REFERENCE – DO NOT USE FACTS`.

### 3.5 Preset texts and Task Sheet templates (`core/prompts/`)
- `presets/IPO-0x.md` holds the exact preset texts to register in Stylus Workspace. They are versioned in git, and each preset name includes its version, e.g. `IPO-02 v1.0`.
- `task_sheets/*.md` are the templates Python fills and renders as page 1 of each pack. Each Task Sheet includes a Task ID, the deal, the preset version, the subsection list, indicators, required risks and the output format.

Rules common to all presets:
- Use only facts from the uploaded pack and the provided indicators.
- Cite every fact as `[D1 p.45]`. Never cite a `C*` comparable as a source of facts.
- Write `[●]` or `[TBD: <what is missing>]` when data is missing. Never make figures up.
- Risk Factors: quantify each risk, no softening language, order by materiality, use DRHP-style bold headings.
- **Fixed output format**, so the reply can be parsed (subsections delimited as `### <ID> <Title>`).

### 3.6 Response parser and validator (`core/validator.py`)
- Splits the pasted reply into subsections according to the blueprint.
- Pulls out citation tags and links each one to its source page.
- **Number check**: every number or percentage in the draft must appear on the cited page or in the indicators, otherwise it is flagged ⚠.
- **Missing-citation check**: flags sentences that state facts but have no citation.
- **Copy check**: flags sentences with ≥ 85% similarity to a comparable's text.
- **Risk Factors checks**: every mandatory trigger is covered, and no softening language appears.

### 3.7 Storage (`core/store.py`)
- One folder per deal at `data/deals/<deal_id>/`: uploads, extracted sections (JSON), packs (PDF), prompts, raw replies, drafts and edit history (JSON).
- The banker can close the app and resume later.

---

## 4. LLM backend interface (works with any LLM)

```
LLMBackend.run(task) -> response_text
  ManualBackend   (MVP)  : builds the prompt + pack, waits for the pasted reply
  LocalBackend    (dev)  : Ollama, for testing with public data only
  CitiApiBackend  (later): if Citi ever exposes an API
```

---

## 5. Streamlit UI

| Page | Purpose |
|---|---|
| **1. Deal setup** | Name, sector, FY. Upload company documents (with type tag) and comparable DRHPs/RHPs. |
| **2. Section extraction** | Table per comparable: section, start (page/line), end (page/line), method, confidence. Editable page fields, plus a preview of the first and last page. |
| **3. Company context** | Document parts found, financial indicators (editable), risk triggers that fired. |
| **4. LLM tasks** | Task queue as cards: *Download pack PDF* → "Upload to preset **IPO-02 v1.0**" → *Upload or paste the reply* → *Validate & save*. Status shown per task. |
| **5. Section Workspace** | See below. |

### Section Workspace (one screen for each section)
```
┌ Section: [Our Business ▼]   Subsection: [Overview ▼]   [Save] [Regenerate prompt] ┐
├───────────────── Draft (editable) ────────────┬──── Right panel (tabs) ───────────┤
│ text_area with the subsection draft           │ [References] [Comparables] [Checks]│
│ - Edit / Preview toggle                       │ References: each citation [D1 p.45]│
│ - citations shown inline                      │  → extracted text + page image     │
│ - edits saved with version history            │ Comparables: pick C1..Cn → same    │
│                                               │  section, searchable, page images  │
│                                               │ Checks: ⚠ unverified numbers,      │
│                                               │  missing citations, copy matches   │
└───────────────────────────────────────────────┴────────────────────────────────────┘
```
- An option to show the full section, i.e. all subsections stitched together, read-only.
- Copy to clipboard and download as `.md`. Word export may be added later.

---

## 6. Tech stack
`streamlit`, `pdfplumber` (MIT), `pypdf` (BSD), `reportlab` (page stamping and cover page), `rapidfuzz`, `rank_bm25`, `pandas`, `pydantic`, `pyyaml`. Optional: `python-docx` (reading DOCX inputs), `pytest`.
PyMuPDF is avoided because its AGPL licence would need Citi legal approval.

## 7. Folder layout
```
IPO_DRAFT/
  app.py
  pages/ 1_Deal_Setup.py 2_Section_Extraction.py 3_Company_Context.py 4_LLM_Tasks.py 5_Workspace.py
  config/ sections.yaml keywords.yaml
  core/ pdf_reader.py section_extractor.py company_docs.py financials.py packer.py
        validator.py store.py models.py llm/{base,manual,local}.py prompts/*.md
  data/            (gitignored)
  tests/fixtures/  (public DRHPs + annual reports)
```

## 8. Build phases

| Phase | What gets built | Done when |
|---|---|---|
| 1 | PDF reader + comparable section extractor + extraction review page | Correct start/end on 5 public DRHPs (SEBI/NSE) |
| 2 | Company document processor, BM25 mapping, indicators, risk triggers | Sensible pages chosen for each subsection on 2 annual reports |
| 3 | Packer + prompt templates + LLM task queue (manual) | One full round trip through the Citi chat UI works |
| 4 | Parser/validator + Section Workspace (edit, references, comparables, checks); version history, full-section view, Word/Markdown export, IPO-04 revise | Both sections drafted, editable, every citation clickable — **built 2026-11-18; waits on a real Stylus reply** |
| 5 | Polish: prompt tuning on real Stylus replies, more DRHPs/ARs in tests, sector dictionaries | Demo-ready |

## 9. Test data
Use public data only during development: recent DRHPs/RHPs from sebi.gov.in and annual reports of listed companies in the same sector. Don't use any client documents until the tool is approved.

## 10. Open items
- Which file format does Stylus download replies in? The parser handles `.docx`, `.md`, `.txt` and `.pdf`, so this only affects which format to test first.
- Sector keyword dictionaries need input from the banking team.

Resolved (2026-11-18/19): multiple files per run are allowed, with 100 pages and 10 MB combined; replies can be downloaded as files; output is not cut off; presets can be created immediately.
