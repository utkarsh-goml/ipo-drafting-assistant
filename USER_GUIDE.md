# IPO Drafting Assistant – User Guide

This guide is for anyone using the tool for the first time. It explains the whole process from start to finish. You don't need to know any programming.

---

## 1. What the tool does

The tool helps you write the first draft of two DRHP sections for an Indian IPO:
- **Our Business**
- **Risk Factors**

The work is split in two:

| Who | Does what |
|---|---|
| **The tool (Python, on your machine)** | Reads the PDFs, finds the right sections and pages, pulls out the key figures, and prepares small "packs" of pages. It then checks the draft that comes back. |
| **Stylus Workspace (Citi GenAI)** | Writes the text, using the pack you upload to it. |
| **You (the banker)** | Upload the documents, review what the tool found, move packs between the tool and Stylus, and edit and approve the final text. |

Every fact in the draft carries a page citation such as **[D1 p.145]**, so you can always check where it came from.

---

## 2. Two kinds of documents

| Kind | Examples | Label | Used for |
|---|---|---|---|
| **Company documents** | Annual report, audited financials, investor presentation, management notes | **D1, D2, …** | **Facts.** These are the only source of numbers and statements. |
| **Comparable prospectuses** | DRHPs/RHPs of similar listed companies | **C1, C2, …** | **Style and structure only.** No facts are ever taken from them. |

Citation labels:
- `[D1 p.145]`: company document D1, printed page 145. This is the page number printed on the page, not the PDF page number.
- `[C2 p.170]`: comparable C2, page 170. This appears only as a style example, never as a source of a fact.

---

## 3. The process at a glance

```
1 Deal Setup  →  2 Section Extraction  →  3 Company Context  →  4 LLM Tasks  ⇄  Stylus  →  5 Section Workspace
 (upload)        (find sections in          (figures, risks,      (build packs,           (edit, check,
                  comparables)               relevant pages)       read replies)            export Word)
```

The pages are listed in the left sidebar of the app, in this order.

---

## 4. One-time setup

### 4.1 Start the app
In a command window opened in the project folder, run:
```bash
python -m streamlit run app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
```
Then open **http://localhost:8501** in the browser. Installation on the office VDI is described in `README.md`.

### 4.2 Register the four presets in Stylus (once per team)
A *preset* is a fixed, shared prompt in Stylus. The tool has four:

| Preset | Purpose |
|---|---|
| **IPO-01 Style & Blueprint** | Learns the structure of the sections from the comparables (optional step). |
| **IPO-02 Draft Our Business** | Writes Our Business. |
| **IPO-03 Draft Risk Factors** | Writes Risk Factors. |
| **IPO-04 Revise Subsection** | Rewrites one subsection based on your instructions. |

To register them:
1. Open **LLM Tasks** and expand **"Stylus presets: register once in Stylus Workspace"**.
2. Copy or download each text, and create a preset with the same name in Stylus.

Preset names include a version, for example *v1.1*. If a version changes, update the text in Stylus.

---

## 5. Step by step

### Step 1 – Deal Setup
1. Expand **Create a new deal** and enter the company name, sector and latest financial year, then click **Create deal**.
2. On the **left**, under *Company documents*:
   - Choose the document type (for example *Annual report*).
   - Upload the PDFs and click **Add company documents**.
   - They become D1, D2, …
3. On the **right**, under *Comparable DRHPs / RHPs*, upload 2–3 prospectuses of similar companies and click **Add comparables**. They become C1, C2, …

Tip: pick comparables from the same sector and business model. Better comparables give a better structure.

### Step 2 – Section Extraction (comparables)
The tool finds where *Our Business* and *Risk Factors* start and end in each comparable.
1. Click **Extract sections**.
2. The tool finds each section like this:
   - It reads the **table of contents** first.
   - It checks that the section really starts with a **bold, capital-letter heading** (for example **OUR BUSINESS**).
   - The section ends just before the next section's bold heading.
   - The full rules are in the Appendix at the end of this guide.
