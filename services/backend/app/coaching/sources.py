"""Small content-free dependency receipts for current guidance."""

import hashlib
import json

from sqlalchemy import select

from app.coaching import library
from app.db import MODELS, get
from app.service import dt


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def knowledge(book, seller_name, examples):
    return {
        "book": [book.id, book.version] if book else None,
        "seller_name_hash": digest(seller_name),
        "examples": {item["id"]: item["version"] for item in examples},
    }


def knowledge_current(service, s, p, receipt):
    book = service.approved_playbook(s, p)
    seller = service.required(s, "sellers", p["seller_id"], p)
    if not receipt or receipt.get("book") != ([book.id, book.version] if book else None):
        return False
    if receipt.get("seller_name_hash") != digest(seller.data["name"]):
        return False
    expected = receipt.get("examples", {})
    current = {item["id"]: item["version"] for item in library.examples(service, s, p)} if expected else {}
    return all(current.get(identity) == version for identity, version in expected.items())


def lead_context(lead):
    return {key: lead.data.get(key) for key in ("name", "company", "segment", "prospect_brief")}


def current(service, s, p, receipt):
    if not receipt or not knowledge_current(service, s, p, receipt.get("knowledge")):
        return False
    lead = get(s, "leads", receipt["lead_id"], p["tenant_id"])
    if not lead or lead.seller_id != p["seller_id"] or digest(lead_context(lead)) != receipt["lead_hash"]:
        return False
    if strategy := receipt.get("strategy"):
        campaign = get(s, "campaigns", strategy["campaign_id"], p["tenant_id"])
        assignment = (
            get(s, "experiment_assignments", strategy["assignment_id"], p["tenant_id"])
            if strategy.get("assignment_id")
            else None
        )
        if not campaign or campaign.seller_id != p["seller_id"]:
            return False
        if assignment and assignment.seller_id != p["seller_id"]:
            return False
        from app.db import doc

        context = service.strategy_context(s, p, campaign, lead, doc(assignment) if assignment else None)
        if digest(context) != strategy["fingerprint"]:
            return False
    # One query per used kind, never per fact or over unrelated history.
    for kind, expected in receipt["rows"].items():
        if not expected and kind != "notes":
            continue
        note_calls = receipt.get("note_call_ids", [])
        if kind == "notes" and not note_calls:
            continue
        model = MODELS[kind]
        rows = list(
            s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key.in_(note_calls) if kind == "notes" else model.id.in_(expected),
                )
            )
        )
        if {row.id: row.version for row in rows} != expected:
            return False
        if kind == "research_facts" and any(
            row.data.get("status") != "supported"
            or not row.data.get("expires_at")
            or dt(row.data["expires_at"]) <= service.clock()
            for row in rows
        ):
            return False
    return True
