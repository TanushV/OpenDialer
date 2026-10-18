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


def test_control_identity_lookup_does_not_decrypt_completed_commands(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    history(service, p, clock)
    decoded = count_decrypted(app, monkeypatch)
    with service.db.transaction() as s:
        first = service.control_intent(s, p, "current", "hangup", {})
        second = service.control_intent(s, p, "current", "hangup", {})
        assert first.id == second.id
    assert len(decoded) <= 5, len(decoded)


def test_watchdogs_select_due_obligations_beyond_many_future_deadlines(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    due = clock() - timedelta(seconds=1)
    future = clock() + timedelta(minutes=2)
    identities = {}
    with service.db.transaction() as s:
        for kind in ("call_attempts", "followup_tasks"):
            for i in range(151):
                deadline = due if i == 150 else future
                value = (
                    {"state": "VM_PENDING", "voicemail_deadline": deadline.isoformat()}
                    if kind == "call_attempts"
                    else {
                        "inbound": True,
                        "inbound_state": "routing",
                        "status": "open",
                        "provider_id": f"incoming-{i}",
                        "inbound_deadline": deadline.isoformat(),
                    }
                )
                row = put(s, kind, p["tenant_id"], p["seller_id"], value)
                if i == 150:
                    identities[kind] = row.id
    decoded = count_decrypted(app, monkeypatch)
    commands, calls = service._control_work(p)
    assert [c["id"] for c in calls] == [identities["call_attempts"]]
    assert [c["call_id"] for c in commands] == [identities["followup_tasks"]]
    assert commands[0]["action"] == "hangup"
    assert len(decoded) <= 15, len(decoded)
    with service.db.transaction(False) as s:
        assert get(s, "followup_tasks", identities["followup_tasks"]).data["inbound_state"] == "ending"


@pytest.mark.asyncio
async def test_callback_reconcile_due_read_does_not_decrypt_recently_checked_calls(
    workspace, app, clock, monkeypatch, carrier
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(151):
            put(
                s,
                "followup_tasks",
                p["tenant_id"],
                p["seller_id"],
                {
                    "inbound": True,
                    "inbound_state": "connected",
                    "status": "completed",
                    "provider_id": f"incoming-{i}",
                    "last_reconciled_at": (clock() - timedelta(seconds=30 if i == 150 else 0)).isoformat(),
                },
                now=clock(),
            )
    seen = []

    async def receipt(command_id, provider_id):
        seen.append(provider_id)
        return {"call_control_id": provider_id, "is_alive": True}

    monkeypatch.setattr(carrier, "reconcile", receipt)
    decoded = count_decrypted(app, monkeypatch)
    await service.reconcile_inbound(p)
    assert seen == ["incoming-150"]
    assert len(decoded) <= 5, len(decoded)
    await service.reconcile_inbound(p)
    assert seen == ["incoming-150"]


def test_control_deadline_migration_preserves_payloads(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from app.migrate import migrate_database
    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite:///{tmp_path}/previous.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0011_model_serving")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO followup_tasks (id,tenant_id,seller_id,data,version,created_at) VALUES ('saved','t','s','{\"human_text\":\"Preserve this note\"}',7,'2026-10-17')"
            )
        )
    migrate_database(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT data,version FROM followup_tasks WHERE id='saved'")).one() == (
            '{"human_text":"Preserve this note"}',
            7,
        )
    inspector = inspect(engine)
    assert "op_control_due_at" in {c["name"] for c in inspector.get_columns("call_attempts")}
    assert {"op_control_due_at", "op_reconcile_at"} <= {
        c["name"] for c in inspector.get_columns("followup_tasks")
    }
    migrate_database(url)
    engine.dispose()
