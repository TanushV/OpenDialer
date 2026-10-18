"""A06/A50: live control maintenance reads active obligations, not closed history."""

from datetime import timedelta

import pytest
from app.db import get, put, update
from test_context_reads import count_decrypted
from test_control_completion import principal


def history(service, p, clock, count=400):
    with service.db.transaction() as s:
        for i in range(count):
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {"state": "FINALIZED", "started_at": clock().isoformat()},
                key=f"old-call-{i}",
            )
            put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "action",
                    "status": "completed",
                    "action": "transcription_stop",
                    "call_id": f"old-call-{i}",
                },
                key=f"old-control-{i}",
            )
            put(
                s,
                "followup_tasks",
                p["tenant_id"],
                p["seller_id"],
                {
                    "inbound": True,
                    "inbound_state": "ended",
                    "status": "completed",
                    "provider_id": f"old-inbound-{i}",
                    "inbound_deadline": (clock() - timedelta(minutes=1)).isoformat(),
                },
                key=f"old-inbound-{i}",
            )


@pytest.mark.asyncio
async def test_idle_control_tick_does_not_decrypt_ended_call_or_command_history(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    history(service, p, clock)
    decoded = count_decrypted(app, monkeypatch)
    result = await service.dispatch_controls(p)
    assert result == {"processed": 0, "voicemail_watchdogs": 0}
    assert len(decoded) <= 10, len(decoded)


@pytest.mark.asyncio
async def test_inbound_reconciliation_excludes_ended_history_but_keeps_completed_live_task(
    workspace, app, clock, monkeypatch, carrier
):
    service, p = app.state.service, principal(workspace)
    history(service, p, clock)
    with service.db.transaction() as s:
        active = put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "inbound": True,
                "inbound_state": "connected",
                "status": "completed",
                "provider_id": "still-live",
                "routing_attempts": [],
            },
        )
        identity = active.id
    seen = []

    async def receipt(command_id, provider_id):
        seen.append(provider_id)
        return {"call_control_id": provider_id, "is_alive": True}

    monkeypatch.setattr(carrier, "reconcile", receipt)
    decoded = count_decrypted(app, monkeypatch)
    await service.reconcile_inbound(p)
    assert seen == ["still-live"]
    assert len(decoded) <= 10, len(decoded)
    with service.db.transaction(False) as s:
        assert get(s, "followup_tasks", identity).data["last_reconciled_at"] == clock().isoformat()


def test_old_inbound_activity_backfill_preserves_human_task_status_and_version(workspace, app, clock):
    from sqlalchemy import text

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        task = put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "inbound": True,
                "inbound_state": "connected",
                "status": "completed",
                "human_text": "Keep my callback note",
            },
        )
        identity, version, content = task.id, task.version, dict(task.data)
    with service.db.engine.begin() as connection:
        connection.execute(
            text("UPDATE followup_tasks SET op_index_version=1, op_active=false WHERE id=:id"),
            {"id": identity},
        )
    service.db.backfill_operational_columns()
    with service.db.transaction() as s:
        task = get(s, "followup_tasks", identity)
        assert task.op_active is True
        assert task.version == version and task.data == content
        update(task, inbound_state="ended")
    with service.db.transaction(False) as s:
        assert get(s, "followup_tasks", identity).op_active is False
