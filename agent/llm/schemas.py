"""Strict output schemas for OpenAI structured output.

Every field is required and no extra keys are allowed, so OpenAI's strict mode accepts the schema
and the reply either matches it exactly or the call fails. There is no repair step: the tolerant
parser in `core/replies.py` is only for Stylus files in the Streamlit app (LANGGRAPH_PLAN.md §2).
"""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.blueprint import Blueprint, OBSubsection, OurBusinessBP, RFTheme, RiskFactorsBP, attach_map_keys

RFGroup = Literal["Internal Risks", "External Risks", "Risks Relating to the Offer"]
RF_GROUPS: List[str] = ["Internal Risks", "External Risks", "Risks Relating to the Offer"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OBSubsectionOut(_Strict):
    id: str = Field(pattern=r"^OB\d{2}$", description="OB01, OB02, ... in reading order")
    title: str = Field(min_length=1)
    purpose: str
    target_words: int = Field(ge=1, description="target length in words, a single whole number")
    content_elements: List[str]
    tables: List[str]
    example_ref: str = Field(description="comparable page that shows this subsection, e.g. 'C1 p.160'")


class OurBusinessOut(_Strict):
    style_notes: str
    subsections: List[OBSubsectionOut]


class RFThemeOut(_Strict):
    id: str = Field(pattern=r"^RF\d{2}$", description="RF01, RF02, ...")
    group: RFGroup
    theme: str = Field(min_length=1)
    heading_pattern: str
    quantification: str
    example_ref: str


class RiskFactorsOut(_Strict):
    style_notes: str
    intro_pattern: str
    groups: List[RFGroup]
    themes: List[RFThemeOut]


class BlueprintOut(_Strict):
    """What call 1 (IPO-01 rules) must return."""
    our_business: OurBusinessOut
    risk_factors: RiskFactorsOut


class BlueprintInvalid(ValueError):
    """The model's blueprint is unusable. The run stops; no default blueprint is used."""


def blueprint_problems(out: BlueprintOut) -> List[str]:
    """Rules the JSON schema can't express. Empty list = usable."""
    probs: List[str] = []
    subs, themes = out.our_business.subsections, out.risk_factors.themes
    if not subs:
        probs.append("our_business.subsections is empty (at least 1 subsection is needed)")
    if not themes:
        probs.append("risk_factors.themes is empty (at least 1 risk theme is needed)")
    for label, ids in (("subsection", [s.id for s in subs]), ("theme", [t.id for t in themes])):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            probs.append(f"duplicate {label} id(s): {', '.join(dup)}")
    missing = sorted({t.group for t in themes} - set(out.risk_factors.groups))
    if missing:
        probs.append(f"theme group(s) not listed in risk_factors.groups: {', '.join(missing)}")
    return probs


def to_blueprint(out: BlueprintOut, source: str) -> Blueprint:
    """Checked LLM output -> the app's Blueprint (with page-mapping keys attached).
    Raises BlueprintInvalid listing every problem."""
    probs = blueprint_problems(out)
    if probs:
        raise BlueprintInvalid("Blueprint from the model is not usable: " + "; ".join(probs))
    ob, rf = out.our_business, out.risk_factors
    bp = Blueprint(
        source=source,
        our_business=OurBusinessBP(style_notes=ob.style_notes, subsections=[
            OBSubsection(id=s.id, title=s.title, purpose=s.purpose, target_words=s.target_words,
                         content_elements=list(s.content_elements), tables=list(s.tables),
                         example_ref=s.example_ref) for s in ob.subsections]),
        risk_factors=RiskFactorsBP(style_notes=rf.style_notes, intro_pattern=rf.intro_pattern,
                                   groups=list(rf.groups), themes=[
            RFTheme(id=t.id, group=t.group, theme=t.theme, heading_pattern=t.heading_pattern,
                    quantification=t.quantification, example_ref=t.example_ref) for t in rf.themes]),
    )
    return attach_map_keys(bp)


def from_blueprint(bp: Blueprint) -> BlueprintOut:
    """App Blueprint -> output schema (used to test the round trip and to show a blueprint to the model).
    Raises pydantic.ValidationError if the Blueprint has values the strict schema doesn't allow
    (e.g. a subsection without target_words)."""
    return BlueprintOut.model_validate({
        "our_business": {
            "style_notes": bp.our_business.style_notes,
            "subsections": [{"id": s.id, "title": s.title, "purpose": s.purpose, "target_words": s.target_words,
                             "content_elements": s.content_elements, "tables": s.tables,
                             "example_ref": s.example_ref} for s in bp.our_business.subsections],
        },
        "risk_factors": {
            "style_notes": bp.risk_factors.style_notes, "intro_pattern": bp.risk_factors.intro_pattern,
            "groups": bp.risk_factors.groups,
            "themes": [{"id": t.id, "group": t.group, "theme": t.theme, "heading_pattern": t.heading_pattern,
                        "quantification": t.quantification, "example_ref": t.example_ref}
                       for t in bp.risk_factors.themes],
        },
    })


def strict_json_schema(model: type = BlueprintOut) -> dict:
    """The JSON schema exactly as the OpenAI SDK sends it in strict mode."""
    from openai.lib._pydantic import to_strict_json_schema
    return to_strict_json_schema(model)


def model_fields_optional(model: type) -> List[str]:
    """Names of fields with a default (none are allowed in a strict output model). For tests."""
    out: List[str] = []
    for name, f in model.model_fields.items():
        if not f.is_required():
            out.append(f"{model.__name__}.{name}")
    return out


__all__ = ["BlueprintOut", "OurBusinessOut", "OBSubsectionOut", "RiskFactorsOut", "RFThemeOut", "RF_GROUPS",
           "BlueprintInvalid", "blueprint_problems", "to_blueprint", "from_blueprint", "strict_json_schema",
           "model_fields_optional"]
