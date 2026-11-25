"""Checks on drafted text, run on every save in the Section Workspace.

  * numbers      - every amount / percentage must appear on a page cited in the same
                   paragraph, or among the verified figures / ratios of the deal
  * citations    - paragraphs with numbers but no citation; citations to unknown pages;
                   comparable pages cited as sources
  * copying      - sentences too close to a comparable prospectus sentence
  * risk factors - mitigation language, heading form, required risks not covered
  * completeness - [TBD] placeholders, empty items, blueprint subsections missing
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel
from rapidfuzz import fuzz, process

from .company_docs import Unit, find_unit
from .financials import Indicators
from .replies import CITE_BLOCK_RX, CITE_RX, TBD_RX

NUM_RX = re.compile(r"(?<![\w.])\(?-?(?:\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+\.\d+|\d+)\)?(?:\s?%)?")
MITIGATION_RX = re.compile(
    r"\bhowever,? we believe\b|\bwe believe (that )?(this|these|such) (risk|risks)\b|"
    r"\bwe have not (experienced|faced|encountered)\b|\badequate(ly)? (measures|insurance|controls|safeguards)\b|"
    r"\bwe have (implemented|taken|put in place) (adequate|sufficient|robust|appropriate)\b|\bmitigat(e|es|ed|ing|ion)\b",
    re.I)
ADVERSE_RX = re.compile(r"advers|affect|impact|harm|disrupt|declin|loss|reduc|penalt|liabilit", re.I)


class Issue(BaseModel):
    kind: str          # number | citation | copy | rf_rule | completeness
    severity: str      # high | medium | low
    message: str
    excerpt: str = ""


# --------------------------------------------------------------------------- numbers
def num_key(s: str) -> Optional[str]:
    """Normalise '4,06,332.14' / '406,332.140' / '(1,250.22)' / '24.10 %' to a comparable key."""
    t = re.sub(r"[,\s%()\-]", "", s)
    if not t or not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    whole, _, frac = t.partition(".")
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{frac}" if frac else whole


def numbers_to_check(text: str) -> List[str]:
    """Numbers in drafted text that must be backed by a source (years, small counts,
    IDs and citation page numbers excluded)."""
    clean = TBD_RX.sub(" ", CITE_BLOCK_RX.sub(" ", text))
    clean = re.sub(r"\b(?:OB|RF)\d+\b", " ", clean)
    out = []
    for m in NUM_RX.finditer(clean):
        raw = m.group(0).strip()
        k = num_key(raw)
        if k is None:
            continue
        is_pct = raw.endswith("%")
        if "." not in k and not is_pct:
            v = int(k)
            if v < 100 or (1990 <= v <= 2040 and "," not in raw):   # small counts / years
                continue
        out.append(raw)
    return out


def page_number_keys(unit: Unit) -> Set[str]:
    return {k for m in NUM_RX.finditer(unit.text) if (k := num_key(m.group(0)))}


def figure_keys(ind: Optional[Indicators]) -> Set[str]:
    """Keys for verified figures (as printed, rounded) and derived ratios (as % or x)."""
    keys: Set[str] = set()
    if not ind:
        return keys
    for f in ind.figures.values():
        for v in f.values:
            if v is None:
                continue
            for d in (2, 1, 0):
                keys.add(num_key(f"{abs(v):.{d}f}"))
    for k, vals in ind.derived.items():
        for v in vals:
            if v is None:
                continue
            if k.endswith(("margin", "growth", "roe")) or k == "roe":
                for d in (2, 1, 0):
                    keys.add(num_key(f"{abs(v) * 100:.{d}f}"))
            for d in (2, 1, 0):
                keys.add(num_key(f"{abs(v):.{d}f}"))
    keys.discard(None)
    return keys


def blocks_with_cites(text: str) -> List[Tuple[str, List[str]]]:
    """Split text into paragraphs / table rows with the citations each one may rely on.
    A table (or its rows) without citations borrows them from a following 'Source:' line."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: List[Tuple[str, List[str]]] = []
    for i, p in enumerate(paras):
        cites = [f"{d} {k}.{n}" for blk in CITE_BLOCK_RX.findall(p) for d, k, n in CITE_RX.findall(blk)]
        if not cites and p.lstrip().startswith("|") and i + 1 < len(paras):
            nxt = paras[i + 1]
            if re.match(r"\s*(\*\*)?(source|note)", nxt, re.I) or len(nxt) < 200:
                cites = [f"{d} {k}.{n}" for blk in CITE_BLOCK_RX.findall(nxt) for d, k, n in CITE_RX.findall(blk)]
        if p.lstrip().startswith("|"):
            # each table row: use row cites if any, else the table's cites
            for row in p.splitlines():
                rc = [f"{d} {k}.{n}" for blk in CITE_BLOCK_RX.findall(row) for d, k, n in CITE_RX.findall(blk)]
                out.append((row, rc or cites))
        else:
            out.append((p, cites))
    return out


