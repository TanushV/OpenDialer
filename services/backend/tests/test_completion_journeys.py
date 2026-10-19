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


def restore_separate_authority(service, p, root):
    """Recover business state into an empty disposable authority with current access."""
    import shutil

    from app.db import put

    from scripts.disposable_database import reset_database

    receipt = service.backup(p)
    root.mkdir(mode=0o700)
    (root / "backups").mkdir(mode=0o700)
    name = receipt["backup_id"] + ".backup"
    shutil.copyfile(service.settings.data_dir / "backups" / name, root / "backups" / name)
    for source in service.settings.data_dir.glob("*-journal*"):
        if source.suffix in {".enc", ".head"}:
            shutil.copyfile(source, root / source.name)
    configuration = service.vault.read(p["tenant_id"])
    url = "sqlite:///" + str(root / "recovered.db")
    if not service.db.sqlite:
        url = service.settings.database_url
        service.db.engine.dispose()
        reset_database(url)  # Exact test URL guard; never an installed or browser authority.
    restored = create_app(
        service.settings.model_copy(update={"data_dir": root, "database_url": url}),
        clock=service.clock,
        carrier=service.simulated_carrier,
    ).state.service
    with restored.db.transaction() as session:
        put(
            session,
            "tenants",
            p["tenant_id"],
            "",
            {"name": "Recovered current authority"},
            object_id=p["tenant_id"],
        )
        put(
            session,
            "users",
            p["tenant_id"],
            p["seller_id"],
            {"role": "owner", "name": "Current authorized rep"},
            object_id=p["user_id"],
        )
    restored.vault.write(p["tenant_id"], configuration)
    assert restored.restore(p, receipt["backup_id"])["outbound_paused"]
    return restored


@pytest.mark.asyncio
async def test_restored_conversation_new_interpretation_and_old_source_correction(
    workspace, app, clock, monkeypatch, tmp_path
):
    import json

    from app.db import get, put
    from test_live_memory import setup, turn

    call, p, requests, changes = setup(workspace, app, monkeypatch)
    source = app.state.service
    turn(source, p, call, "early-source")
    await source.coach_turn(p, call["id"], urgent=True)
    with source.db.transaction() as session:
        state = source._by_key(session, p, "conversation_states", call["id"])
        original_id = state.data["observations"][0]["id"]
        put(
            session,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call["id"], "human_text": "Keep the rep's corrected account context."},
            key=call["id"],
        )
        task = put(
            session,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "lead_id": call["lead_id"],
                "owner_id": p["user_id"],
                "kind": "callback",
                "status": "open",
                "human_edited": True,
                "confirmation_state": "confirmed",
                "action": "Human-confirmed next conversation",
                "due_at": (clock.now + timedelta(days=2)).isoformat(),
            },
            key="retained-human-task",
        )
        task_id, task_data = task.id, dict(task.data)
    clock.now += timedelta(seconds=301)
    target = restore_separate_authority(source, p, tmp_path / "recovered-conversation")
    monkeypatch.setattr(target, "configured_coach", source.configured_coach)
    try:
        assert target.analysis(p, call["id"])["transcript"] == []
        turn(target, p, call, "new-source")
        changes["result"] = {
            "action": "NO_ACTION",
            "state_update": {
                "stage": "discovery",
                "observations": [
                    {
                        "kind": "need",
                        "text": "Procurement could benefit from clearer review steps.",
                        "state": "confirmed",
                        "sources": [{"kind": "observation", "id": original_id, "version": 1}],
                        "replaces": [],
                        "quote": "",
                        "commitment": None,
                    }
                ],
                "rubric_items": [],
                "tracker_matches": [],
                "interpreted_segments": [{"kind": "segment", "id": "new-source", "version": 1}],
            },
        }
        await target.coach_turn(p, call["id"], urgent=True)
        with target.db.transaction(False) as session:
            observations = target._by_key(session, p, "conversation_states", call["id"]).data["observations"]
            dependent_id = next(o["id"] for o in observations if o["kind"] == "need")
        turn(target, p, call, "pending-source")
        changes["during"] = lambda: turn(
            target, p, call, "early-source", revision=2, text="The earlier account description was incorrect."
        )
        result = await target.coach_turn(p, call["id"], urgent=True)
        assert result.get("action") == "NO_ACTION", "The old in-flight answer must not publish"
        with target.db.transaction(False) as session:
            observations = target._by_key(session, p, "conversation_states", call["id"]).data["observations"]
            assert next(o for o in observations if o["id"] == dependent_id)["state"] == "stale"
            assert get(session, "followup_tasks", task_id).data == task_data
            assert (
                target._by_key(session, p, "notes", call["id"]).data["human_text"]
                == "Keep the rep's corrected account context."
            )
        assert "workflow constraint in part 1" in json.dumps(requests[-1]), (
            "Retained context must reach new interpretation"
        )
        assert all(
            r["segment_id"] != "early-source" or r["revision"] == 2
            for r in target.analysis(p, call["id"])["transcript"]
        )
    finally:
        target.db.engine.dispose()


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
