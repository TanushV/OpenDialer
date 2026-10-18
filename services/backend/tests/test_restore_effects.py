"""A06/A49/A54: rollback cannot undo external effects or their liabilities."""

import pytest
from app.db import ActiveClaim, doc, get, put, update
from conftest import start
from fastapi import HTTPException
from sqlalchemy import select
from test_control_completion import principal


def test_restore_keeps_a_call_submitted_after_the_backup_and_its_live_claim(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    call = start(workspace)
    with service.db.transaction(False) as s:
        commands = {r.id: dict(r.data) for r in service.scoped(s, "command_outbox", p)}
        claims = [r.id for r in s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call["id"]))]
        assert claims
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"])
        assert saved is not None, "Restoring older business data cannot erase an actual submitted call"
        assert saved.data["provider_id"] == call["provider_id"]
        assert [
            r.id for r in s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call["id"]))
        ] == claims
        for identity, data in commands.items():
            assert get(s, "command_outbox", identity).data["status"] == data["status"]


def test_restore_does_not_resurrect_a_finished_call_or_completed_command(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    backup = service.backup(p)
    response = workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert response.status_code == 200
    with service.db.transaction(False) as s:
        command = service._by_key(s, p, "command_outbox", call["id"] + ":hangup")
        identity, status = command.id, command.data["status"]
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == "ENDED"
        assert get(s, "command_outbox", identity).data["status"] == status


def test_restore_keeps_later_settled_cost_and_its_attributed_credit(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        hold = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "optional_work_reservation",
                "request_key": "original-request",
                "status": "reserved",
                "reserved_usd": 0.01,
                "cost_usd": None,
            },
            key="optional:original-request",
        )
        identity = hold.id
    backup = service.backup(p)
    with service.db.transaction() as s:
        update(
            get(s, "usage_ledger", identity),
            status="settled",
            provider="openrouter",
            request_id="provider-original",
            reserved_usd=0,
            cost_usd=0.008,
            credited_usd=0.002,
            billing_verified=True,
        )
        before = doc(get(s, "usage_ledger", identity))
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert doc(get(s, "usage_ledger", identity)) == before


def test_restore_keeps_prior_uncertain_number_effects_and_their_original_targets(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    with service.db.transaction() as s:
        command = put(
            s,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "number_configure",
                "status": "uncertain",
                "provider_id": "original-owned-number",
                "connection_id": "original-route",
                "request_key": "original-configuration",
            },
            key="number-configure:original-owned-number",
        )
        identity, content = command.id, doc(command)
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        saved = get(s, "command_outbox", identity)
        assert saved is not None
        assert doc(saved) == content


def test_restore_preserves_dependencies_of_newly_submitted_calls(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    with service.db.transaction() as s:
        for lead in service.scoped(s, "leads", p):
            update(lead, status="needs_review")
    result = workspace["client"].post(
        "/v1/leads/import",
        headers=workspace["headers"],
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [
                {
                    "name": "Later legitimate contact",
                    "phone": "+14155550777",
                    "timezone": "America/Los_Angeles",
                }
            ],
        },
    )
    assert result.status_code == 200, result.text
    call = start(workspace)
    with service.db.transaction(False) as s:
        endpoint_id = get(s, "leads", call["lead_id"]).data["endpoint_id"]
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "leads", call["lead_id"]) is not None
        assert get(s, "phone_endpoints", endpoint_id) is not None
        assert get(s, "reservations", call["reservation_id"]) is not None


def test_restore_does_not_erase_later_goal_holds_tasks_or_human_notes(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    call = start(workspace)
    with service.db.transaction() as s:
        update(get(s, "leads", call["lead_id"]), outreach_hold=True, meeting_status="booked")
        task = put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": call["lead_id"],
                "call_id": call["id"],
                "status": "completed",
                "human_edited": True,
            },
        )
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "human_text": "Rep confirmed a meeting independently; keep this note.",
            },
            key=call["id"],
        )
        identities = task.id, note.id
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "leads", call["lead_id"]).data.get("outreach_hold") is True
        assert get(s, "followup_tasks", identities[0]).data["status"] == "completed"
        assert get(s, "notes", identities[1]).data["human_text"].startswith("Rep confirmed")