def check_numbers(text: str, units: List[Unit], ind: Optional[Indicators]) -> List[Issue]:
    issues: List[Issue] = []
    fig = figure_keys(ind)
    cache: Dict[str, Set[str]] = {}
    all_cites = {f"{d} {k}.{n}" for blk in CITE_BLOCK_RX.findall(text) for d, k, n in CITE_RX.findall(blk)}
    for block, cites in blocks_with_cites(text):
        nums = numbers_to_check(block)
        if not nums:
            continue
        if not cites:
            issues.append(Issue(kind="citation", severity="high",
                                message=f"Numbers without a citation: {', '.join(nums[:6])}",
                                excerpt=block.strip()[:220]))
            continue
        keys = set()
        for c in cites:
            if c not in cache:
                u = find_unit(units, c)
                cache[c] = page_number_keys(u) if u else set()
            keys |= cache[c]
        missing = [n for n in nums if num_key(n) not in keys and num_key(n) not in fig]
        if missing:
            # found on another page cited elsewhere in this item? -> weaker warning
            other = set()
            for c in all_cites - set(cites):
                if c not in cache:
                    u = find_unit(units, c)
                    cache[c] = page_number_keys(u) if u else set()
                other |= cache[c]
            hard = [n for n in missing if num_key(n) not in other]
            soft = [n for n in missing if num_key(n) in other]
            if hard:
                issues.append(Issue(kind="number", severity="high",
                                    message=f"Not found on the cited page(s) {', '.join(cites)}: {', '.join(hard[:8])}",
                                    excerpt=block.strip()[:220]))
            if soft:
                issues.append(Issue(kind="number", severity="low",
                                    message=f"Found on a page cited elsewhere in this item, not in this paragraph: {', '.join(soft[:8])}",
                                    excerpt=block.strip()[:220]))
    return issues


# --------------------------------------------------------------------------- copying
SENT_SPLIT = re.compile(r"(?<=[.;:])\s+(?=[A-Z(\"'])")


def sentences(text: str, min_len: int = 60) -> List[str]:
    text = CITE_BLOCK_RX.sub("", text)
    out = []
    for para in re.split(r"\n+", text):
        if para.lstrip().startswith("|"):
            continue
        for s in SENT_SPLIT.split(para):
            s = re.sub(r"\s+", " ", s).strip(" *#")
            if len(s) >= min_len:
                out.append(s)
    return out


def check_copying(text: str, comparable_sentences: List[str], cutoff: int = 85) -> List[Issue]:
    issues = []
    if not comparable_sentences:
        return issues
    for s in sentences(text):
        m = process.extractOne(s, comparable_sentences, scorer=fuzz.ratio, score_cutoff=cutoff)
        if m:
            issues.append(Issue(kind="copy", severity="medium",
                                message=f"{m[1]:.0f}% similar to a comparable prospectus sentence",
                                excerpt=f"Draft: {s[:160]}\nComparable: {m[0][:160]}"))
    return issues


# --------------------------------------------------------------------------- risk factors
def check_rf_item(title: str, text: str) -> List[Issue]:
    issues = []
    for m in MITIGATION_RX.finditer(text):
        start = max(0, m.start() - 60)
        issues.append(Issue(kind="rf_rule", severity="medium",
                            message=f"Mitigation / softening language: '{m.group(0)}'",
                            excerpt=text[start:m.end() + 60].replace("\n", " ")))
    t = title.strip().strip("*")
    if len(t) < 40 or not t.endswith((".", "?")):
        issues.append(Issue(kind="rf_rule", severity="low",
                            message="Heading should be one full sentence ending with a full stop.", excerpt=t))
    if not ADVERSE_RX.search(t):
        issues.append(Issue(kind="rf_rule", severity="low",
                            message="Heading should state the possible adverse effect.", excerpt=t))
    return issues


