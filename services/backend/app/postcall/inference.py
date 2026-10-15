import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.postcall.analysis import DIMENSIONS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    text: str = Field(max_length=1000)
    evidence: list[str]
    quote: str


class Commitment(StrictModel):
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


def validate_analysis(data, segments):
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
    for item in result["commitments"]:
        verify(item["evidence"], item["action"], item["verbatim_time_expression"])
        # Exact appointment resolution remains a reviewed operation; vague expressions never acquire a guessed date.
        item["due_at"] = None
        item["needs_time_confirmation"] = True
    for item in result["score_items"]:
        if item["dimension"] not in DIMENSIONS:
            raise ValueError("Unknown scorecard dimension")
        if item["status"] == "SCORED":
            verify(item["evidence"])
            if item["score"] is None:
                raise ValueError("Score without an anchored rating")
    return result
