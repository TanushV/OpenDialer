"""E03/C009: public rep journey, persisted edits and a real authority restart."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from app.db import update
from app.main import create_app
from conftest import start
from fastapi.testclient import TestClient
from test_remediation_privacy import principal


@pytest.mark.parametrize("mode", ["local", "connected"])
def test_full_rep_journey_both_authorities(workspace, app, clock, carrier, monkeypatch, mode):
    if mode == "connected" and not os.getenv("OPENDIALER_TEST_POSTGRES_URL"):
        pytest.skip("Connected journey requires the explicitly guarded PostgreSQL run")
    if mode == "connected":
        assert not app.state.db.sqlite
    monkeypatch.setattr(
        app.state.settings if hasattr(app.state, "settings") else app.state.service.settings, "mode", mode
    )
    service, p = app.state.service, principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    imported = c.post(
        "/v1/leads/import",
        headers=h,
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [
                {
                    "name": "Journey contact",
                    "company": "Journey Company",
                    "domain": "journey.example.test",
                    "phone": "+14155550808",
                    "timezone": "America/Los_Angeles",
                }
            ],
        },
    )
    assert imported.status_code == 200, imported.text
    lead_id = imported.json()["leads"][0]["id"]
    with service.db.transaction() as s:
        for row in service.scoped(s, "leads", p):
            if row.id != lead_id:
                update(row, status="needs_review")
    prepared = c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead_id]})
    assert prepared.status_code == 200, prepared.text
    c.post("/v1/admin/jobs/run", headers=h)
    packet = c.get(f"/v1/leads/{lead_id}/packet", headers=h).json()
    assert packet["script"]["ready_for_rep"]
    call = start(workspace)
    assert call["lead_id"] == lead_id
    clock.now += timedelta(minutes=1)
    early = c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "early-promise",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "Please call me Tuesday at two Pacific about approval times.",
        },
    )
    assert early.status_code == 200
    clock.now += timedelta(minutes=11)
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    current = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()["call"]
    due = "2026-10-20T21:00:00+00:00"
    request_id = str(uuid4())
    body = {
        "outcome": "callback_requested",
        "contact_role": "right_party",
        "callback_at": due,
        "request_id": request_id,
        "expected_version": current["version"],
        "callback_note": "Rep confirmed approval discussion.",
    }
    saved = c.post(f"/v1/calls/{call['id']}/disposition", headers=h, json=body)
    assert saved.status_code == 200, saved.text
    replay = c.post(f"/v1/calls/{call['id']}/disposition", headers=h, json=body)
    assert replay.status_code == 200, replay.text
    tasks = [t for t in c.get("/v1/workspace", headers=h).json()["tasks"] if t.get("lead_id") == lead_id]
    assert len(tasks) == 1
    task = tasks[0]
    # Snoozing a reminder must not move the agreed callback itself.
    snooze = (clock.now + timedelta(hours=1)).isoformat()
    edit = {
        "request_id": str(uuid4()),
        "expected_version": task["version"],
        "status": "snoozed",
        "snoozed_until": snooze,
    }
    changed = c.patch(f"/v1/tasks/{task['id']}", headers=h, json=edit)
    assert changed.status_code == 200, changed.text
    assert changed.json()["due_at"] == due
    assert changed.json()["snoozed_until"] == snooze
    assert c.patch(f"/v1/tasks/{task['id']}", headers=h, json=edit).status_code == 200
    assert (
        c.patch(f"/v1/tasks/{task['id']}", headers=h, json={**edit, "snoozed_until": due}).status_code == 409
    )
    restarted = create_app(service.settings, clock=clock, carrier=carrier)
    with TestClient(restarted) as again:
        restored = again.get(f"/v1/leads/{lead_id}/packet", headers=h)
        assert restored.status_code == 200, restored.text
        assert due in restored.text
        review = again.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
        assert review["transcript"] == []
        assert "Please call me Tuesday" not in str(review)
        latest = restored.json()["lead"]
        booking = {
            "request_id": str(uuid4()),
            "expected_version": latest["version"],
            "status": "booked",
            "due_at": due,
            "timezone": "America/Los_Angeles",
        }
        booked = again.post(f"/v1/leads/{lead_id}/meeting", headers=h, json=booking)
        assert booked.status_code == 200, booked.text
        packet = again.get(f"/v1/leads/{lead_id}/packet", headers=h).json()
        assert packet["lead"]["outreach_hold"] == "meeting_booked"
        assert not packet["prospect_brief"]["meeting"]["invitation_sent"]
        assert not packet["prospect_brief"]["meeting"]["held"]
        invalid = again.post(
            f"/v1/leads/{lead_id}/meeting",
            headers=h,
            json={
                "request_id": str(uuid4()),
                "expected_version": packet["lead"]["version"],
                "status": "no_show",
            },
        )
        assert invalid.status_code == 422, "The rep must choose what happens after a no-show"
        resumed = again.post(
            f"/v1/leads/{lead_id}/meeting",
            headers=h,
            json={
                "request_id": str(uuid4()),
                "expected_version": packet["lead"]["version"],
                "status": "no_show",
                "next_action": "callback",
                "callback_at": "2026-10-21T21:00:00+00:00",
            },
        )
        assert resumed.status_code == 200, resumed.text
        final = again.get(f"/v1/leads/{lead_id}/packet", headers=h).json()
        assert final["lead"]["callback_due_at"] == "2026-10-21T21:00:00+00:00"
    assert not carrier.purchases
