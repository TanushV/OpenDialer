"""Small, reviewed teaching library and personal goals in the existing store."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.coaching.context import lineage_key, state_view
from app.db import MODELS, audit, doc, put, update
from app.service import fail


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Goal(Strict):
    id: UUID
    text: str = Field(min_length=1, max_length=300)
    status: Literal["active", "completed"]


class GoalsEdit(Strict):
    expected_version: int = Field(ge=0)
    goals: list[Goal] = Field(max_length=3)


class ExampleReview(Strict):
    call_id: str
    coaching_point_id: str
    expected_transcript_version: int = Field(ge=0)
    review_reference: str = Field(min_length=1, max_length=300)


class ExampleWithdraw(Strict):
    expected_version: int = Field(ge=1)
    approved: Literal[False]


def examples(service, s, p):
    seller = service.required(s, "sellers", p["seller_id"], p)
    identities = seller.data.get("coaching_library_ids", [])[-24:]
    if not identities:
        return []
    model = MODELS["snippets"]
    rows = list(
        s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], model.id.in_(identities)
            )
        )
    )
    keys = {r.data["call_id"] + ":" + key for r in rows for key in r.data.get("evidence_revisions", {})}
    source = MODELS["transcript_segments"]
    revisions = {
        r.external_key: r
        for r in s.scalars(
            select(source).where(
                source.tenant_id == p["tenant_id"],
                source.seller_id == p["seller_id"],
                source.external_key.in_(keys),
            )
        )
    }
    result = []
    for row in rows:
        data = row.data
        evidence = data.get("evidence_revisions", {})
        if not data.get("approved") or data.get("privacy_contract") != 1 or not evidence:
            continue
        if any(
            (source := revisions.get(data["call_id"] + ":" + key)) is None
            or source.data["revision"] != revision
            for key, revision in evidence.items()
        ):
            continue
        result.append(
            {
                **doc(row),
                "raw_evidence_status": "available_temporarily"
                if all(service.raw.has(service.raw_key(p, data["call_id"], key)) for key in evidence)
                else "expired",
            }
        )
    return sorted(result, key=lambda row: row["id"])


def context_examples(service, s, p, query):
    from app.coaching.context import words

    terms = words(query or "")
    candidates = examples(service, s, p)
    candidates.sort(key=lambda row: (-len(words(row["text"]) & terms), row["id"]))
    return [
        {
            "id": row["id"],
            "version": row["version"],
            "text": row["text"],
            "scope": "Reviewed coaching example; not a fact about this prospect or proof of conversion lift",
        }
        for row in candidates[:3]
    ]


def overview(service, p):
    from app.optimization.conversations import aliases_for, normalized

    with service.db.transaction(False) as s:
        user = service.required(s, "users", p["user_id"], p)
        model = MODELS["call_attempts"]
        calls = list(
            s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_owner_id == p["user_id"],
                )
                .order_by(model.created_at.desc(), model.id.desc())
                .limit(20)
            )
        )
        state = MODELS["conversation_states"]
        metadata = MODELS["outcomes"]
        lineage = {}
        for row in s.scalars(
            select(metadata).where(
                metadata.tenant_id == p["tenant_id"],
                metadata.seller_id == p["seller_id"],
                metadata.ctx_key.in_([c.id for c in calls]),
                metadata.ctx_status == "conversation_lineage",
            )
        ):
            lineage.setdefault(row.data["call_id"], {})[lineage_key(row.data["id"], row.data["version"])] = (
                row.data
            )
        states = {
            r.external_key: state_view(r.data, lineage=lineage.get(r.external_key))
            for r in s.scalars(
                select(state).where(
                    state.tenant_id == p["tenant_id"],
                    state.seller_id == p["seller_id"],
                    state.external_key.in_([c.id for c in calls]),
                )
            )
        }
        opportunities = []
        for call in calls:
            for item in reversed(states.get(call.id, {}).get("observations", [])):
                if (
                    item.get("kind") != "approach"
                    or item.get("state") in {"stale", "retracted"}
                    or item.get("epistemic_status") not in {"human_confirmed", "model_reviewed"}
                    or item.get("review_status") != "reviewed"
                    or not item.get("observed_at")
                ):
                    continue
                opportunities.append(
                    {
                        "id": item["id"],
                        "call_id": call.id,
                        "campaign_id": call.data.get("campaign_id"),
                        "at": item["observed_at"],
                        "concept": item["text"],
                        "approach": item.get("approach"),
                        "review_status": item["epistemic_status"],
                        "purpose": states[call.id].get("purpose", {}).get("id"),
                    }
                )
        opportunities = sorted(opportunities, key=lambda i: (i["at"], i["call_id"], i["id"]), reverse=True)[
            :20
        ]
        vocabularies, groups = {}, {}
        for item in opportunities:
            campaign_id = item["campaign_id"]
            if campaign_id not in vocabularies:
                campaign = None
                # Campaign IDs are primary identities, not external keys.
                if campaign_id:
                    campaign = service.required(s, "campaigns", campaign_id, p)
                vocabularies[campaign_id] = aliases_for(
                    campaign.data.get("conversation_vocabulary", {}) if campaign else {}
                )
            aliases = vocabularies[campaign_id]
            concept = aliases.get(("concept", normalized(item["concept"])), normalized(item["concept"]))
            approach = aliases.get(("approach", normalized(item["approach"])), normalized(item["approach"]))
            key = (campaign_id, item["purpose"], concept, approach)
            group = groups.setdefault(
                key,
                {
                    "id": hashlib.sha256(json.dumps(key).encode()).hexdigest(),
                    "concept": item["concept"],
                    "approach": item["approach"],
                    "campaign_id": campaign_id,
                    "purpose": item["purpose"],
                    "count": 0,
                    "examples": [],
                },
            )
            group["count"] += 1
            if len(group["examples"]) < 2:
                group["examples"].append(
                    {
                        "call_id": item["call_id"],
                        "observation_id": item["id"],
                        "text": item["approach"] or item["concept"],
                    }
                )
        return {
            "reviewed_opportunities": opportunities,
            "improvement_groups": sorted(groups.values(), key=lambda g: (-g["count"], g["id"])),
            "goals_version": user.data.get("coaching_goals_version", 0),
            "goals": user.data.get("coaching_goals", []),
            "recent_tips": [
                {
                    "call_id": c.id,
                    "at": c.data["started_at"],
                    "tips": states[c.id].get("coaching_points", [])[:3],
                }
                for c in calls
                if c.id in states and not states[c.id].get("stale") and states[c.id].get("coaching_points")
            ],
            "inspected_calls": len(calls),
            "history_limit": 20,
            "examples": examples(service, s, p),
        }


def save_goals(service, p, data):
    goals = data["goals"]
    if len({g["id"] for g in goals}) != len(goals) or any(not g["text"].strip() for g in goals):
        fail(422, "Use distinct goals with meaningful text")
    with service.db.transaction() as s:
        user = service.required(s, "users", p["user_id"], p)
        s.refresh(user, with_for_update=True)
        version = user.data.get("coaching_goals_version", 0)
        if data["expected_version"] != version:
            fail(409, "Coaching goals changed; reload before saving")
        update(user, coaching_goals=goals, coaching_goals_version=version + 1)
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "coaching.goals_updated",
            user.id,
            service.clock(),
        )
        return {"goals": goals, "goals_version": version + 1}


def publish(service, p, data):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        call = service.required(s, "call_attempts", data["call_id"], p)
        if call.data["transcript_version"] != data["expected_transcript_version"]:
            fail(409, "The call context changed; review the current tip")
        state = service._by_key(s, p, "conversation_states", call.id)
        view = state_view(state.data, lineage=service._conversation_lineage(s, p, call.id)) if state else {}
        point = next(
            (i for i in view.get("coaching_points", []) if i.get("id") == data["coaching_point_id"]),
            None,
        )
        if (
            not point
            or not 0 < len(point.get("evidence_revisions", {})) <= 16
            or not data["review_reference"].strip()
        ):
            fail(422, "Review a current source-linked coaching tip")
        for identity, version in point["evidence_revisions"].items():
            source = service._by_key(s, p, "transcript_segments", call.id + ":" + identity)
            if not source or source.data["revision"] != version:
                fail(409, "The supporting evidence changed")
        # A new source revision requires a new editorial decision; a withdrawn
        # review of the same wording/evidence must never be resurrected by retry.
        fingerprint = hashlib.sha256(
            json.dumps([point["text"], point["evidence_revisions"]], sort_keys=True).encode()
        ).hexdigest()
        key = call.id + ":" + point["id"] + ":" + fingerprint
        prior = service._by_key(s, p, "snippets", key)
        if prior:
            if not prior.data.get("approved") or prior.data.get("text") != point["text"]:
                fail(409, "This example was withdrawn or changed; review a new source tip")
            return doc(prior)
        seller = service.required(s, "sellers", p["seller_id"], p)
        identities = [item["id"] for item in examples(service, s, p)]
        if len(identities) >= 24:
            fail(409, "Withdraw an older example before adding another to the 24-example library")
        example = put(
            s,
            "snippets",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call.id,
                "coaching_point_id": point["id"],
                "text": point["text"],
                "evidence_revisions": point["evidence_revisions"],
                "review_reference": data["review_reference"],
                "reviewed_by": p["user_id"],
                "reviewed_at": service.clock().isoformat(),
                "approved": True,
                "privacy_contract": 1,
            },
            key=key,
            now=service.clock(),
        )
        update(seller, coaching_library_ids=[*identities, example.id])
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "coaching.example_approved",
            example.id,
            service.clock(),
        )
        return doc(example)


def withdraw(service, p, identity, expected_version):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        example = service.required(s, "snippets", identity, p)
        if example.version != expected_version:
            fail(409, "Example changed; reload before withdrawing")
        update(example, approved=False)
        seller = service.required(s, "sellers", p["seller_id"], p)
        update(
            seller,
            coaching_library_ids=[i for i in seller.data.get("coaching_library_ids", []) if i != identity],
        )
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "coaching.example_withdrawn",
            identity,
            service.clock(),
        )
        return doc(example)
