"""Provider-neutral conversation transport. Private text never belongs in receipts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


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


class ObservationDelta(StrictModel):
    text: str = Field(max_length=400)
    quote: str = Field(max_length=400)
    evidence_segment_ids: list[str] = Field(max_length=32)
    status: Literal["open", "resolved", "confirmed"]
    replaces: list[str] = Field(max_length=12)


# The array read view is retained for the installed client until the V2 migration.
StateDelta = create_model(
    "StateDelta",
    __base__=StrictModel,
    stage=(Literal["introduction", "discovery", "objection", "next_step", "declined"], ...),
    **{
        key: (list[ObservationDelta], Field(max_length=3))
        for key in (
            "needs",
            "business_impact",
            "objections",
            "answered_questions",
            "buying_signals",
            "commitments",
            "rundown_points",
            "coaching_points",
            "open_questions",
        )
    },
)


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
