"""Seller-owned script structure with small, evidence-backed model-filled slots."""

import hashlib
import json
from string import Formatter
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.validation import ValidationFailure

FIELDS = ("opener", "gatekeeper", "screening_intro", "discovery_questions", "proof", "closing_question")
SLOTS = {"name", "rep", "seller", "company", "topic", "relevance", "proof"}


class ScriptTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opener: str = Field(
        default="Hello {name}, this is {rep} from {seller}. {relevance}", min_length=1, max_length=600
    )
    gatekeeper: str = Field(
        default="Who would be the right person to speak with about {topic}?", min_length=1, max_length=400
    )
    screening_intro: str = Field(
        default="{rep} from {seller}, calling about {topic}.", min_length=1, max_length=400
    )
    discovery_questions: list[str] = Field(
        default_factory=lambda: ["How are you handling {topic}?"], min_length=1, max_length=5
    )
    topic: str = Field(default="your current process", min_length=1, max_length=150)
    proof: str = Field(default="{proof}", max_length=600)
    closing_question: str = Field(default="Would a further conversation be useful?", max_length=400)

    @model_validator(mode="after")
    def placeholders(self):
        for field in FIELDS:
            values = getattr(self, field)
            for value in values if isinstance(values, list) else [values]:
                if len(value) > 600:
                    raise ValueError("Keep script sections concise")
                for _, key, spec, conversion in Formatter().parse(value):
                    if key is not None and (key not in SLOTS or spec or conversion):
                        raise ValueError(
                            "Use only named script placeholders, without formatting or property access"
                        )
        if "{seller}" not in self.screening_intro:
            raise ValueError("The screening introduction must retain {seller}")
        return self


class PreparationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    ahead_count: int = Field(default=20, ge=1, le=100)
    batch_size: int = Field(default=20, ge=1, le=20)
    off_hours_start: int = Field(default=0, ge=0, le=23)
    off_hours_end: int = Field(default=24, ge=0, le=24)
    timezone: str = "America/Los_Angeles"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as exc:
            raise ValueError("Use an IANA timezone") from exc
        return value


def template_version(template):
    return (
        hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()[:16]
        if template
        else "legacy-playbook"
    )


def policy_values(stored):
    stored = dict(stored)
    preparation = dict(stored.get("preparation") or {})
    for old, new in {
        "prepared_ahead": "ahead_count",
        "preparation_timezone": "timezone",
        "preparation_start_hour": "off_hours_start",
        "preparation_end_hour": "off_hours_end",
    }.items():
        if old in stored:
            preparation.setdefault(new, stored.pop(old))
    stored["preparation"] = preparation
    return stored


def in_window(now, config):
    hour = now.astimezone(ZoneInfo(config["timezone"])).hour
    start, end = config["off_hours_start"], config["off_hours_end"]
    return start <= hour < end if start < end else hour >= start or hour < end if start > end else False


def render(template, *, lead, seller, rep, slots=None):
    values = {
        "name": lead.get("name") or "there",
        "company": lead.get("company") or "your company",
        "seller": seller,
        "rep": rep,
        "topic": template["topic"],
        "relevance": "",
        "proof": "",
        **(slots or {}),
    }
    return {
        field: [text.format_map(values).strip() for text in template[field]]
        if isinstance(template[field], list)
        else template[field].format_map(values).strip()
        for field in FIELDS
    }


def fill(candidate, template, *, lead, seller, rep, facts):
    if set(candidate) != {"lead_id", "slots"} or candidate["lead_id"] != lead["id"]:
        raise ValidationFailure("A template response must identify the lead and only fill allowed slots")
    slots = candidate["slots"]
    if set(slots) != {"relevance", "proof"}:
        raise ValidationFailure("Template slots are incomplete or attempt to replace locked fields")
    mapped = {fact["fact_id"]: fact for fact in facts}
    used, text = set(), {}
    for key, slot in slots.items():
        if set(slot) != {"text", "fact_ids"} or not isinstance(slot["text"], str) or len(slot["text"]) > 400:
            raise ValidationFailure("Invalid script slot")
        ids = slot["fact_ids"]
        if slot["text"]:
            if not ids or not set(ids).issubset(mapped):
                raise ValidationFailure("Every inserted factual slot requires supported facts")
            # Use an already verified statement as the slot. Novel factual paraphrases need
            # semantic review; a model-supplied claims list cannot exempt visible text.
            if slot["text"].strip() not in {mapped[i]["statement"].strip() for i in ids}:
                raise ValidationFailure("Inserted text is not supported by the cited fact statements")
        text[key] = slot["text"]
        used.update(ids)
    return {
        **render(template, lead=lead, seller=seller, rep=rep, slots=text),
        "lead_id": lead["id"],
        "fact_ids_used": sorted(used),
        "claims": [
            {"text": slot["text"], "fact_ids": slot["fact_ids"]} for slot in slots.values() if slot["text"]
        ],
    }


SLOT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lead_id", "slots"],
    "properties": {
        "lead_id": {"type": "string"},
        "slots": {
            "type": "object",
            "additionalProperties": False,
            "required": ["relevance", "proof"],
            "properties": {
                name: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "fact_ids"],
                    "properties": {
                        "text": {"type": "string"},
                        "fact_ids": {"type": "array", "items": {"type": "string"}},
                    },
                }
                for name in ("relevance", "proof")
            },
        },
    },
}
