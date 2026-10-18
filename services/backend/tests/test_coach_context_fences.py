"""A24/A27: advice may not outlive changes to the knowledge that grounded it."""

from datetime import timedelta
from uuid import uuid4

import pytest
from app.db import get, put, update
from test_coaching_library import publish, source
from test_remediation_coach import advice


def prepared(workspace, app, clock):
    call, p = source(workspace, app)
    example = publish(workspace, call).json()
    service = app.state.service
    with service.db.transaction() as s:
        lead = get(s, "leads", call["lead_id"])
        fact = put(
            s,
            "research_facts",
            p["tenant_id"],
            p["seller_id"],
            {
                "domain": lead.data["domain"],
                "fact_id": "context-fact",
                "status": "supported",
                "statement": "The company offers maintenance services.",
                "expires_at": (clock.now + timedelta(seconds=2)).isoformat(),
            },
        )
        commitment = put(
            s,
            "commitments",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "status": "confirmed",
                "action": "Clarify the current process",
            },
        )
        old_call = put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "rep_id": p["user_id"],
                "state": "FINALIZED",
                "started_at": (clock.now - timedelta(days=2)).isoformat(),
                "outcome": "right_party",
            },
        )
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": old_call.id,
                "human_text": "Procurement owns the decision.",
                "notes": {"summary": "An earlier process discussion."},
            },
            key=old_call.id,
        )
    return (
        service,
        p,
        call,
        {"fact": fact.id, "commitment": commitment.id, "note": note.id, "example": example["id"]},
    )


def change(service, p, call, ids, boundary, clock):
    with service.db.transaction() as s:
        if boundary == "book":
            update(service.approved_playbook(s, p), product_description="The offering has changed.")
        elif boundary == "seller":
            update(get(s, "sellers", p["seller_id"]), name="New business identity")
        elif boundary == "lead":
            update(
                get(s, "leads", call["lead_id"]), prospect_brief={"summary": "The prior need was corrected."}
            )
        elif boundary == "fact":
            update(get(s, "research_facts", ids["fact"]), status="withdrawn")
        elif boundary == "commitment":
            update(get(s, "commitments", ids["commitment"]), status="cancelled")
        elif boundary == "note":
            update(get(s, "notes", ids["note"]), human_text="Correction: purchasing is not the owner.")
        elif boundary == "example":
            update(get(s, "snippets", ids["example"]), approved=False)
    if boundary == "expiry":
        clock.now += timedelta(seconds=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["book", "seller", "lead", "fact", "commitment", "note", "example", "expiry"]
)
async def test_live_advice_rechecks_all_used_knowledge_before_publication(
    workspace, app, clock, monkeypatch, boundary
):
    service, p, call, ids = prepared(workspace, app, clock)

    class Provider:
        last_usage, last_request_id = {"cost": 0.001}, "changed-context"

        async def suggest(self, request, **kwargs):
            assert request["facts"] and request["prior_commitments"] and request["prior_call_context"]
            change(service, p, call, ids, boundary, clock)
            return advice(request["state_version"], "lesson")

    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I clarify?")
    assert result["action"] == "NO_ACTION", boundary
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "coach_events", p)
        assert service.scoped(s, "usage_ledger", p)[0].data["cost_usd"] == 0.001


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["display", "retry"])
async def test_published_help_is_hidden_after_knowledge_changes(workspace, app, clock, monkeypatch, path):
    service, p, call, ids = prepared(workspace, app, clock)
    calls = []

    class Provider:
        last_usage, last_request_id = {"cost": 0.001}, "published-context"

        async def suggest(self, request, **kwargs):
            calls.append(request)
            return advice(request["state_version"], "lesson")

    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    identity = str(uuid4())
    result = await service.coach_turn(p, call["id"], urgent=True, question="Clarify?", request_id=identity)
    assert result["action"] == "SHOW_CARD"
    change(service, p, call, ids, "book", clock)
    if path == "retry":
        replay = await service.coach_turn(
            p, call["id"], urgent=True, question="Clarify?", request_id=identity
        )
        assert replay["action"] == "NO_ACTION"
    else:
        with service.db.transaction(False) as s:
            assert (
                service.visible_help(
                    s,
                    p,
                    get(s, "call_attempts", call["id"]),
                    service._by_key(s, p, "conversation_states", call["id"]).data,
                )
                is None
            )
    assert len(calls) == 1
