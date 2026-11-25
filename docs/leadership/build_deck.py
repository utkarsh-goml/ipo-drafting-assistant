"""Builds docs/leadership/IPO Drafting Agent - Leadership Review.pptx (leadership deck, before deployment).

    pip install python-pptx            # only needed for this script; not part of the app
    python docs/leadership/build_deck.py "docs/leadership/IPO Drafting Agent - Leadership Review.pptx"

Numbers come only from verified results: the test-run slide (8) is from a real cold run on 25 Nov 2026 on public
documents (SBI Funds Management AR 2024-25 + two AMC DRHPs), stopped before any AI call. Unknown figures are [__].
Slide 4 fades its steps in automatically when the slide opens.
"""
import sys
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from lxml import etree

OUT = sys.argv[1]

NAVY = RGBColor(0x14, 0x21, 0x3D)
BLUE = RGBColor(0x2B, 0x59, 0xC3)
BG = RGBColor(0xF6, 0xF7, 0xF9)
MUTED = RGBColor(0x4D, 0x57, 0x6C)
LINE = RGBColor(0xDF, 0xE2, 0xE6)
TINT = RGBColor(0xE8, 0xEE, 0xFB)
ICE = RGBColor(0xBF, 0xD0, 0xF5)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
RUST = RGBColor(0xB2, 0x3A, 0x0A)
CONN = RGBColor(0x5A, 0x65, 0x7C)
FONT = "Arial"

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]


# ---------- helpers ----------
def bg(slide, color):
    f = slide.background.fill
    f.solid()
    f.fore_color.rgb = color


