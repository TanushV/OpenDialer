"""Append-only human score reviews; the model assessment is never overwritten."""

from copy import deepcopy
from typing import Literal

from pydantic import Field, model_validator

from app.postcall.analysis import score_summary
from app.postcall.rubrics import Strict

RATINGS = {"SCORED", "N/A", "INSUFFICIENT_EVIDENCE"}


class ScoreReview(Strict):
    reason: str = Field(min_length=1, max_length=1000)
    status: Literal["disputed", "SCORED", "N/A", "INSUFFICIENT_EVIDENCE", "resolved"] = "disputed"
    dimension: str | None = Field(default=None, max_length=64)
    score: int | None = Field(default=None, ge=0, le=2, strict=True)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    # 0 names a scorecard no writer has materialized yet (see the analysis read).
    expected_version: int | None = Field(default=None, ge=0)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    resolves_review_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def consistent(self):
        if self.status != "disputed" and (self.expected_version is None or not self.request_id):
            raise ValueError("A rating or resolution requires the current version and request identity")
        if self.status in RATINGS and not self.dimension:
            raise ValueError("Select the reviewed dimension")
        if self.status == "SCORED":
            if self.score is None or not self.evidence:
                raise ValueError("A numerical rating requires a score and source references")
        elif self.score is not None:
            raise ValueError("Only an assessed dimension can have a numerical score")
        if self.status == "resolved" and not self.resolves_review_id:
            raise ValueError("Select the appeal being resolved")
        if self.status == "disputed" and self.resolves_review_id:
            raise ValueError("An appeal cannot resolve another appeal")
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("Evidence references must be unique")
        return self


def reviewed_view(card, reviews, revisions, live_ids):
    result = deepcopy(card)
    items = {item["dimension"]: item for item in result["items"]}
    resolutions = {r["resolves_review_id"]: r["id"] for r in reviews if r.get("resolves_review_id")}
    history = []
    for review in sorted(reviews, key=lambda r: (r.get("sequence", 0), r.get("at", ""), r["id"])):
        sources = review.get("evidence_revisions", {})
        applies = (
            review.get("rubric_version") == card["rubric_version"]
            and review.get("transcript_version") == card["transcript_version"]
            and all(revisions.get(key) == value for key, value in sources.items())
        )
        history.append(
            {
                **review,
                "applies": applies,
                "resolution_id": resolutions.get(review["id"]),
                "raw_evidence_status": "available" if set(sources) <= live_ids else "expired",
            }
        )
        dimension = review.get("dimension")
        if applies and dimension in items and review["status"] in RATINGS:
            items[dimension].update(
                score=review.get("score"),
                status=review["status"],
                explanation=review["reason"],
                evidence=review.get("evidence", []),
                source="human",
                reviewer_id=review["reviewer_id"],
                review_id=review["id"],
            )
    result["overall"] = score_summary(result["items"], list(items))
    return result, history