def test_restore_preserves_durable_inbox_completion_instead_of_replaying_old_events(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        inbox = put(
            s,
            "webhook_inbox",
            p["tenant_id"],
            p["seller_id"],
            {
                "status": "received",
                "event_id": "original-carrier-event",
                "payload_hash": "saved-hash",
            },
            key="original-carrier-event",
        )
        identity = inbox.id
    backup = service.backup(p)
    with service.db.transaction() as s:
        update(get(s, "webhook_inbox", identity), status="processed")
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "webhook_inbox", identity).data["status"] == "processed"


def test_restore_recovers_missing_records_and_retains_current_business_corrections(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        first, second, *_ = service.scoped(s, "leads", p)
        missing, current = doc(first), doc(second)
    backup = service.backup(p)
    with service.db.transaction() as s:
        s.delete(get(s, "leads", missing["id"]))
        update(get(s, "leads", current["id"]), name="Newer rep correction")
    receipt = service.restore(p, backup["backup_id"])
    assert receipt["mode"] == "recover_missing_keep_current"
    with service.db.transaction(False) as s:
        assert doc(get(s, "leads", missing["id"])) == missing
        assert get(s, "leads", current["id"]).data["name"] == "Newer rep correction"
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert doc(get(s, "leads", missing["id"])) == missing
        assert get(s, "leads", current["id"]).data["name"] == "Newer rep correction"


def test_restore_rejects_a_missing_identity_now_owned_by_another_seller_atomically(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        first, second, *_ = service.scoped(s, "leads", p)
        missing, conflict = first.id, second.id
    backup = service.backup(p)
    with service.db.transaction() as s:
        s.delete(get(s, "leads", missing))
        row = get(s, "leads", conflict)
        row.seller_id = "separate-business"
    with pytest.raises(HTTPException) as error:
        service.restore(p, backup["backup_id"])
    assert error.value.status_code == 409
    with service.db.transaction(False) as s:
        assert get(s, "leads", missing) is None
        assert get(s, "leads", conflict).seller_id == "separate-business"


@pytest.mark.parametrize("kind", ["suppressions", "consent_evidence"])
def test_backup_denial_cannot_be_lost_when_current_database_is_older(workspace, app, kind):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        row = put(
            s,
            kind,
            p["tenant_id"],
            p["seller_id"],
            {
                "phone": "+14155550778",
                "endpoint_id": "original-endpoint",
                "permissions": ["live"],
                "active": True,
                "permanent": True,
                "reason": "opt_out",
                "revoked": True,
            },
            key="backup-denial",
        )
        identity = row.id
    backup = service.backup(p)
    with service.db.transaction() as s:
        update(get(s, kind, identity), active=False, permanent=False, revoked=False)
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        data = get(s, kind, identity).data
        assert data["active"] if kind == "suppressions" else data["revoked"]


@pytest.mark.asyncio
async def test_restore_reconciliation_includes_live_calls_missing_from_journal(
    workspace, app, carrier, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(get(s, "sellers", p["seller_id"]), restore_hold=True)
        put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "state": "ENDING",
                "provider_id": "unjournaled-original-leg",
            },
        )
    checked = []

    async def reconcile(call_id, provider_id):
        checked.append(provider_id)
        return {"call_control_id": provider_id, "is_alive": True}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        await service.reconcile_restore(p, "Reviewed synthetic carrier records")
    assert error.value.status_code == 409
    assert checked == ["unjournaled-original-leg"]
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is True


@pytest.mark.asyncio
async def test_a_new_live_obligation_during_restore_reconciliation_prevents_release(
    workspace, app, carrier, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        update(get(s, "sellers", p["seller_id"]), restore_hold=True)

    async def reconcile(call_id, provider_id):
        with service.db.transaction() as s:
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "state": "ENDING",
                    "provider_id": "additional-original-leg",
                },
            )
        return {"call_control_id": call["provider_id"], "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        await service.reconcile_restore(p, "Reviewed previous snapshot")
    assert error.value.status_code == 409
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is True