def text(slide, x, y, w, h, paras, size=14, color=NAVY, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, margin=0, spacing=None, name=None, space_after=0):
    """paras: str | list of paragraphs; a paragraph is str or list of (text, {opts}) runs."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.vertical_anchor = anchor
    m = Inches(margin)
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = m
    if isinstance(paras, str):
        paras = [paras]
    for i, p in enumerate(paras):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        if space_after:
            para.space_after = Pt(space_after)
        if spacing:
            para.line_spacing = spacing
        runs = [(p, {})] if isinstance(p, str) else [(r, {}) if isinstance(r, str) else r for r in p]
        for t, o in runs:
            r = para.add_run()
            r.text = t
            f = r.font
            f.name = FONT
            f.size = Pt(o.get("size", size))
            f.bold = o.get("bold", bold)
            f.italic = o.get("italic", False)
            f.color.rgb = o.get("color", color)
            if o.get("caps_spacing"):
                r._r.get_or_add_rPr().set("spc", str(o["caps_spacing"]))
    return tb


def box(slide, x, y, w, h, fill=WHITE, line=LINE, line_w=1.0, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08, name=None):
    s = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        s.name = name
    s.shadow.inherit = False
    if fill is None:
        s.fill.background()
    else:
        s.fill.solid()
        s.fill.fore_color.rgb = fill
    if line is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = line
        s.line.width = Pt(line_w)
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = radius
    s.text_frame.text = ""
    return s


def circle_num(slide, x, y, d, label, fill=BLUE, color=WHITE, size=14):
    c = box(slide, x, y, d, d, fill=fill, line=None, shape=MSO_SHAPE.OVAL)
    tf = c.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    r.font.name = FONT
    r.font.size = Pt(size)
    r.font.bold = True
    r.font.color.rgb = color
    return c


def line(slide, x1, y1, x2, y2, arrow=False, color=CONN, width=1.5, dash=False):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = color
    c.line.width = Pt(width)
    ln = c.line._get_or_add_ln()
    if dash:
        d = etree.SubElement(ln, qn("a:prstDash"))
        d.set("val", "dash")
    if arrow:
        t = etree.SubElement(ln, qn("a:tailEnd"))
        t.set("type", "triangle")
        t.set("w", "med")
        t.set("len", "med")
    return c


def header(slide, eyebrow, title):
    text(slide, 0.6, 0.5, 12.1, 0.3, [[(eyebrow.upper(), {"bold": True, "caps_spacing": 200})]], size=12, color=BLUE)
    text(slide, 0.6, 0.82, 12.1, 0.8, title, size=32, bold=True, color=NAVY, name="Title")


def content_slide(eyebrow, title, notes):
    s = prs.slides.add_slide(BLANK)
    bg(s, BG)
    header(s, eyebrow, title)
    s.notes_slide.notes_text_frame.text = notes
    return s


def footer_note(slide, runs, y=6.55):
    text(slide, 0.6, y, 12.1, 0.45, [runs], size=13, color=MUTED)


# ---------- 1. title ----------
s = prs.slides.add_slide(BLANK)
bg(s, NAVY)
text(s, 0.9, 2.0, 11, 0.4, [[("LEADERSHIP REVIEW · BEFORE DEPLOYMENT", {"bold": True, "caps_spacing": 200})]], size=14, color=ICE)
text(s, 0.9, 2.45, 11.5, 1.1, "IPO Drafting Agent", size=54, bold=True, color=WHITE, name="Title")
text(s, 0.9, 3.75, 9.5, 1.2,
     "Drafts the DRHP “Our Business” and “Risk Factors” sections end to end. The banker approves twice, "
     "and every fact is cited to its page.", size=20, color=ICE, spacing=1.1)
text(s, 0.9, 5.25, 9, 0.4, "Equity Capital Markets · November 2026 · [Presenter name]", size=14, color=ICE)
s.notes_slide.notes_text_frame.text = (
    "This is the next stage of the IPO Drafting Assistant we showed earlier. The proof of concept needed the banker to carry every "
    "pack to Stylus and back by hand. The agent now does those steps itself, with the banker approving at two points. "
    "We are asking for approval to deploy it as a demo on public documents.")

# ---------- 2. summary ----------
s = content_slide("Summary", "Built and tested; ready for a demo on public documents",
                  "Three points. What it is: an agent that runs the whole drafting flow, not just one step. Where it stands: all build "
                  "steps are done and tested on a real annual report and two real DRHPs, but it has not yet run with the live AI model, "
                  "because that needs an OpenAI key. What we ask: approval to deploy a password-protected demo that uses public documents only.")
cols = [
    ("What it is", ["An AI agent that drafts Our Business and Risk Factors from the company’s own documents.",
                    "It reads, plans, drafts, checks and fixes by itself.",
                    "The banker approves the facts first and the draft last."]),
    ("Where it stands", ["Every build step is done and tested on a real annual report and two real DRHPs.",
                         "280 automated tests pass.",
                         "Not yet run with the live AI model: this needs an OpenAI API key."]),
    ("What we ask", ["Approval to deploy a password-protected demo.",
                     "Public documents only, until compliance approves anything else.",
                     "An OpenAI API key and a small usage budget."]),
]
for i, (head, items) in enumerate(cols):
    x = 0.6 + i * 4.1
    box(s, x, 1.9, 3.9, 3.9, name=f"Card {i+1}")
    circle_num(s, x + 0.3, 2.15, 0.55, str(i + 1))
    text(s, x + 0.3, 2.9, 3.3, 0.45, head, size=20, bold=True)
    paras = [[("•  ", {"color": BLUE, "bold": True}), (t, {})] for t in items]
    text(s, x + 0.3, 3.45, 3.35, 2.7, paras, size=14, color=MUTED, space_after=8)

# ---------- 3. before vs now ----------
s = content_slide("What has changed", "From a manual round trip to an agent that runs the flow",
                  "On the left is the proof of concept we showed before. The tool prepared packs, but the banker carried each one to "
                  "Stylus and brought the reply back: two manual hand-offs per pack, one section at a time. On the right is the agent. "
                  "It calls the AI model directly, drafts all parts at the same time, and checks and fixes them before the banker sees "
                  "anything. The banker's role is now two approvals instead of shuttling files. Note that the agent uses OpenAI's API "
                  "rather than Stylus, because Stylus has no API today.")
for i, (head, sub, items, fill, hc) in enumerate([
    ("Before", "Proof of concept with Stylus", [
        "The banker uploads every pack to Stylus and loads every reply back.",
        "2 manual hand-offs per pack, for each section and each revision.",
        "One section at a time.",
        "Checks run after the banker loads the reply."], WHITE, MUTED),
    ("Now", "LangGraph agent with the OpenAI API", [
        "The agent sends the work to the AI model and reads the reply itself.",
        "No manual uploads.",
        "All parts are drafted at the same time.",
        "Checks and fixes run automatically before the banker sees the draft."], TINT, BLUE),
]):
    x = 0.6 + i * 6.35
    box(s, x, 1.9, 5.75, 4.35, fill=fill, line=LINE if i == 0 else ICE)
    text(s, x + 0.35, 2.15, 5, 0.5, head, size=24, bold=True, color=hc)
    text(s, x + 0.35, 2.7, 5, 0.4, sub, size=14, color=MUTED)
    paras = [[("•  ", {"color": hc, "bold": True}), (t, {})] for t in items]
    text(s, x + 0.35, 3.3, 5.05, 2.8, paras, size=15, color=NAVY, space_after=9)
arr = box(s, 6.43, 3.75, 0.45, 0.6, fill=BLUE, line=None, shape=MSO_SHAPE.RIGHT_ARROW)
footer_note(s, [("Still true: ", {"bold": True, "color": NAVY}),
                ("facts come only from the company’s documents; comparables are used for style only.", {})])

# ---------- 4. process flow (flashy, animated) ----------
GOLD = RGBColor(0xF2, 0xB8, 0x4B)
TEAL = RGBColor(0x2E, 0xC4, 0xB6)
ABLUE = RGBColor(0x5B, 0x8D, 0xEF)
DCARD = RGBColor(0x1E, 0x2D, 0x52)
TRACK = RGBColor(0x33, 0x45, 0x70)
ACTOR = {"B": ("Banker", GOLD), "A": ("Agent", ABLUE), "M": ("AI model", TEAL)}


def add_entrance_animations(slide, spids, step_ms=450, dur_ms=500):
    """Fade each shape in, one after another, starting when the slide opens (no clicks needed)."""
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    ids = iter(range(3, 10000))
    pars = []
    for i, spid in enumerate(spids):
        a, b, c, d = next(ids), next(ids), next(ids), next(ids)
        pars.append(
            f'<p:par><p:cTn id="{a}" fill="hold"><p:stCondLst><p:cond delay="{i * step_ms}"/></p:stCondLst><p:childTnLst>'
            f'<p:par><p:cTn id="{b}" presetID="10" presetClass="entr" presetSubtype="0" fill="hold" '
            f'nodeType="{"afterEffect" if i else "afterEffect"}"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
            f'<p:set><p:cBhvr><p:cTn id="{c}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
            f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>'
            f'</p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>'
            f'<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="{d}" dur="{dur_ms}"/>'
            f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cBhvr></p:animEffect>'
            f'</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>')
    xml = (f'<p:timing xmlns:p="{P}"><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
           f'<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
           f'<p:par><p:cTn id="{next(ids)}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/>'
           f'<p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond></p:stCondLst><p:childTnLst>'
           + "".join(pars) +
           f'</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn>'
           f'<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
           f'<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq>'
           f'</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>')
    el = etree.fromstring(xml)
    sld = slide._element
    ext = sld.find(qn("p:extLst"))
    if ext is not None:
        ext.addprevious(el)
    else:
        sld.append(el)


s = prs.slides.add_slide(BLANK)
bg(s, NAVY)
text(s, 0.6, 0.5, 12.1, 0.3, [[("HOW IT WORKS", {"bold": True, "caps_spacing": 200})]], size=12, color=ICE)
text(s, 0.6, 0.82, 12.1, 0.8, "From documents to an approved draft in nine steps", size=32, bold=True, color=WHITE, name="Title")
s.notes_slide.notes_text_frame.text = (
    "The steps appear one by one when the slide opens. Gold is the banker, blue is the agent, green is the AI model. "
    "Phase one, prepare: the banker uploads the documents, the agent finds the sections in the comparables and pulls out "
    "the facts, and the banker approves those facts at checkpoint 1. Phase two, draft and check: the agent plans the work, "
    "the AI model drafts every part at the same time, the agent checks each part, and anything flagged goes back to the "
    "model to fix and is checked again, up to two rounds. Phase three: the banker reviews and approves at checkpoint 2, "
    "and only then does the agent export Word files. No files are carried by hand at any point.")
steps9 = [("B", "Upload documents"), ("A", "Find sections, extract facts"), ("B", "Approve facts"),
          ("A", "Plan the work"), ("M", "Draft all parts at once"), ("A", "Check every part"),
          ("M", "Fix flagged parts"), ("B", "Review and approve"), ("A", "Export Word")]
CX0, DX, TY = 1.3, 1.34, 3.6
cxs = [CX0 + i * DX for i in range(9)]
# phases
for (a, b, label, col) in [(0, 2, "PREPARE", GOLD), (3, 6, "DRAFT AND CHECK", TEAL), (7, 8, "APPROVE AND EXPORT", GOLD)]:
    x1, x2 = cxs[a] - 0.6, cxs[b] + 0.6
    pill = box(s, x1, 1.85, x2 - x1, 0.42, fill=DCARD, line=None, radius=0.5)
    text(s, x1, 1.85, x2 - x1, 0.42, [[(label, {"bold": True, "caps_spacing": 150})]], size=12, color=col,
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
# track
trk = line(s, cxs[0], TY, cxs[-1], TY, color=TRACK, width=6)
spids = []
for i, (act, label) in enumerate(steps9):
    who, col = ACTOR[act]
    cp = i in (2, 7)
    d = 1.05 if cp else 0.85
    grp = s.shapes.add_group_shape()
    grp.name = f"Step {i + 1}"
    if cp:
        halo = box(grp, cxs[i] - d / 2 - 0.1, TY - d / 2 - 0.1, d + 0.2, d + 0.2, fill=None, line=GOLD, line_w=1.5,
                   shape=MSO_SHAPE.OVAL)
        text(grp, cxs[i] - 1.0, 2.55, 2.0, 0.3, [[("BANKER CHECKPOINT", {"bold": True, "caps_spacing": 100})]],
             size=10, color=GOLD, align=PP_ALIGN.CENTER)
    circle_num(grp, cxs[i] - d / 2, TY - d / 2, d, str(i + 1), fill=col, color=NAVY, size=22 if cp else 18)
    text(grp, cxs[i] - 0.66, 4.35, 1.32, 0.65, label, size=13, bold=True, color=WHITE, align=PP_ALIGN.CENTER)
    text(grp, cxs[i] - 0.66, 5.0, 1.32, 0.3, who, size=11, color=col, align=PP_ALIGN.CENTER)
    if i == 6:  # loop back to the check
        arc = box(grp, cxs[5] + 0.1, 2.45, cxs[6] - cxs[5] - 0.2, 0.62, fill=TEAL, line=None, shape=MSO_SHAPE.CURVED_DOWN_ARROW)
        arc._element.spPr.find(qn("a:xfrm")).set("flipH", "1")
    spids.append(grp.shape_id)
# stat tiles
tiles = [("2", "banker checkpoints: facts first, draft last"), ("0", "files carried by hand"),
         ("≤ 2", "automatic fix rounds per part, then the banker decides")]
for i, (big, small) in enumerate(tiles):
    x = 0.6 + i * 4.1
    grp = s.shapes.add_group_shape()
    grp.name = f"Stat {i + 1}"
    box(grp, x, 5.6, 3.9, 1.15, fill=DCARD, line=None)
    text(grp, x + 0.25, 5.6, 1.1, 1.15, big, size=34, bold=True, color=GOLD if i == 0 else TEAL, anchor=MSO_ANCHOR.MIDDLE)
    text(grp, x + 1.35, 5.6, 2.4, 1.15, small, size=13, color=ICE, anchor=MSO_ANCHOR.MIDDLE)
    spids.append(grp.shape_id)
add_entrance_animations(s, spids)

# ---------- 5. check-and-fix ----------
s = content_slide("Quality control", "Every part is checked and fixed before a banker sees it",
                  "This is the inside of steps 6 and 7. Each part is checked by exact rules: every fact has a page citation, every "
                  "number matches the page it cites, nothing comes from comparables, risk wording follows SEBI drafting rules, and no "
                  "sentences are copied from comparables. Then an AI judge, using the Ragas library, checks that each claim is supported by "
                  "the pages it cites. The pass mark is 0.90 and can be changed. If a part fails, it goes back for a fix, up to two rounds. "
                  "If it still fails, it is marked FAILED and the export is blocked until the banker fixes it. The agent never fills a gap "
                  "with a guess: missing facts are written as [TBD] and listed for the company.")
# loop row
ry = 1.95
flow = [("Draft part", NAVY, WHITE), ("Exact checks", WHITE, NAVY), ("AI faithfulness score", WHITE, NAVY)]
for i, (t, f, c) in enumerate(flow):
    x = 0.6 + i * 2.55
    box(s, x, ry, 2.15, 0.9, fill=f, line=None if f == NAVY else RGBColor(0xB8, 0xC0, 0xCC))
    text(s, x, ry, 2.15, 0.9, t, size=15, bold=True, color=c, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    if i:
        line(s, x - 0.4, ry + 0.45, x, ry + 0.45, arrow=True)
dx = 0.6 + 3 * 2.55
line(s, dx - 0.4, ry + 0.45, dx, ry + 0.45, arrow=True)
d = box(s, dx, ry - 0.15, 1.75, 1.2, fill=TINT, line=BLUE, shape=MSO_SHAPE.DIAMOND)
text(s, dx, ry - 0.15, 1.75, 1.2, "All pass?", size=14, bold=True, color=NAVY, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
px = dx + 2.15
line(s, dx + 1.75, ry + 0.45, px, ry + 0.45, arrow=True)
text(s, dx + 1.8, ry + 0.08, 0.5, 0.3, "yes", size=12, color=MUTED)
box(s, px, ry, 2.0, 0.9, fill=WHITE, line=BLUE, line_w=2.25)
text(s, px, ry, 2.0, 0.9, "Ready for banker", size=15, bold=True, color=NAVY, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
# no branch: fix, back to checks
fy2 = ry + 1.55
line(s, dx + 0.875, ry + 1.05, dx + 0.875, fy2, arrow=True)
text(s, dx + 0.95, ry + 1.1, 0.4, 0.3, "no", size=12, color=MUTED)
box(s, dx - 0.2, fy2, 2.15, 0.8, fill=NAVY, line=None)
text(s, dx - 0.2, fy2, 2.15, 0.8, "Fix the flagged part", size=14, bold=True, color=WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
cx2 = 0.6 + 2.55 + 1.075
line(s, dx - 0.2, fy2 + 0.4, cx2, fy2 + 0.4, color=BLUE, dash=True)
line(s, cx2, fy2 + 0.4, cx2, ry + 0.9, arrow=True, color=BLUE, dash=True)
text(s, cx2 + 0.15, fy2 + 0.47, 3.6, 0.3, "checked again, up to 2 rounds", size=12, color=BLUE)
box(s, px, fy2, 2.0, 0.8, fill=WHITE, line=RUST, line_w=2.0)
text(s, px + 0.1, fy2, 1.8, 0.8, [[("Still failing: FAILED", {"bold": True, "color": RUST})], "export blocked"],
     size=13, color=NAVY, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
line(s, dx + 1.95, fy2 + 0.4, px, fy2 + 0.4, arrow=True, color=RUST)
# checks grid
checks = [("Page citation on every fact", "Each fact carries a label such as [D1 p.145] that points to its page."),
          ("Numbers match the source", "Every figure is matched against the page it cites."),
          ("Company facts only", "Comparable DRHPs are used for style. A fact taken from one is flagged."),
          ("SEBI drafting rules", "Risk Factors are checked for mitigation wording and heading form."),
          ("No copying", "Sentences too close to a comparable are flagged for rewriting."),
          ("Claims backed by pages", "An AI judge scores each part; the pass mark is 0.90.")]
for i, (h, b) in enumerate(checks):
    col, row = i % 3, i // 3
    x = 0.6 + col * 4.1
    y = 4.55 + row * 1.0
    box(s, x, y, 3.9, 0.88, fill=WHITE, line=LINE)
    text(s, x + 0.2, y + 0.09, 3.55, 0.3, h, size=14, bold=True)
    text(s, x + 0.2, y + 0.42, 3.6, 0.45, b, size=11, color=MUTED)
footer_note(s, [("No guesses: ", {"bold": True, "color": NAVY}),
                ("missing facts are written as [TBD] and listed for the company; errors stop the run with a clear message.", {})],
            y=6.65)

# ---------- 6. features ----------
s = content_slide("Features", "What the agent does",
                  "Six features. Two checkpoints keep the banker in control. All parts are drafted at the same time instead of one "
                  "after another. The check-and-fix loop runs without the banker. Every fact is cited to a printed page. Runs are saved "
                  "step by step, so they survive a closed browser, and a deal can be downloaded as a backup and restored. Every AI call is "
                  "logged with its tokens and cost, and runs are traced for monitoring, without any document text in the traces.")
feats = [("Two banker checkpoints", "Approve the facts before any drafting, and the draft before any export."),
         ("Parallel drafting", "Each Our Business subsection and risk group is drafted at the same time."),
         ("Automatic check and fix", "Flagged parts go back to the model and are checked again, up to 2 rounds."),
         ("Every fact cited", "Citations such as [D1 p.145] use printed page numbers the banker can open."),
         ("Pause, resume, back up", "Each step is saved. A deal can be downloaded and restored later."),
         ("Cost and quality tracked", "Calls, tokens, cost and scores are logged per run. No document text in logs.")]
for i, (h, b) in enumerate(feats):
    col, row = i % 3, i // 3
    x = 0.6 + col * 4.1
    y = 1.95 + row * 2.25
    box(s, x, y, 3.9, 2.0, fill=WHITE, line=LINE)
    circle_num(s, x + 0.3, y + 0.3, 0.55, str(i + 1))
    text(s, x + 1.05, y + 0.35, 2.7, 0.5, h, size=17, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    text(s, x + 0.3, y + 1.05, 3.35, 0.85, b, size=14, color=MUTED)

# ---------- 7. benefits ----------
s = content_slide("Benefits", "Faster first drafts, with control and a clear audit trail",
                  "Three benefits. Speed: the banker no longer reads 200-plus pages to build the first draft, or carries packs to Stylus "
                  "and back. We will measure the actual time saved on the first live run; the figures here are placeholders until then. "
                  "Control: the banker approves at both ends, nothing is guessed, and a failed part cannot be exported. Visibility: every "
                  "run records what was called, what it cost and how it scored.")
bens = [("Faster", "Banker time per first draft",
         ["No manual uploads or downloads.", "All parts drafted at once.", "Today: [__] person-days. With the agent: [__]."]),
        ("Safer", "Every sentence can be traced",
         ["Each fact cites its page.", "Missing facts become [TBD] gaps, never guesses.", "A failed part blocks the export."]),
        ("Visible", "Cost and quality per run",
         ["Calls, tokens and cost logged.", "Quality scores shown for each part.", "Cost per deal: [__], measured on the first live run."])]
for i, (h, sub, items) in enumerate(bens):
    x = 0.6 + i * 4.1
    box(s, x, 1.95, 3.9, 3.75, fill=WHITE, line=LINE)
    text(s, x + 0.35, 2.2, 3.3, 0.7, h, size=32, bold=True, color=BLUE)
    text(s, x + 0.35, 2.95, 3.3, 0.4, sub, size=15, bold=True, color=NAVY)
    paras = [[("•  ", {"color": BLUE, "bold": True}), (t, {})] for t in items]
    text(s, x + 0.35, 3.55, 3.3, 2.6, paras, size=14, color=MUTED, space_after=10)
footer_note(s, [("[__] ", {"bold": True, "color": NAVY}), ("figures are placeholders: they are filled in after the first live run, not estimated.", {})], y=5.95)

# ---------- 8. test run at a glance (real run, 25 Nov 2026) ----------
GREEN = RGBColor(0x1E, 0x8E, 0x5A)
s = content_slide("Agent test run · public documents", "One real run: documents to approved facts in 3 minutes",
                  "This is a real run of the agent on this laptop, starting from nothing, on public documents: the SBI Funds "
                  "Management annual report 2024-25 and two asset management DRHPs as comparables. In about three minutes it read "
                  "1,066 PDF pages, found Our Business and Risk Factors in both comparables at the right pages, pulled out 18 "
                  "consolidated figures and 11 risk triggers, and stopped for the banker at checkpoint 1 with nothing blocking. "
                  "After approval it planned 6 drafting parts, each well under the size limit, and sent one subsection back to the "
                  "banker because no company pages covered it: it did not guess. Steps 5 to 9 need the live AI model, which needs an "
                  "OpenAI key. One simulated piece: the blueprint used for planning came from a test stand-in, not the AI model.")
# inputs
docs_in = [("Company", "SBI Funds Management AR 2024-25", "104 PDF pages · 206 printed"),
           ("Comparable 1", "ICICI Prudential AMC DRHP", "439 PDF pages"),
           ("Comparable 2", "Second asset management DRHP", "523 PDF pages")]
for i, (k, n, p) in enumerate(docs_in):
    x = 0.6 + i * 4.1
    box(s, x, 1.8, 3.9, 0.78, fill=WHITE, line=LINE)
    text(s, x + 0.2, 1.84, 3.6, 0.72, [[(k.upper() + "  ", {"bold": True, "size": 10, "color": BLUE, "caps_spacing": 100}),
                                         (p, {"size": 11, "color": MUTED})], [(n, {"bold": True})]],
         size=13, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)
# progress track: steps 1-4 done, 5-9 waiting
TY2 = 3.15
xs = [0.95 + i * 1.425 for i in range(9)]
line(s, xs[0], TY2, xs[3], TY2, color=GREEN, width=5)
line(s, xs[3], TY2, xs[8], TY2, color=RGBColor(0xB8, 0xC0, 0xCC), width=3, dash=True)
short = ["Upload", "Sections + facts", "Checkpoint 1", "Plan", "Draft", "Check", "Fix", "Checkpoint 2", "Export"]
for i, lab in enumerate(short):
    done = i < 4
    if done:
        circle_num(s, xs[i] - 0.25, TY2 - 0.25, 0.5, "✓", fill=GREEN, size=16)
    else:
        box(s, xs[i] - 0.25, TY2 - 0.25, 0.5, 0.5, fill=WHITE, line=RGBColor(0xB8, 0xC0, 0xCC), line_w=1.5, shape=MSO_SHAPE.OVAL)
        text(s, xs[i] - 0.25, TY2 - 0.25, 0.5, 0.5, str(i + 1), size=13, bold=True, color=MUTED, align=PP_ALIGN.CENTER,
             anchor=MSO_ANCHOR.MIDDLE)
    text(s, xs[i] - 0.7, TY2 + 0.3, 1.4, 0.3, lab, size=11, bold=done, color=NAVY if done else MUTED, align=PP_ALIGN.CENTER)
# result cards
cards = [("1 · Upload", "1.4 s", "3 documents", "1,066 PDF pages registered"),
         ("2 · Sections + facts", "2 min 56 s", "4 of 4 sections", "start and end verified; 18 figures, 11 risk triggers"),
         ("3 · Checkpoint 1", "banker", "0 blockers", "PAT ₹2,54,015.32 lakhs [D1 p.145] shown for approval"),
         ("4 · Plan", "1.1 s", "6 parts", "all within the size limit; 1 subsection sent to the banker: no pages")]
for i, (h, t, big, small) in enumerate(cards):
    x = 0.6 + i * 2.5
    box(s, x, 4.0, 2.35, 2.35, fill=WHITE, line=LINE)
    text(s, x + 0.18, 4.12, 2.0, 0.3, h, size=11, bold=True, color=BLUE)
    chip = box(s, x + 0.18, 4.45, 1.0, 0.3, fill=TINT, line=None, radius=0.5)
    text(s, x + 0.18, 4.45, 1.0, 0.3, t, size=10, bold=True, color=NAVY, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    text(s, x + 0.18, 4.85, 2.05, 0.75, big, size=20, bold=True, color=NAVY)
    text(s, x + 0.18, 5.6, 2.05, 0.72, small, size=11, color=MUTED)
box(s, 10.6, 4.0, 2.13, 2.35, fill=TINT, line=ICE)
text(s, 10.78, 4.12, 1.85, 2.15, [[("5 – 9 · Waiting", {"bold": True, "color": BLUE, "size": 11})],
                                  [("Draft, check, fix, approve, export", {"bold": True, "size": 14})],
                                  [("Need the live AI model (OpenAI key). Time and cost: [__]", {"size": 11, "color": MUTED})]],
     size=13, color=NAVY, space_after=6)
footer_note(s, [("Real run on a laptop, 25 Nov 2026, starting with no saved data. ", {"bold": True, "color": NAVY}),
                ("No AI model was called; the planning blueprint came from a test stand-in.", {})], y=6.6)

# ---------- 9. deployment ----------
s = content_slide("Deployment", "How it would run, and how it is protected",
                  "This is the deployment. The code sits on GitHub. Every change runs the automatic tests and builds the app; only if "
                  "they pass does Render, a cloud host, deploy it. Bankers open it in a browser and must enter a password on every page. "
                  "The app calls OpenAI for drafting and sends run traces to Phoenix for monitoring, with no document text. Keys are typed "
                  "into Render, never stored in the code. Because Render and OpenAI are outside the bank, only public documents go in until "
                  "compliance approves otherwise.")
chain = [("GitHub", "code, no data or keys"), ("Automatic tests", "run on every change"),
         ("Render", "cloud host, deploys only if tests pass"), ("Banker’s browser", "password on every page")]
for i, (h, b) in enumerate(chain):
    x = 0.6 + i * 3.1
    f = NAVY if i == 2 else WHITE
    box(s, x, 1.95, 2.6, 1.25, fill=f, line=None if i == 2 else RGBColor(0xB8, 0xC0, 0xCC))
    text(s, x + 0.2, 2.05, 2.25, 1.05, [[(h, {"bold": True, "size": 16})], b], size=12,
         color=WHITE if i == 2 else NAVY, anchor=MSO_ANCHOR.MIDDLE)
    if i:
        line(s, x - 0.5, 2.575, x, 2.575, arrow=True)
rx = 0.6 + 2 * 3.1
for j, (h, b) in enumerate([("OpenAI API", "drafting and scoring"), ("Phoenix", "run traces, no document text")]):
    x = rx - 1.45 + j * 2.9
    line(s, rx + 1.3, 3.2, rx + 1.3, 3.55)
    box(s, x, 3.75, 2.6, 0.85, fill=TINT, line=ICE)
    text(s, x + 0.15, 3.75, 2.3, 0.85, [[(h, {"bold": True})], b], size=12, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)
line(s, rx - 0.15, 3.55, rx + 2.75, 3.55)
line(s, rx - 0.15, 3.55, rx - 0.15, 3.75, arrow=True)
line(s, rx + 2.75, 3.55, rx + 2.75, 3.75, arrow=True)
safe = [("Password on every page", "The app shows nothing until the password is entered."),
        ("Keys kept out of the code", "Typed into Render; never stored in GitHub."),
        ("Public documents only", "Until compliance approves cloud use for client documents."),
        ("Locked-down container", "Runs without admin rights; holds no deal data or keys.")]
for i, (h, b) in enumerate(safe):
    x = 0.6 + i * 3.08
    box(s, x, 4.95, 2.9, 1.35, fill=WHITE, line=LINE)
    text(s, x + 0.2, 5.05, 2.55, 0.35, h, size=14, bold=True)
    text(s, x + 0.2, 5.42, 2.55, 0.85, b, size=12, color=MUTED)

# ---------- 10. free tier ----------
s = content_slide("Demo hosting", "A free demo first, with known limits",
                  "We plan to start on Render's free plan, which is enough for a demo. Three limits matter. It goes to sleep after 15 "
                  "minutes with no use and takes up to a minute to wake. Its disk is wiped when it sleeps, so deals are deleted: the "
                  "banker downloads a backup zip and can restore it, and the run carries on from where it stopped. And it has 512 MB of "
                  "memory, which we will measure on the first full run. If any of these becomes a problem, the same app moves to a paid "
                  "plan with a permanent disk; that is a settings change, not a rebuild.")
lim = [("Sleeps when idle", "After 15 minutes with no use; up to about a minute to wake.", "Fine for a demo."),
       ("Deals deleted on sleep", "The disk is wiped when the app sleeps or restarts.", "Download a backup .zip; restoring it resumes the run."),
       ("512 MB memory", "Reading a 200-page report is the heaviest step.", "Measured on the first full run.")]
for i, (h, b, m) in enumerate(lim):
    y = 1.95 + i * 1.4
    box(s, 0.6, y, 12.13, 1.2, fill=WHITE, line=LINE)
    circle_num(s, 0.9, y + 0.33, 0.55, str(i + 1))
    text(s, 1.75, y + 0.15, 4.2, 0.9, [[(h, {"bold": True, "size": 17})], b], size=13, color=MUTED, anchor=MSO_ANCHOR.MIDDLE)
    box(s, 6.6, y + 0.2, 5.85, 0.8, fill=TINT, line=None)
    text(s, 6.85, y + 0.2, 5.5, 0.8, m, size=14, bold=True, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)
footer_note(s, [("Moving to a paid plan ", {"bold": True, "color": NAVY}),
                ("(always on, permanent disk) is a settings change, not a rebuild. Cost: [__] per month.", {})], y=6.3)

# ---------- 11. status ----------
s = content_slide("Where we are", "The agent is built; a live run is the next step",
                  "On the left is what is built and tested. On the right is what is left before a pilot: a first run with the live AI "
                  "model, which measures cost and quality; deploying the demo and running the full test deal on it; a benchmark against a "
                  "real published DRHP; a review of draft quality by bankers; and a compliance view on whether client documents may be used "
                  "with a cloud host and OpenAI.")
built = ["Document reading, section finding, facts", "Planning, parallel drafting, check and fix", "Two banker checkpoints and Word export",
         "Pause, resume, backup and restore", "Cost, quality and run tracking", "Password, container, automatic tests"]
todo = ["First run with the live AI model", "Deploy the demo and run the full test deal", "Benchmark against a real published DRHP",
        "Bankers review draft quality", "Compliance view on client documents"]
for i, (head, items, done) in enumerate([("Built and tested", built, True), ("Still to do", todo, False)]):
    x = 0.6 + i * 6.2
    box(s, x, 1.95, 5.93, 4.45, fill=WHITE if done else TINT, line=LINE if done else ICE)
    text(s, x + 0.35, 2.15, 5.2, 0.5, head, size=20, bold=True, color=BLUE if done else NAVY)
    for j, t in enumerate(items):
        y = 2.8 + j * 0.58
        if done:
            circle_num(s, x + 0.35, y + 0.04, 0.34, "✓", size=11)
        else:
            c = box(s, x + 0.35, y + 0.04, 0.34, 0.34, fill=WHITE, line=BLUE, line_w=1.5, shape=MSO_SHAPE.OVAL)
        text(s, x + 0.9, y, 4.9, 0.45, t, size=15, color=NAVY, anchor=MSO_ANCHOR.MIDDLE)

# ---------- 12. decisions ----------
s = prs.slides.add_slide(BLANK)
bg(s, NAVY)
text(s, 0.9, 0.9, 11, 0.4, [[("DECISIONS NEEDED", {"bold": True, "caps_spacing": 200})]], size=14, color=ICE)
text(s, 0.9, 1.3, 11.5, 0.9, "Four things to move to a live demo", size=36, bold=True, color=WHITE, name="Title")
asks = [("Approve a demo deployment", "Password-protected, on Render’s free plan."),
        ("Agree: public documents only", "Until compliance approves cloud use for client documents."),
        ("Fund an OpenAI API key", "Usage budget: [__] per month."),
        ("Two bankers’ time", "To review the first live drafts: [names]."),]
for i, (h, b) in enumerate(asks):
    col, row = i % 2, i // 2
    x = 0.9 + col * 5.9
    y = 2.7 + row * 1.75
    circle_num(s, x, y + 0.05, 0.6, str(i + 1), fill=BLUE)
    text(s, x + 0.85, y, 4.8, 0.5, h, size=20, bold=True, color=WHITE)
    text(s, x + 0.85, y + 0.5, 4.8, 0.8, b, size=15, color=ICE)
s.notes_slide.notes_text_frame.text = (
    "To close, four decisions. First, approval to deploy the demo, password-protected, on Render's free plan. Second, agreement "
    "that only public documents go into it until compliance has looked at cloud hosting for client documents. Third, an OpenAI API "
    "key with a monthly budget; fill in the figure before presenting. Fourth, time from two bankers to review the first live drafts.")

# ---------- 13. appendix: swimlane ----------
s = content_slide("Appendix · detailed flow", "Who does what: banker, agent and AI model",
                  "This is the full flow. The top lane is the banker, the middle lane is the agent, the bottom lane is the AI model. "
                  "Step 1: the banker uploads the annual report and other company documents, plus two or three comparable DRHPs. "
                  "Step 2: the agent finds the two sections in each comparable and extracts the key financials, risk signals and relevant pages. "
                  "Step 3 is checkpoint 1: the banker checks and approves those facts. "
                  "Step 4: the agent plans the work, one piece per subsection or risk group. "
                  "Step 5: the AI model drafts all pieces at the same time. "
                  "Step 6: the agent checks every piece. Step 7: anything flagged goes back to the model to fix, and is checked again, up to two rounds. "
                  "Step 8 is checkpoint 2: the banker reviews, edits or asks for revisions, and approves. "
                  "Step 9: the agent exports Word files. Nothing is exported before step 8.")
LX, LW = 0.6, 12.13
lanes = [("Banker", 1.8, WHITE), ("Agent", 3.15, RGBColor(0xEE, 0xF1, 0xF5)), ("AI model\n(OpenAI)", 4.5, TINT)]
LH = 1.3
for name, y, fill in lanes:
    box(s, LX, y, LW, LH, fill=fill, line=LINE, radius=0.04)
    text(s, LX + 0.2, y + 0.15, 1.2, 0.8, name, size=14, bold=True, color=NAVY)
lane_y = {"B": 1.8, "A": 3.15, "M": 4.5}
steps = [("B", "Upload documents"), ("A", "Find sections, extract facts"), ("B", "Checkpoint 1:\napprove facts"),
         ("A", "Plan the work"), ("M", "Draft all parts at once"), ("A", "Check and score each part"),
         ("M", "Fix flagged parts"), ("B", "Checkpoint 2:\nreview, approve"), ("A", "Export Word")]
X0, CW, BW, BH = 1.95, 1.18, 1.06, 0.98
geo = []
for i, (lane, label) in enumerate(steps):
    x = X0 + i * CW
    y = lane_y[lane] + (LH - BH) / 2
    is_cp = label.startswith("Checkpoint")
    if lane == "M":
        b = box(s, x, y, BW, BH, fill=NAVY, line=None, radius=0.06, name=f"Step {i+1}")
        num_c, txt_c = ICE, WHITE
    else:
        b = box(s, x, y, BW, BH, fill=WHITE, line=BLUE if is_cp else RGBColor(0xB8, 0xC0, 0xCC),
                line_w=2.25 if is_cp else 1.0, radius=0.06, name=f"Step {i+1}")
        num_c, txt_c = BLUE, NAVY
    text(s, x + 0.07, y + 0.06, BW - 0.12, BH - 0.1,
         [[(str(i + 1), {"bold": True, "color": num_c, "size": 13})], [(label.replace("\n", " "), {"color": txt_c})]],
         size=11, color=txt_c)
    geo.append((x, y))
def link(a, b, color=CONN, dash=False):
    (x1, y1), (x2, y2) = geo[a], geo[b]
    cx = x1 + BW / 2
    my = y2 + BH / 2
    line(s, cx, y1 + BH if y2 > y1 else y1, cx, my, color=color, dash=dash)
    line(s, cx, my, x2, my, arrow=True, color=color, dash=dash)


for a, b in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (5, 7), (7, 8)]:  # 6 -> 8 when passed, 6 -> 7 when flagged
    link(a, b)
kx, ky = geo[5]
fx, fy = geo[6]
text(s, kx + BW / 2 + 0.07, ky - 0.55, 0.9, 0.3, "passed", size=11, bold=True, color=MUTED)
text(s, kx + BW / 2 + 0.07, ky + BH + 0.25, 0.9, 0.3, "flagged", size=11, bold=True, color=MUTED)
# fixed part goes back to the checks (dashed)
rx2 = fx + BW + 0.12
line(s, fx + BW, fy + BH / 2, rx2, fy + BH / 2, color=BLUE, dash=True)
line(s, rx2, fy + BH / 2, rx2, ky + BH * 0.75, color=BLUE, dash=True)
line(s, rx2, ky + BH * 0.75, kx + BW, ky + BH * 0.75, arrow=True, color=BLUE, dash=True)
text(s, rx2 + 0.2, fy + 0.12, 2.0, 0.75, "Fixed parts are checked again, up to 2 rounds", size=11, color=BLUE)
footer_note(s, [("Nothing is exported until the banker approves at step 8. ", {"bold": True, "color": NAVY}),
                ("Every step is saved, so a run can be paused and resumed later.", {})], y=6.05)

prs.save(OUT)
print("saved", OUT, len(prs.slides), "slides")
