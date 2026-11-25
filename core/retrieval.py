"""Offline BM25 retrieval over company-document units (no embeddings, no network).

Maps each "Our Business" subsection and each risk theme to the most relevant pages,
with part-based boosts (e.g. business narrative parts for Our Business).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from .company_docs import Unit, load_config

STOPWORDS = set("""a an and are as at be by for from has have in is it its of on or our that the
their this to was were will with we which during year also such any all other been not more than
under these those per into its such can may""".split())
TOKEN = re.compile(r"[a-z][a-z0-9&]+")


def tokenize(text: str) -> List[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


class BM25:
    """Okapi BM25 (k1=1.5, b=0.75)."""

    def __init__(self, docs: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 0.0
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: List[str]) -> List[float]:
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for q in query:
                f = tf.get(q)
                if not f:
                    continue
                s += self.idf[q] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


def pick_sector_pack(sector: str, cfg: Optional[dict] = None) -> Optional[str]:
    """Name of the sector pack best matching the deal's sector text (or None)."""
    cfg = cfg or load_config()
    if not sector:
        return None
    best, best_score = None, 0.0
    for name, pack in cfg.get("sector_packs", {}).items():
        for m in pack.get("match", []):
            s = fuzz.partial_ratio(m.lower(), sector.lower())
            if s > best_score:
                best, best_score = name, s
    return best if best_score >= 80 else None


def build_queries(kind: str, sector_pack: Optional[str], cfg: Optional[dict] = None) -> Dict[str, dict]:
    """{key: {title, terms}} for kind 'our_business' or 'risk_themes', with sector terms merged."""
    cfg = cfg or load_config()
    base = {k: dict(v) for k, v in cfg[kind].items()}
    if sector_pack:
        extra = cfg["sector_packs"][sector_pack].get(kind, {})
        for k, terms in extra.items():
            if k in base:
                base[k]["terms"] = f'{base[k]["terms"]} {terms}'
    return base


class UnitIndex:
    def __init__(self, units: List[Unit]):
        self.units = units
        self.bm25 = BM25([tokenize(u.text) for u in units])

    def search(self, terms: str, top_k: int = 6, boost_parts: Optional[List[str]] = None,
               boost: float = 1.5, penalize_prefixes: Optional[List[str]] = None,
               penalty: float = 0.4, min_words: int = 25) -> List[Tuple[Unit, float]]:
        scores = self.bm25.scores(tokenize(terms))
        ranked = []
        for u, s in zip(self.units, scores):
            if s <= 0 or len(u.text.split()) < min_words:
                continue
            if boost_parts and u.part in boost_parts:
                s *= boost
            if penalize_prefixes and any(u.part.startswith(p) for p in penalize_prefixes):
                s *= penalty
            ranked.append((u, s))
        ranked.sort(key=lambda x: -x[1])
        return ranked[:top_k]


def map_subsections(units: List[Unit], sector: str = "", top_k: int = 6) -> dict:
    """Map Our Business subsections and risk themes to their most relevant units.

    Returns {"sector_pack", "our_business": {key: {title, hits:[{cite, location, part, score}]}},
             "risk_themes": {...}}
    """
    cfg = load_config()
    pack = pick_sector_pack(sector, cfg)
    idx = UnitIndex(units)
    out = {"sector_pack": pack}
    # audit / statutory boilerplate rarely describes the business
    noisy = ["Auditor's Report", "Secretarial Audit", "Notice of AGM"]
    # with consolidated statements present, standalone notes mostly duplicate them
    if any(u.part.endswith("Consolidated") for u in units):
        noisy += ["Notes - Standalone", "Financial Statements - Standalone"]
    for kind, parts in (("our_business", cfg["business_parts"]), ("risk_themes", None)):
        res = {}
        for key, q in build_queries(kind, pack, cfg).items():
            penal = noisy + (["Notes", "Financial Statements"]
                             if kind == "our_business" and key not in ("properties_ip", "financial_perf") else [])
            hits = idx.search(q["terms"], top_k=top_k, boost_parts=parts, penalize_prefixes=penal)
            top = hits[0][1] if hits else 1.0
            res[key] = {"title": q["title"], "hits": [
                {"cite": u.cite, "doc_id": u.doc_id, "unit_no": u.unit_no, "location": u.location,
                 "part": u.part, "score": round(s / top, 3)} for u, s in hits]}
        out[kind] = res
    return out
