"""Versioned, seller-editable scoring anchors. These do not constrain live advice."""

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import audit, doc, emit, update
from app.service import fail

CallType = Literal["cold_introduction", "qualification", "discovery", "follow_up"]
Purpose = Literal["cold_introduction", "qualification", "discovery", "follow_up", "unknown"]
CALL_TYPES = ("cold_introduction", "qualification", "discovery", "follow_up")
OPERATIONAL = {"wrong_party", "voicemail", "screening", "ivr", "no_answer", "busy", "invalid_endpoint"}
Text = Annotated[str, Field(min_length=1, max_length=600)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Dimension(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    weight: float = Field(gt=0, le=10)
    anchors: dict[Literal["0", "1", "2"], Text]

    @model_validator(mode="after")
    def all_anchors(self):
        if set(self.anchors) != {"0", "1", "2"}:
            raise ValueError("Provide observable anchors for all three ratings")
        return self


class Rubric(Strict):
    title: str = Field(min_length=1, max_length=100)
    purpose: Text
    dimensions: list[Dimension] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_dimensions(self):
        if len({d.id for d in self.dimensions}) != len(self.dimensions):
            raise ValueError("Each rubric dimension must have a unique ID")
        return self


class RubricConfig(Strict):
    campaign_id: str
    expected_version: int = Field(ge=1)
    default_call_type: CallType
    rubrics: dict[CallType, Rubric]

    @model_validator(mode="after")
    def all_types(self):
        if set(self.rubrics) != set(CALL_TYPES):
            raise ValueError("Provide a rubric for each supported sales call purpose")
        return self


class PurposeEdit(Strict):
    expected_version: int = Field(ge=1)
    call_type: Purpose
    reason: str = Field(min_length=1, max_length=500)


def dimension(identity, title, anchors):
    return {
        "id": identity,
        "title": title,
        "weight": 1,
        "anchors": dict(zip(("0", "1", "2"), anchors, strict=True)),
    }


def defaults():
    # Editable starting anchors describe observable opportunity, never a topic classifier or learned truth.
    common = {
        "listening_response": (
            "Listening and response",
            [
                "Ignored a clear answer or repeated an answered question",
                "Acknowledged the answer without adapting",
                "Used the actual answer to guide the next response",
            ],
        ),
        "objection_handling": (
            "Objection handling",
            [
                "Dismissed an explicit concern despite opportunity",
                "Acknowledged but did not clarify the concern",
                "Clarified and addressed the concern using supported information",
            ],
        ),
        "next_step": (
            "Next step",
            [
                "Left an offered next step unclear",
                "Identified a tentative action without ownership",
                "Confirmed an appropriate action and owner, or respected a clear refusal",
            ],
        ),
        "factual_accuracy": (
            "Factual accuracy",
            [
                "Made a contradicted or unsupported business claim",
                "Qualified uncertainty but left a material claim unclear",
                "Used supported facts and clearly identified unknowns",
            ],
        ),
    }
    descriptions = {
        "cold_introduction": (
            "Cold introduction",
            "A brief first contact; score only opportunities actually offered.",
            {
                "identity_relevance": (
                    "Identity and relevance",
                    [
                        "Omitted identity or purpose despite opportunity",
                        "Identified the seller but left relevance unclear",
                        "Gave a truthful identity and concise relevant purpose",
                    ],
                ),
                "problem_discovery": (
                    "Initial discovery",
                    [
                        "Missed an explicit opportunity to understand a need",
                        "Asked a broad opening question",
                        "Asked a relevant initial question and used the answer",
                    ],
                ),
                "business_impact": (
                    "Initial impact",
                    [
                        "Invented urgency or ignored an offered consequence",
                        "Acknowledged a stated consequence",
                        "Clarified an explicitly offered consequence without forcing discovery",
                    ],
                ),
            },
        ),
        "qualification": (
            "Qualification",
            "Explore fit and decision process without treating an unknown budget as a failure.",
            {
                "identity_relevance": (
                    "Qualification purpose",
                    [
                        "Pursued irrelevant qualification despite correction",
                        "Stated a broad fit-check purpose",
                        "Agreed a relevant fit-check purpose with the prospect",
                    ],
                ),
                "problem_discovery": (
                    "Fit and constraints",
                    [
                        "Ignored stated requirements or constraints",
                        "Established a general need",
                        "Clarified requirements, fit and remaining unknowns",
                    ],
                ),
                "business_impact": (
                    "Decision process",
                    [
                        "Assumed authority or budget without evidence",
                        "Identified one decision-process detail",
                        "Clarified the relevant process and constraints without inventing authority",
                    ],
                ),
            },
        ),
        "discovery": (
            "Discovery",
            "Understand workflow, consequences and priorities in a substantive conversation.",
            {
                "identity_relevance": (
                    "Agreed purpose",
                    [
                        "Ignored the prospect's stated agenda",
                        "Gave a general agenda",
                        "Aligned the discussion to the prospect's stated priorities",
                    ],
                ),
                "problem_discovery": (
                    "Workflow and needs",
                    [
                        "Ignored a described workflow or pain",
                        "Established a surface-level problem",
                        "Explored concrete workflow and unmet needs using follow-up questions",
                    ],
                ),
                "business_impact": (
                    "Consequences",
                    [
                        "Invented consequences or ignored offered impact",
                        "Identified a consequence without clarifying it",
                        "Clarified prospect-stated consequences and priority without manufacturing urgency",
                    ],
                ),
            },
        ),
        "follow_up": (
            "Follow up",
            "Continue earlier work and promises; do not require a repeated introduction or full discovery.",
            {
                "identity_relevance": (
                    "Continuity",
                    [
                        "Misstated the prior conversation or promise",
                        "Recalled prior context generally",
                        "Accurately resumed the agreed topic and purpose",
                    ],
                ),
                "problem_discovery": (
                    "Changes and open questions",
                    [
                        "Repeated resolved questions or ignored new information",
                        "Checked for updates generally",
                        "Resolved an open question or explored a relevant change",
                    ],
                ),
                "business_impact": (
                    "Progress and commitments",
                    [
                        "Ignored a due commitment despite opportunity",
                        "Mentioned progress without confirming the next dependency",
                        "Checked prior commitments and clarified the next dependency",
                    ],
                ),
            },
        ),
    }
    order = (
        "identity_relevance",
        "listening_response",
        "problem_discovery",
        "business_impact",
        "objection_handling",
        "next_step",
        "factual_accuracy",
    )
    return {
        kind: {
            "title": title,
            "purpose": purpose,
            "dimensions": [dimension(key, *(custom | common)[key]) for key in order],
        }
        for kind, (title, purpose, custom) in descriptions.items()
    }


def configuration(campaign):
    value = campaign.data.get("rubric_config") or {
        "default_call_type": "cold_introduction",
        "rubrics": defaults(),
    }
    return {
        "campaign_id": campaign.id,
        "expected_version": campaign.data.get("rubric_config_version", 1),
        **deepcopy(value),
    }


def snapshot(config, call_type=None):
    kind = call_type or config["default_call_type"]
    rubric = (
        config["rubrics"][kind]
        if kind in config["rubrics"]
        else {
            "title": "Unknown purpose",
            "purpose": "Confirm the actual opportunity before applying a sales rubric.",
            "dimensions": [],
        }
    )
    digest = hashlib.sha256(json.dumps(rubric, sort_keys=True).encode()).hexdigest()[:12]
    return {
        **deepcopy(rubric),
        "call_type": kind,
        "rubric_version": f"{kind}:{config['expected_version']}:{digest}",
    }


def effective(call):
    if call.get("outcome") in OPERATIONAL:
        return {
            "call_type": "operational",
            "rubric_version": "operational:1",
            "title": "Operational only",
            "purpose": "This outcome provides no sales-scoring opportunity.",
            "dimensions": [],
        }
    return call.get("rubric_snapshot") or snapshot(
        {"default_call_type": "cold_introduction", "rubrics": defaults(), "expected_version": 1}
    )


def bind_api(app, service, principal, administrator):
    @app.get("/v1/admin/rubrics")
    def get_config(campaign_id: str, p=Depends(administrator)):
        with service.db.transaction(False) as s:
            return configuration(service.required(s, "campaigns", campaign_id, p))

    @app.put("/v1/admin/rubrics")
    def save_config(data: RubricConfig, p=Depends(administrator)):
        with service.db.transaction() as s:
            campaign = service.required(s, "campaigns", data.campaign_id, p)
            if configuration(campaign)["expected_version"] != data.expected_version:
                fail(409, "Scoring rubrics changed; reload before saving")
            update(
                campaign,
                rubric_config=data.model_dump(exclude={"campaign_id", "expected_version"}),
                rubric_config_version=data.expected_version + 1,
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "rubrics.updated",
                campaign.id,
                service.clock(),
            )
            return configuration(campaign)

    @app.put("/v1/calls/{call_id}/rubric")
    def change_purpose(call_id: str, data: PurposeEdit, p=Depends(principal)):
        with service.db.transaction() as s:
            call = service.required(s, "call_attempts", call_id, p)
            if call.version != data.expected_version:
                fail(409, "Call changed; refresh before changing its purpose")
            lead = service.required(s, "leads", call.data["lead_id"], p)
            campaign = service.required(s, "campaigns", lead.data["campaign_id"], p)
            update(
                call,
                rubric_snapshot=snapshot(configuration(campaign), data.call_type),
                rubric_revision=call.data.get("rubric_revision", 1) + 1,
            )
            # The stale token exists even before any reader: a rating of the old purpose
            # can never be recorded against the new rubric.
            _, card = service._materialize_views_in(s, p, call, notes=False)
            update(card, stale=True)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "call.purpose_changed",
                call_id,
                service.clock(),
                {
                    "call_type": data.call_type,
                    "reason": data.reason,
                    "rubric_revision": call.data["rubric_revision"],
                },
            )
            if service.transcript_rows(s, p, call_id):
                service.enqueue_finalize(s, p, call, "purpose_changed")
            emit(s, p["tenant_id"], "call.updated", call, service.clock())
            return doc(call)
