"""Provider-neutral conversation transport. Private text never belongs in receipts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AuditSource(StrictModel):
    kind: Literal[
        "segment", "observation", "fact", "knowledge", "human_note", "commitment", "outcome", "card"
    ]
    id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=0)


class FieldAudit(StrictModel):
    field: str = Field(min_length=1, max_length=160)
    classification: Literal["nonassertive", "supported_fact", "uncertain", "material_claim"]
    sources: list[AuditSource] = Field(max_length=32)
    implied_premises: list[str] = Field(max_length=8)


class Guidance(StrictModel):
    kind: str = Field(min_length=1, max_length=80)
    say_or_ask: str = Field(min_length=1, max_length=1000)
    reason: str = Field(max_length=2000)
    alternatives: list[str] = Field(max_length=2)
    evidence_segment_ids: list[str] = Field(max_length=32)
    playbook_card_ids: list[str] = Field(max_length=32)
    expires_in_ms: int = Field(ge=1, le=300000)


ObservationKind = Literal[
    "need",
    "impact",
    "objection",
    "answer",
    "signal",
    "commitment",
    "rundown",
    "coaching",
    "question",
    "approach",
]
ObservationState = Literal["open", "resolved", "confirmed", "retracted", "stale"]
ShortText = Annotated[str, Field(min_length=1, max_length=400)]


class SourceRef(AuditSource):
    occurred_at: str | None
    available_at: str | None
    content_hash: str | None
    raw_expires_at: str | None


class ProposedCommitment(StrictModel):
    kind: Literal["callback", "followup"]
    owner: Literal["rep", "prospect", "unknown"]
    due_at: str | None
    verbatim_time_expression: str = Field(max_length=400)
    needs_time_confirmation: bool


class ObservationDelta(StrictModel):
    kind: ObservationKind
    text: ShortText
    state: ObservationState
    sources: list[AuditSource] = Field(min_length=1, max_length=32)
    replaces: list[str] = Field(max_length=12)
    quote: str = Field(max_length=400)
    commitment: ProposedCommitment | None
    review_required: bool = False
    approach: ShortText | None = None


class RubricDelta(StrictModel):
    dimension: str = Field(min_length=1, max_length=64)
    status: Literal["unassessed", "insufficient", "not_applicable", "scored"]
    score: int | None = Field(ge=0, le=2)
    explanation: str = Field(max_length=400)
    sources: list[AuditSource] = Field(max_length=32)


class TrackerDelta(StrictModel):
    tracker_id: str = Field(min_length=1, max_length=160)
    definition_version: int = Field(ge=1)
    sources: list[AuditSource] = Field(min_length=1, max_length=32)


class StateDelta(StrictModel):
    stage: Literal["introduction", "discovery", "objection", "next_step", "declined"]
    observations: list[ObservationDelta] = Field(max_length=20)
    rubric_items: list[RubricDelta] = Field(max_length=12)
    tracker_matches: list[TrackerDelta] = Field(max_length=20)
    interpreted_segments: list[AuditSource] = Field(max_length=32)


class Observation(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    kind: ObservationKind
    text: ShortText
    state: ObservationState
    sources: list[SourceRef] = Field(max_length=32)
    replaces: list[str] = Field(max_length=12)
    epistemic_status: Literal["model_interpreted", "model_reviewed", "human_confirmed"]
    observed_at: str | None
    available_at: str | None
    invalidated_at: str | None
    review_receipt_id: str | None
    review_status: Literal["pending", "not_required", "reviewed", "unavailable", "rejected"]
    lineage_depth: int = Field(ge=0, le=8)
    commitment: dict | None = None
    approach: ShortText | None = None
    reviewer_id: str | None = None
    reviewed_at: str | None = None


class ConversationStateV2(StrictModel):
    schema_version: Literal[2] = 2
    call_id: str
    version: int = Field(ge=0)
    transcript_version: int = Field(ge=0)
    context_fingerprint: str
    stage: str
    trigger: str
    purpose: dict
    observations: list[Observation] = Field(max_length=96)
    rubric_items: list[dict] = Field(max_length=12)
    tracker_matches: list[dict] = Field(max_length=20)
    coverage: dict
    finalization: dict
    source_conflicts: list[dict] = Field(max_length=96)


class TurnOutput(StrictModel):
    state_version: int = Field(ge=0)
    context_fingerprint: str = Field(min_length=1, max_length=128)
    field_audit: list[FieldAudit] = Field(max_length=40)
    guidance: Guidance | None
    state_delta: StateDelta | None


def card_view(result):
    """One canonical provider turn, with a bounded old-client wire view."""
    if not isinstance(result, dict):
        raise ValueError("Conversation output must be an object")
    if "guidance" not in result:
        return result  # Legacy generated fields require independent review.
    parsed = TurnOutput.model_validate(result).model_dump()
    return {
        "action": "SHOW_CARD" if parsed["guidance"] else "NO_ACTION",
        **(parsed["guidance"] or {}),
        "state_version": parsed["state_version"],
        "context_fingerprint": parsed["context_fingerprint"],
        "field_audit": parsed["field_audit"],
        "state_update": parsed["state_delta"],
    }


COACH_SCHEMA = TurnOutput.model_json_schema()


# Explicitly configured legacy adapter compatibility only.
CARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["SHOW_CARD", "NO_ACTION"]},
        "kind": {"type": "string"},
        "say_or_ask": {"type": "string"},
        "reason": {"type": "string"},
        "evidence_segment_ids": {"type": "array", "items": {"type": "string"}},
        "playbook_card_ids": {"type": "array", "items": {"type": "string"}},
        "state_version": {"type": "integer"},
        "expires_in_ms": {"type": "integer"},
    },
    "required": [
        "action",
        "kind",
        "say_or_ask",
        "reason",
        "evidence_segment_ids",
        "playbook_card_ids",
        "state_version",
        "expires_in_ms",
    ],
}
