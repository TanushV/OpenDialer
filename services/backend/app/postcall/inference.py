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


class Observation(StrictModel):
    concept: str = Field(min_length=3, max_length=120)
    approach: str | None = Field(default=None, min_length=3, max_length=160)
    evidence: list[str] = Field(min_length=1, max_length=8)
    quote: str = Field(min_length=1, max_length=600)


class TrackerMatch(StrictModel):
    tracker_id: str
    evidence: list[str] = Field(min_length=1, max_length=8)


class AnalysisOutput(StrictModel):
    summary: str = Field(max_length=1200)
    summary_evidence: list[str]
    problems: list[Evidence]
    objections: list[Evidence]
    commitments: list[Commitment]
    unknowns: list[str]
    score_items: list[ScoreItem]
    observations: list[Observation] = Field(default_factory=list, max_length=12)
    tracker_matches: list[TrackerMatch] = Field(default_factory=list, max_length=20)


def parse_analysis(
    data, segments, *, call_at=None, timezone=None, dimensions=None, rubric=None, trackers=None
):
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
    known_trackers = {t["id"] for t in trackers or []}
    matched = set()
    for item in result["tracker_matches"]:
        if item["tracker_id"] not in known_trackers or item["tracker_id"] in matched:
            raise ValueError("Tracker matches require unique configured identities")
        matched.add(item["tracker_id"])
        verify(item["evidence"])
    for item in [*result["problems"], *result["objections"]]:
        verify(item["evidence"], item["text"], item["quote"])
    from app.privacy import paraphrased

    segment_map = {s["segment_id"]: s for s in segments}
    seen = set()
    for item in result["observations"]:
        verify(item["evidence"], item["concept"] + " " + (item["approach"] or ""), item["quote"])
        key = tuple(sorted(set(item["evidence"])))
        if key in seen:
            raise ValueError("Combine observations sharing the same evidence into one bounded item")
        seen.add(key)
        sides = {segment_map[i].get("side") for i in item["evidence"]}
        if "remote" not in sides or (item["approach"] and "rep" not in sides):
            raise ValueError("Observed concepts need prospect evidence; approaches need actual rep speech")
        if not all(paraphrased(text, segments) for text in (item["concept"], item["approach"] or "")):
            raise ValueError("Retained observations must paraphrase, not copy raw dialogue")
    for index, item in enumerate(result["commitments"]):
        verify(item["evidence"], item["action"], item["verbatim_time_expression"])
        result["commitments"][index] = resolve_time(item, call_at=call_at, timezone=timezone)
    dimensions = DIMENSIONS if dimensions is None else dimensions
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


def analysis_review_request(
    data, segments, *, call_at=None, timezone=None, dimensions=None, rubric=None, trackers=None
):
    data = AnalysisOutput.model_validate(data).model_dump()
    fields = [{"field": "summary", "value": data["summary"], "evidence": data["summary_evidence"]}]
    for name in (
        "problems",
        "objections",
        "commitments",
        "unknowns",
        "score_items",
        "observations",
        "tracker_matches",
    ):
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
            "dimensions": DIMENSIONS if dimensions is None else dimensions,
            "rubric": rubric,
            "trackers": trackers or [],
            "evidence_scope": "Only still-live chunks; missing older conversation is unknown",
        },
    )


def validate_analysis(
    data, segments, *, call_at=None, timezone=None, dimensions=None, rubric=None, trackers=None, review=None
):
    result = parse_analysis(
        data,
        segments,
        call_at=call_at,
        timezone=timezone,
        dimensions=dimensions,
        rubric=rubric,
        trackers=trackers,
    )
    request = analysis_review_request(
        data,
        segments,
        call_at=call_at,
        timezone=timezone,
        dimensions=dimensions,
        rubric=rubric,
        trackers=trackers,
    )
    require_review(request, review)
    return result
