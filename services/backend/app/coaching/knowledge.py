"""Versioned seller knowledge; facts constrain the helper, not exact wording."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.db import audit, doc, emit, put
from app.service import fail

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    text: Line
    tags: list[Annotated[str, StringConstraints(max_length=80)]] = Field(default_factory=list, max_length=12)
    approved_variants: list[Line] = Field(default_factory=list, max_length=8)


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Business introduction", min_length=1, max_length=200)
    opener: str = Field(min_length=1, max_length=600)
    screening_intro: str = Field(min_length=1, max_length=400)
    product_description: str = Field(default="", max_length=12000)
    claims: list[Line] = Field(default_factory=list, max_length=32)
    prohibited_claims: list[Line] = Field(default_factory=list, max_length=32)
    cards: list[Example] = Field(default_factory=list, max_length=32)
    approved: bool
    reason: Line
    expected_previous_id: str | None
    expected_previous_version: int = Field(ge=0)

    @model_validator(mode="after")
    def unique_examples(self):
        if len({card.id for card in self.cards}) != len(self.cards):
            raise ValueError("Example IDs must be distinct")
        return self


def publish(service, p, data):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        seller = service.required(s, "sellers", p["seller_id"], p)
        if seller.data["name"].casefold() not in data["screening_intro"].casefold():
            fail(422, "Reviewed truthful identity and claims are required")
        previous = service.approved_playbook(s, p)
        if (data["expected_previous_id"], data["expected_previous_version"]) != (
            previous.id if previous else None,
            previous.version if previous else 0,
        ):
            fail(409, "Business knowledge changed; reload and review the current version")
        obj = put(
            s,
            "playbooks",
            p["tenant_id"],
            p["seller_id"],
            {key: value for key, value in data.items() if not key.startswith("expected_previous_")}
            | {"previous_id": previous.id if previous else None},
            now=service.clock(),
        )
        obj.version = previous.version + 1 if previous else 1
        audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "playbook.published", obj.id, service.clock())
        if obj.data["approved"]:
            emit(s, p["tenant_id"], "knowledge.updated", obj, service.clock())
        return doc(obj)


def sync_from_script(service, p, template):
    """The script's Facts and Never say are the helper's approved and prohibited claims: one place."""
    with service.db.transaction(False) as s:
        previous = service.approved_playbook(s, p)
    if previous is None:
        return None
    claims, prohibited = template.get("facts", [])[:32], template.get("never_say", [])[:32]
    data = previous.data
    if data.get("claims", []) == claims and data.get("prohibited_claims", []) == prohibited:
        return None
    return publish(
        service,
        p,
        Edit(
            **{key: data[key] for key in ("name", "opener", "screening_intro") if data.get(key)},
            product_description=data.get("product_description", ""),
            claims=claims,
            prohibited_claims=prohibited,
            cards=data.get("cards", []),
            approved=True,
            reason="Facts and Never say from the saved script",
            expected_previous_id=previous.id,
            expected_previous_version=previous.version,
        ).model_dump(),
    )
