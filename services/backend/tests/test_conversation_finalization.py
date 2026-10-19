"""E02/C005-C006: public transcript ingestion and one bounded long-call state."""

import json
from datetime import timedelta

import pytest
from app.db import update
from test_live_memory import setup
from test_remediation_privacy import persisted_content


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_at_hangup", [False, True])
async def test_early_context_survives_ttl(workspace, app, clock, monkeypatch, raw_at_hangup):
    call, p, requests, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    c, h = workspace["client"], workspace["headers"]
    began = clock.now
    old_roles = []

    def absent_legacy_inference(tenant, role, user=None):
        old_roles.append(role)
        return None

    monkeypatch.setattr(service, "inference", absent_legacy_inference)
    for minute, identity, text, paraphrase in [
        (
            1,
            "early",
            "Our approvals take weeks. Call me Tuesday at two Pacific.",
            "The buyer needs a shorter approval process and requested a Tuesday callback.",
        ),
        (
            6,
            "resolved",
            "Budget is now approved, that earlier concern is resolved.",
            "Funding is available following approval.",
        ),
        (
            9,
            "correction",
            "I meant Wednesday, not Tuesday. Still two Pacific.",
            "The requested follow-up moved to Wednesday afternoon Pacific.",
        ),
    ]:
        clock.now = began + timedelta(minutes=minute)
        changes["result"] = {
            "action": "NO_ACTION",
            "state_update": {
                "stage": "discovery",
                "rundown_points": [
                    {
                        "text": paraphrase,
                        "quote": text,
                        "evidence_segment_ids": [identity],
                        "status": "confirmed",
                    }
                ]
            },
        }
        response = c.post(
            f"/v1/calls/{call['id']}/transcript",
            headers=h,
            json={
                "segment_id": identity,
                "revision": 1,
                "is_final": True,
                "side": "remote",
                "text": text,
            },
        )
        assert response.status_code == 200, response.text
        clock.now += timedelta(milliseconds=301)
        await service.coach_turn(p, call["id"], urgent=True)
    clock.now = began + timedelta(minutes=12 if raw_at_hangup else 15)
    initial = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert "shorter approval" in json.dumps(initial["call_memory"])
    assert "early" not in {s["segment_id"] for s in initial["transcript"]}
    if not raw_at_hangup:
        assert initial["transcript"] == []
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    before = len(requests)
    await service.analyze_postcall(p, call["id"])
    assert len(requests) == before + 1, "Finalization must use the configured Cerebras conversation route"
    assert "postcall_extract" not in old_roles, "Do not invoke the separate recent-window extraction pipeline"
    final_request = requests[-1]
    assert "shorter approval" in json.dumps(final_request)
    assert "Wednesday" in json.dumps(final_request)
    assert "Our approvals take weeks" not in json.dumps(final_request)
    assert "Our approvals take weeks" not in persisted_content(service, p)
    result = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert result.get("finalization", {}).get("status") in {"ready", "partial"}
    # The same state version cannot create another finalizer charge.
    await service.analyze_postcall(p, call["id"])
    assert len(requests) == before + 1


@pytest.mark.asyncio
async def test_merge_correction_and_missing_source(workspace, app, clock, monkeypatch):
    call, p, requests, changes = setup(workspace, app, monkeypatch)
    from test_live_memory import turn

    service = app.state.service
    turn(service, p, call, "original")
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction() as s:
        state = service._by_key(s, p, "conversation_states", call["id"])
        before = dict(state.data)
    clock.now += timedelta(seconds=301)
    turn(service, p, call, "unrelated")
    changes["result"] = {"action": "NO_ACTION", "state_update": {"stage": "discovery"}}
    await service.coach_turn(p, call["id"], urgent=True)
    view = service.analysis(p, call["id"])
    assert "workflow constraint in part 1" in json.dumps(view["call_memory"])
    assert not view["transcript"][0]["segment_id"] == "original"
    # Current human text must survive a late interpretation/correction.
    with service.db.transaction() as s:
        note = service._by_key(s, p, "notes", call["id"])
        update(note, human_text="Rep verified the current requirement directly.")
    turn(service, p, call, "original", revision=2, text="The original statement was incorrect.")
    corrected = service.analysis(p, call["id"])
    assert corrected["human_notes"] == "Rep verified the current requirement directly."
    assert corrected["call_memory"] != before
    assert corrected.get("source_conflicts"), (
        "Correction must expose an actionable conflict after evidence expires"
    )
