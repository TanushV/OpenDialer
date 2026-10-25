"""Seller-owned script structure with small, evidence-backed model-filled slots."""

import hashlib
import json
import re
from string import Formatter
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.validation import ValidationFailure

FIELDS = ("opener", "gatekeeper", "screening_intro", "discovery_questions", "proof", "closing_question")
SLOTS = {"name", "rep", "seller", "company", "topic", "relevance", "proof"}


class ScriptSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4000)


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
    sections: list[ScriptSection] = Field(default_factory=list, max_length=40)
    final_voicemail_asset: str | None = Field(default=None, min_length=1, max_length=2000)

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
        for section in self.sections:
            for _, key, spec, conversion in Formatter().parse(section.text):
                if key is not None and (key not in SLOTS or spec or conversion):
                    raise ValueError("Use only named script placeholders")
        if "{seller}" not in self.screening_intro:
            raise ValueError("The screening introduction must retain {seller}")
        return self


class PreparationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    ahead_count: int = Field(default=20, ge=1, le=100)
    workspace_ahead_cap: int = Field(default=100, ge=1, le=500)
    stable_fact_days: int = Field(default=30, ge=1, le=30)
    time_sensitive_fact_hours: int = Field(default=24, ge=1, le=24)
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
        "sections": [
            {"title": section["title"], "text": section["text"].format_map(values).strip()}
            for section in template.get("sections", [])
        ],
        "final_voicemail_asset": template.get("final_voicemail_asset"),
        **{
            field: [text.format_map(values).strip() for text in template[field]]
            if isinstance(template[field], list)
            else template[field].format_map(values).strip()
            for field in FIELDS
        },
    }


def approved_template(candidate, template, *, lead, seller, rep, facts):
    """Only unchanged seller text and whole reviewed facts qualify for the cheap path."""
    mapped = {fact["fact_id"]: fact["statement"].strip() for fact in facts}
    slots = {
        key: slot["text"]
        if slot["text"].strip() in {mapped[i] for i in slot["fact_ids"] if i in mapped}
        else ""
        for key, slot in candidate["slots"].items()
    }
    return render(template, lead=lead, seller=seller, rep=rep, slots=slots)


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
            # This checks structure and source identity only. The caller must pass
            # the rendered fields through validate_script with approved_template.
            # An empty slot cites nothing, so it never pins evidence it does not use.
            used.update(ids)
        text[key] = slot["text"]
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


def import_markdown(markdown):
    """Preserve seller wording; headings delimit sections, not model-generated copy."""
    sections, title, lines = [], "Script", []

    def flush():
        text = "\n".join(lines).strip()
        if text:
            sections.append({"title": title, "text": text})
        lines.clear()

    for line in markdown.splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush()
            title = heading[1]
        else:
            lines.append(line)
    flush()
    if not sections:
        raise ValueError("Add script text below the headings")
    template = ScriptTemplate().model_dump()
    template["sections"] = sections
    # Keep the standard fields available to preparation and controlled script tests.
    aliases = {
        "opener": "opener",
        "opening": "opener",
        "gatekeeper": "gatekeeper",
        "screening": "screening_intro",
        "next step": "closing_question",
        "closing": "closing_question",
    }
    for section in sections:
        field = aliases.get(section["title"].casefold())
        if field:
            template[field] = section["text"]
    return ScriptTemplate.model_validate(template).model_dump()
