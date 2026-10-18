"""Append-only human score reviews; the model assessment is never overwritten."""

import hashlib
import json
from copy import deepcopy
from typing import Literal

from fastapi import Depends
from pydantic import Field, model_validator

from app.db import audit, doc, emit, get, put, update
from app.postcall.analysis import score_summary
from app.postcall.rubrics import Strict, effective
from app.service import fail

RATINGS = {"SCORED", "N/A", "INSUFFICIENT_EVIDENCE"}


class ScoreReview(Strict):
    reason: str = Field(min_length=1, max_length=1000)
    status: Literal["disputed", "SCORED", "N/A", "INSUFFICIENT_EVIDENCE", "resolved"] = "disputed"
    dimension: str | None = Field(default=None, max_length=64)
    score: int | None = Field(default=None, ge=0, le=2, strict=True)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    expected_version: int | None = Field(default=None, ge=1)
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


def bind_api(app, service, principal):
    @app.post("/v1/scorecards/{scorecard_id}/review")
    def save_review(scorecard_id: str, data: ScoreReview, p=Depends(principal)):
        fingerprint = hashlib.sha256(json.dumps(data.model_dump(), sort_keys=True).encode()).hexdigest()
        key = f"score-review:{p['user_id']}:{data.request_id}" if data.request_id else None
        with service.db.transaction() as s:
            card = service.required(s, "scorecards", scorecard_id, p)
            call = service.required(s, "call_attempts", card.data["call_id"], p)
            s.refresh(call, with_for_update=True)
            s.refresh(card, with_for_update=True)
            previous = service._by_key(s, p, "reviews", key) if key else None
            if previous:
                if (
                    previous.data.get("fingerprint") != fingerprint
                    or previous.data["scorecard_id"] != card.id
                ):
                    fail(409, "This review request was already used with different content")
                return doc(previous)
            if (
                (data.expected_version is not None and data.expected_version != card.version)
                or card.data.get("stale")
                or card.data["transcript_version"] != call.data["transcript_version"]
                or card.data["rubric_version"] != effective(call.data)["rubric_version"]
            ):
                fail(409, "The assessment changed; reload before reviewing")
            dimensions = {item["dimension"] for item in card.data["items"]}
            if data.dimension is not None and data.dimension not in dimensions:
                fail(422, "Review dimension is not in this call's rubric")
            revisions = {}
            for identity in data.evidence:
                segment = service._by_key(s, p, "transcript_segments", call.id + ":" + identity)
                if not segment or not segment.data.get("is_final") or segment.data.get("system_audio"):
                    fail(422, "Review evidence does not exist in this call")
                revisions[identity] = segment.data.get("revision", 1)
            if data.resolves_review_id:
                appeal = get(s, "reviews", data.resolves_review_id, p["tenant_id"])
                if (
                    not appeal
                    or appeal.seller_id != p["seller_id"]
                    or appeal.data.get("scorecard_id") != card.id
                    or appeal.data["status"] != "disputed"
                ):
                    fail(422, "Select an appeal belonging to this scorecard")
                if appeal.data.get("resolution_id"):
                    fail(409, "This appeal already has a resolution")
                if data.status in RATINGS and appeal.data.get("dimension") not in {None, data.dimension}:
                    fail(422, "The rating must address the appealed dimension")
            else:
                appeal = None
            sequence = card.data.get("review_sequence", 0) + 1
            obj = put(
                s,
                "reviews",
                p["tenant_id"],
                p["seller_id"],
                {
                    **data.model_dump(exclude={"expected_version", "request_id"}),
                    "scorecard_id": card.id,
                    "rubric_version": card.data["rubric_version"],
                    "transcript_version": card.data["transcript_version"],
                    "evidence_revisions": revisions,
                    "reviewer_id": p["user_id"],
                    "reviewer_role": p["role"],
                    "sequence": sequence,
                    "fingerprint": fingerprint,
                    "at": service.clock().isoformat(),
                },
                key=key,
                now=service.clock(),
            )
            if appeal:
                update(appeal, resolution_id=obj.id)
            update(card, review_sequence=sequence)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "scorecard.reviewed",
                card.id,
                service.clock(),
                {"review_id": obj.id, "status": data.status},
            )
            emit(s, p["tenant_id"], "scorecard.updated", card, service.clock())
            return doc(obj)