# Key terms per risk trigger (key prefix -> regex). A risk factor whose heading or opening
# paragraph contains them covers that trigger, however the heading is worded.
COVER_TERMS: List[Tuple[str, str]] = [
    ("contingent_liabilities", r"contingent liabilit"),
    ("related_party", r"related[- ]part"),
    ("litigation", r"legal proceedings|litigation|proceedings (pending|involving|against)|show[- ]cause|"
                   r"pending (cases|proceedings)"),
    ("regulatory_action", r"penalt|regulatory action|inspection|adverse orders?|adjudicat|deficiency letter"),
    ("concentration", r"(limited|small) number of|concentrat|significant (portion|proportion|share|part) of|"
                      r"top (ten|10|five|5|twenty|20)\b|single largest|depend\w* (on|upon) (a few|certain|our top)"),
    ("attrition", r"key (managerial )?personnel|senior management|attrition|attract(,| and) (or )?retain"),
    ("brand_parent", r"\bbrand|promoter|parent"),
    ("auditor_qualification", r"qualifi|emphasis of matter|adverse (remark|opinion)|auditor"),
    ("revised_financials", r"revis|restat"),
    ("unspent_csr", r"\bcsr\b|corporate social responsibility"),
    ("statutory_dues", r"statutory dues"),
    ("cyber", r"cyber|data (breach|privacy|security)|information (technology|security)|\bit systems?\b"),
    ("regulated_industry", r"regulat"),
    ("negative_ocf", r"negative (operating )?cash flows?|cash flows? (from|used in) operati"),
    ("loss_", r"\blosses\b|net loss|incurred (a )?loss"),
    ("revenue_ops_decline", r"(decline|decrease|fall)\w* in (our )?(total )?revenue"),
    ("pat_decline", r"(decline|decrease|fall)\w* in (our )?(net )?profit"),
    ("high_leverage", r"indebtedness|borrowings?|\bdebt\b|covenant"),
    ("offer_price", r"offer price"),
    ("no_prior_market", r"no (prior )?(public )?market|not been any (prior )?public market|active (trading )?market"),
    ("ofs_proceeds", r"offer for sale"),
]
_STOP = set("a an the of and or to in on for our we us its is are be by with from may could which that this "
            "has have had any not their there been will can".split())


def _opening(text: str, limit: int = 500) -> str:
    """The first paragraph of a risk factor (where DRHPs state the risk)."""
    para = re.split(r"\n\s*\n", (text or "").strip(), maxsplit=1)[0]
    return para[:limit]


def _content_stems(s: str) -> Set[str]:
    return {w[:5] for w in re.findall(r"[a-z]+", s.lower()) if w not in _STOP and len(w) > 2}


def _covers(key: str, title: str, heading: str, text: str, cutoff: int) -> bool:
    if fuzz.token_set_ratio(title, heading) >= cutoff:
        return True
    where = f"{heading}\n{_opening(text)}"
    for prefix, rx in COVER_TERMS:
        if key.startswith(prefix):
            return bool(re.search(rx, where, re.I))
    # a risk the banker added on page 3: most of its title's words appear in the heading or opening
    want = _content_stems(title)
    return bool(want) and len(want & _content_stems(where)) / len(want) >= 0.6


def uncovered_required(items_titles: List[str], items_texts: List[str], required: List[Tuple[str, str]],
                       cutoff: int = 60) -> List[Issue]:
    """required = [(key, title)]. A required risk is covered if some risk factor's heading is close
    to its title, or the heading / opening paragraph contains the risk's key terms (COVER_TERMS)."""
    issues = []
    texts = list(items_texts) + [""] * (len(items_titles) - len(items_texts))
    for key, title in required:
        if not any(_covers(key, title, h, t, cutoff) for h, t in zip(items_titles, texts)):
            issues.append(Issue(kind="completeness", severity="high",
                                message=f"Required risk not covered: {title}", excerpt=key))
    return issues


# --------------------------------------------------------------------------- entry points
def check_item(section: str, item, units: List[Unit], ind: Optional[Indicators],
               comparable_sentences: List[str]) -> List[Issue]:
    issues: List[Issue] = []
    if not item.text.strip():
        return [Issue(kind="completeness", severity="high", message="Empty text.")]
    issues += check_numbers(item.text, units, ind)
    for blk in CITE_BLOCK_RX.findall(item.text):
        for d, k, n in CITE_RX.findall(blk):
            c = f"{d} {k}.{n}"
            if d.startswith("C"):
                issues.append(Issue(kind="citation", severity="high",
                                    message=f"Comparable page cited as a source: [{c}]"))
            elif not find_unit(units, c):
                issues.append(Issue(kind="citation", severity="high", message=f"Citation to an unknown page: [{c}]"))
    tbd = TBD_RX.findall(item.text)
    if tbd:
        issues.append(Issue(kind="completeness", severity="low", message=f"{len(tbd)} [TBD] placeholder(s)",
                            excerpt="; ".join(tbd[:4])))
    issues += check_copying(item.text, comparable_sentences)
    if section == "risk_factors":
        issues += check_rf_item(item.title, item.text)
    # de-duplicate identical messages
    seen, out = set(), []
    for i in issues:
        k = (i.kind, i.message)
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


@lru_cache(maxsize=8)
def _comparable_sentences_cached(deal_id: str, section: str, stamp: str) -> Tuple[str, ...]:
    from . import store
    out: List[str] = []
    for (doc_id, key), sec in store.load_extractions(deal_id).items():
        if key == section:
            out += sentences(" ".join(" ".join(p.lines) for p in sec.pages))
    return tuple(out)


def comparable_sentences(deal_id: str, section: str) -> List[str]:
    from . import store
    ext = store.deal_dir(deal_id) / "extracted"
    stamp = str(max((f.stat().st_mtime for f in ext.glob("*.json")), default=0)) if ext.exists() else "0"
    return list(_comparable_sentences_cached(deal_id, section, stamp))
