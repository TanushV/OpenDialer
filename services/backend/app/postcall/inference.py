import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.grounding import require_review, review_request
from app.postcall.analysis import DIMENSIONS
from app.postcall.dates import resolve_time


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    text: str = Field(max_length=1000)
    evidence: list[str]
    quote: str


class Commitment(StrictModel):
    kind: Literal["callback", "followup"] = "followup"
    owner: Literal["rep", "prospect", "unknown"]
    action: str = Field(max_length=1000)
    due_at: str | None
    verbatim_time_expression: str
    evidence: list[str]
    needs_time_confirmation: bool


class ScoreItem(StrictModel):
    dimension: str
    score: int | None = Field(ge=0, le=2)
    status: Literal["SCORED", "N/A", "INSUFFICIENT_EVIDENCE"]
    evidence: list[str]
    explanation: str


class AnalysisOutput(StrictModel):
    summary: str = Field(max_length=1200)
    summary_evidence: list[str]
    problems: list[Evidence]
    objections: list[Evidence]
    commitments: list[Commitment]
    unknowns: list[str]
    score_items: list[ScoreItem]


def parse_analysis(data, segments, *, call_at=None, timezone=None, dimensions=None):
    result = AnalysisOutput.model_validate(data).model_dump()
    sources = {
        s["segment_id"]: s["text"] for s in segments if s.get("is_final") and not s.get("system_audio")
    }

    def verify(ids, text="", quote=None):
        if not ids or not set(ids).issubset(sources):
            raise ValueError("Analysis references missing transcript evidence")
        source = " ".join(sources[i] for i in ids)
        if quote is not None and quote.casefold() not in source.casefold():
            raise ValueError("Quoted evidence is absent")
        if not set(re.findall(r"\d+(?:\.\d+)?", text)).issubset(set(re.findall(r"\d+(?:\.\d+)?", source))):
            raise ValueError("Analysis invented a number or date")

    verify(result["summary_evidence"], result["summary"])
    for item in [*result["problems"], *result["objections"]]:
        verify(item["evidence"], item["text"], item["quote"])
    for index, item in enumerate(result["commitments"]):
        verify(item["evidence"], item["action"], item["verbatim_time_expression"])
        result["commitments"][index] = resolve_time(item, call_at=call_at, timezone=timezone)
    dimensions = dimensions or DIMENSIONS
    actual = [item["dimension"] for item in result["score_items"]]
    if actual and (len(actual) != len(dimensions) or set(actual) != set(dimensions)):
        raise ValueError("A complete rubric with unique configured dimensions is required")
    for item in result["score_items"]:
        if item["status"] == "SCORED":
            verify(item["evidence"])
            if item["score"] is None:
                raise ValueError("Score without an anchored rating")
        elif item["score"] is not None:
            raise ValueError("An unscored item cannot carry a numerical rating")
    return result


def analysis_review_request(data, segments, *, call_at=None, timezone=None, dimensions=None):
    data = AnalysisOutput.model_validate(data).model_dump()
    fields = [{"field": "summary", "value": data["summary"], "evidence": data["summary_evidence"]}]
    for name in ("problems", "objections", "commitments", "unknowns", "score_items"):
        for index, item in enumerate(data[name]):
            fields.append(
                {
                    "field": f"{name}/{index}",
                    "value": item,
                    "evidence": item.get("evidence", []) if isinstance(item, dict) else [],
                }
            )
    sources = [
        {"id": s["segment_id"], **{k: s.get(k) for k in ("text", "revision", "side", "speaker_id")}}
        for s in segments
        if s.get("is_final") and not s.get("system_audio")
    ]
    return review_request(
        "analysis",
        fields,
        sources,
        {
            "call_at": call_at.isoformat() if call_at else None,
            "timezone": timezone,
            "dimensions": dimensions or DIMENSIONS,
            "evidence_scope": "Only still-live chunks; missing older conversation is unknown",
        },
    )


def validate_analysis(data, segments, *, call_at=None, timezone=None, dimensions=None, review=None):
    result = parse_analysis(data, segments, call_at=call_at, timezone=timezone, dimensions=dimensions)
    request = analysis_review_request(
        data, segments, call_at=call_at, timezone=timezone, dimensions=dimensions
    )
    require_review(request, review)
    return result