3. Check the table:
   - The **Heading check** column shows ✓ when both the start and end headings were confirmed.
   - Anything marked ✗ or with low confidence needs a look.
4. To correct a page range, type the right start and end pages and click **Apply corrections**. To find the right pages, open **"Look at any other page"**:
   - Type a heading in **Search the text of this document**, for example `OUR BUSINESS` or the next section's name, such as `KEY REGULATIONS AND POLICIES`.
   - Tick **Headings only** to keep only lines in capital letters. This hides mentions in running text and cross-references.
   - The results table shows the PDF page, the printed page and the matching line. Choose a result under **Open a result** to see that page.
   - The section starts on the page with its heading, and ends on the page before the next section's heading (or on the same page, if that heading is halfway down).
5. Under **Review an extracted section**, you can see:
   - the start and end page images,
   - the outline of subheadings,
   - the extracted text.

### Step 3 – Company Context (company documents)
1. Click **Process company documents**. The first run can take about a minute per document. The tool:
   - splits two-page spreads into single printed pages and gives each one a label (`D1 p.N`);
   - tags each page by annual-report part (Directors' Report, MD&A, financial statements, …);
   - reads key figures from the financial statements (revenue, profit after tax, net worth, …) and calculates ratios. It uses consolidated figures where available, otherwise standalone;
   - finds **risk triggers**, i.e. signs that a risk applies, such as customer concentration, litigation or falling margins. Each trigger comes with its evidence;
   - matches the relevant pages to each Our Business subsection and each risk theme.
2. Review the four tabs and correct anything that's wrong:

   | Tab | What to check |
   |---|---|
   | **Document parts** | Pages are tagged to the right annual-report part. |
   | **Financial indicators** | Correct any figure, then click **Save figures**. These figures go to Stylus as *verified* numbers. |
   | **Risk triggers** | Tick the triggers that apply, then click **Save trigger selection**. Ticked triggers become **REQUIRED** risks that the draft must cover. |
   | **Pages per subsection** | Add or remove pages for a subsection or risk theme, then click **Save page selection**. |

Note: **Re-process** throws away the edits on this page.

### Step 4 – LLM Tasks (the Stylus round trip)
This page creates the packs and reads the replies.

#### 4a. The blueprint (Step 1 on the page)
The **blueprint** is the outline Stylus follows:
- which Our Business subsections to write, in what order, what each covers and how long it is;
- which Risk Factor themes to cover, in which group.

There are two ways to get one:
- **Use default blueprint**: instant. It gives 15 standard subsections plus risk themes based on your triggers.
- **Create IPO-01 pack**: Stylus learns the outline from your comparables (see §6). You upload the reply, and it replaces the blueprint.

To change the blueprint, open **Review / edit the blueprint**:
- Untick a subsection or theme to leave it out.
- Change titles, target word counts or risk groups.
- Click **Save blueprint**.

Changes only apply to packs you create afterwards.

#### 4b. Draft the sections (Steps 2 and 3 on the page)
- Click **Create IPO-02 pack** for Our Business.
- Click **Create IPO-03 pack** for Risk Factors.

Each pack becomes a task card. Then do the round trip in §6.

### Step 5 – Section Workspace (review and finish)
1. Choose **Our Business** or **Risk Factors**, then a subsection. A status icon shows whether that subsection has problems.
2. **Edit** the heading and text, then click **Save**:
   - Every save keeps the old text in **Version history**, and you can **Restore** any earlier version.
   - Use **Preview** to see the text formatted.
3. Use the right-hand tabs:

   | Tab | What it shows |
   |---|---|
   | **References** | Click a citation to see that page's text, with the numbers used in the draft in bold, or the page image. |
   | **Comparables** | The comparable section, opened at the most similar heading. You can search it. |
   | **Checks** | Problems the tool found (see §8). |

4. To get Stylus to rewrite one subsection:
   - Open **Revise this subsection with Stylus (IPO-04)** and type your instructions, for example *"shorten to 300 words, add the AUM growth table"*.
   - Click **Create IPO-04 pack** and do the round trip with preset IPO-04.
   - The reply replaces **only that subsection**. The old text is kept in Version history.
5. Look at **Gaps to obtain from the company**. These are facts Stylus needed but couldn't find in the documents (marked `[TBD: …]` in the draft). Send them to the company. If there are a lot of them, see "The draft has a lot of [TBD]s" in §9.
6. Switch to **Full section** view and download:
   - **Word (.docx)** of the section,
   - **Markdown**,
   - **CSV** report of all checks.

---

## 6. The Stylus round trip (same for IPO-01, 02, 03 and 04)

1. On the task card, click **⬇ Download pack (.zip)** and unzip it.
2. In Stylus Workspace, open the preset named on the card (for example *IPO-02 Draft Our Business*).
3. Upload **all the PDF files** from the pack in one go, and send.
4. Save Stylus's reply as .docx, .md or .txt, or copy it into a text file.
5. Back on the task card, upload the reply and click **Read reply**. The tool checks:
   - that the reply belongs to this task, using the **TASK_ID** on its first line;
   - that it is in the expected format;
   - that its citations are valid.
6. The draft now appears in the **Section Workspace**.

If the Task ID doesn't match, the reply was meant for a different pack. Make sure you uploaded the reply to the right card.

---

## 7. What is inside a pack

| File | Contents |
|---|---|
| **00_TASK_SHEET** | The instructions for this run: Task ID, deal, blueprint or risk list, verified figures, list of files. |
| **01_SOURCE** | Original pages from the company documents, each stamped `[D1 p.N]`. These are the **only** source of facts. For IPO-01, it holds a few overview pages so Stylus understands the business. |
| **02_STYLE_REF** | Pages from the comparables, stamped **STYLE ONLY**. They are used for structure and tone only. |

Stylus limits and how the tool stays within them:
- **Pages:** Stylus allows 100 pages per run. The tool keeps every pack at **90 pages or fewer**.
- **Size:** Stylus allows 10 MB per run. The tool keeps every pack at **9 MB or less**:
  - First it makes large photos smaller. The text is never changed.
  - Only if that isn't enough does it drop the least important pages. Dropped pages are listed in the Task Sheet.
- The task card shows the pages and MB used.

---

## 8. Checks the tool runs on every draft

| Check | What it means |
|---|---|
| Number not on cited page | A number in the text doesn't appear on the page it cites. Verify it. |
| No citation | A sentence with a number has no `[D… p.…]` reference. |
| Unknown or comparable citation | The text cites a page that wasn't in the pack, or cites a comparable (`C…`) as a source of a fact. |
| Copied from comparable | A sentence is very close to a sentence in a comparable. Rewrite it. |
| Mitigation language (Risk Factors) | SEBI does not allow wording like "we believe this risk is low" in risk factors. |
| Heading form (Risk Factors) | Each risk heading should be a full sentence describing the risk. |
| [TBD] | A fact still needs to be obtained from the company. |
| Required risk not covered | A risk trigger you ticked on page 3 has no risk factor. |

---

## 9. Common questions

**Do I have to run IPO-01?**
No. The default blueprint works. IPO-01 is useful when the comparables have an unusual structure that you want to follow.

**I edited the blueprint after drafting. Does the draft change?**
No. Create a new IPO-02 or IPO-03 pack to redraft with the new blueprint.

**A figure on page 3 was wrong.**
Correct it, click **Save figures**, and create the pack again. Packs made earlier still contain the old figure.

**The draft has a lot of [TBD]s. How do I reduce them?**
A `[TBD]` means Stylus didn't have the page with that fact. The summary line on the LLM Tasks page counts every `[TBD]`, so one missing fact used in five places counts five times. The gaps list groups them, which is why there are fewer gaps than TBDs.

The usual cause is too many subsections in one pack. A pack holds at most 90 pages, and about 65 of them are left for company pages. With 45 subsections, each one gets only one or two pages. The task card lists the relevant pages that were left out for space ("relevant page(s) left out for budget"). If that list is long, this is the problem.

Fixes, with the biggest effect first:
1. **Use fewer subsections (page 4, blueprint).** Untick subsections you don't need, or merge small ones. Check how many headings your comparables use, and aim for about that many. Then create the IPO-02 pack again.
2. **Run IPO-04 on the subsections with the most TBDs (page 5).** An IPO-04 pack covers one subsection only, so it gets far more pages. For instructions, type something like *"Fill the [TBD] items using the source pages; keep [TBD] only if the information is not there."*
3. **Fix the page mapping (page 3).** Search your documents for a few of the missing facts. If you find one, add that page to the subsection's mapping and create the pack again.
4. **Upload more company documents (page 1).** An annual report rarely has DRHP-level detail such as branch counts, customer splits or KPIs by year. Add the investor presentation, factsheets and full financial statements, then run page 3 again.
5. **Accept the rest as gaps.** What isn't in the documents stays `[TBD]` and goes on the gaps list for the company. That is the tool working correctly.

**Stylus says the upload is too large.**
Create the pack again, because packs made before the size check could be over 10 MB. Look at the MB bar on the task card.

**The Section Extraction page shows ✗ in the Heading check.**
The bold capital heading wasn't found where the table of contents pointed. Open the page images, find the correct page, and type it in.

**Where is my data stored?**
In the `data/` folder on the machine running the app. Nothing is sent anywhere except the packs you upload to Stylus yourself.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| DRHP / RHP | Draft Red Herring Prospectus / Red Herring Prospectus |
| Comparable | Prospectus of a similar listed company, used as a style reference |
| Blueprint | The outline (subsections and risk themes) that Stylus follows |
| Pack | The set of PDFs (Task Sheet, Source, Style Ref) you upload to Stylus |
| Preset | A fixed, shared prompt registered in Stylus Workspace |
| Task ID | A unique code for each pack (for example `IPO02-20261119-…`) that links the reply to its pack |
| Risk trigger | Evidence in the company documents that a particular risk applies |
| Printed page | The page number printed on the document, used in citations |

---

## Appendix – How the tool finds sections and picks pages

This appendix explains the rules the tool follows. You don't need it for day-to-day use. It helps when a result looks wrong and you want to know why.

### A. Finding Our Business and Risk Factors in a comparable (page 2)

The rule behind it: in a DRHP, every named section is **listed in the table of contents** and **starts with a bold, capital-letter heading**.

1. **Find the table of contents.**
   - The tool looks for "TABLE OF CONTENTS", "CONTENTS" or "INDEX" in the first 25 pages.
   - It reads each line that has a section title and a page number.
2. **Work out the page offset.**
   - The table of contents uses the page numbers printed on the pages. The PDF has extra pages at the front (cover, notices), so the two differ.
   - The tool compares printed page numbers with PDF page numbers across the document and uses the most common difference.
   - If the printed numbers can't be read, it works out the difference from where the section heading actually appears.
3. **Find the start.**
   - It goes to the page the table of contents points to and looks for the section heading in **bold capitals**: OUR BUSINESS or BUSINESS OVERVIEW; RISK FACTORS.
   - It also checks up to **4 pages either side**, nearest first. The heading must match at least **90 out of 100**.
4. **Find the end.**
   - The end is the **next entry in the table of contents**. The tool finds that section's bold capital heading in the same way.
   - The section ends on the **line just before** that heading, so a section that ends halfway down a page is cut correctly. If the next heading is at the top of a page, the section ends on the previous page.
5. **Fallbacks (only if step 1 fails).**
   - First the PDF's own **bookmarks**, then a **heading scan** of the whole document.
   - For the heading scan, the end is the first heading from a fixed list. After Our Business, that list includes KEY REGULATIONS AND POLICIES and HISTORY AND CERTAIN CORPORATE MATTERS. After Risk Factors, it includes THE OFFER and INTRODUCTION.
   - Both fallbacks show a note asking you to verify the result.
6. **Confidence and Heading check.**

   | Result | Confidence |
   |---|---|
   | Found from the table of contents, both headings confirmed in bold | 100% |
   | Start heading found, but not in bold | −15% |
   | Start heading not found; the table of contents page used as is | −25% |
   | End heading found, but not in bold | −10% |
   | End heading not found | −20% |
   | Heading scan, end found / end not found | 60% / 40% |

   The **Heading check** column shows ✓ only when both headings were confirmed in bold capitals.
7. **Subheading outline.**
   - Inside the section, the tool lists the bold lines that start at the left margin. These are the subheadings that IPO-01 uses to design the blueprint.
   - It leaves out lines that look like table headers or totals, and running headers repeated on many pages.

### B. Splitting the company documents into pages (page 3)

- Where a PDF shows two printed pages side by side (a spread), the tool splits it into two pages. Each page is labelled with its **printed** page number, such as `[D1 p.145]`.
- Each page is tagged with the annual-report part it belongs to: Directors' Report, MD&A, BRSR, Corporate Governance, the financial statements and notes (consolidated or standalone), Auditor's Report, and so on.
- **Pages per subsection** is a keyword search. For each Our Business subsection and each risk theme, the tool scores every page on that subsection's search words and keeps the **top 6**. The search words are in `config/company_docs.yaml`; for example, Overview uses *overview, incorporated, leading, largest, established, presence, market share…*. If the deal's sector matches a sector pack, its words are added; for asset management, *AUM, QAAUM, schemes, SIP, folios, distributors…*.
- **Which parts it prefers for Our Business:**
  - **Scored up:** Corporate Overview, Directors' Report, MD&A, BRSR and Corporate Governance.
  - **Scored down:** auditor's report, secretarial audit and AGM notice. Also the financial statements and notes, except for the Properties and Key Financial Metrics subsections.
  - When consolidated statements exist, standalone statements are scored down too.
- You can change any page list on the **Pages per subsection** tab. The packs use your edited list.

### C. Which company pages go into each pack

Every pack must fit in **90 pages**. About 8 are kept for the Task Sheet. Pages are chosen in **rounds**: each subsection or risk adds its best page not yet chosen, then its next best, and so on until the space runs out. If one PDF page carries two printed pages, the second one comes free. When pages have to be cut, the last ones added are cut first, and the cut pages are listed in the Task Sheet and on the task card.

| Pack | Company pages (01_SOURCE) | Comparable pages (02_STYLE_REF) |
|---|---|---|
| **IPO-01** Blueprint | Up to **4 PDF pages** from the **Overview** row of *Pages per subsection*, just enough for Stylus to understand the business | The rest of the space, shared across the extracted comparable sections. For each section: its first page, then the pages with the most subheadings, then the following pages in order |
| **IPO-02** Our Business | First the financial statement pages the verified figures came from, then the pages mapped to each blueprint subsection, in rounds | 14 pages |
| **IPO-03** Risk Factors | First the financial statement pages, then the **evidence pages of each ticked risk trigger**, then up to 4 pages for each risk theme, in rounds | 14 pages |
| **IPO-04** Revise one subsection | The pages that subsection already cites, its mapped pages, and 8 pages found by searching its title and your instructions. At most 40 pages | 6 pages |

What this means in practice:
- The more subsections or risks in one pack, the fewer pages each one gets. See "The draft has a lot of [TBD]s" in §9.
- To make sure a page reaches Stylus, add it on the *Pages per subsection* tab, then create the pack again.
