"""A06/A09/A50: due orphan obligations cannot hide behind healthy calls."""

from datetime import timedelta

import pytest
from app.db import get, put, update
from conftest import start
from test_context_reads import count_decrypted
from test_control_completion import principal


@pytest.mark.parametrize("state", ["HUMAN", "ENDING"])
def test_recovery_selects_due_obligations_before_recent_healthy_calls(
    workspace, app, clock, monkeypatch, state
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for index in range(150):
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "state": "HUMAN",
                    "started_at": clock().isoformat(),
                    "max_call_seconds": 3600,
                    "provider_id": f"healthy-{index}",
                },
                now=clock() - timedelta(minutes=10),
            )
        due = put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "state": state,
                "started_at": (clock() - timedelta(minutes=2)).isoformat(),
                "max_call_seconds": 30,
                "provider_id": "due-original-leg",
            },
            now=clock(),
        )
        identity = due.id
    decoded = count_decrypted(app, monkeypatch)
    _, calls, _ = service._recovery_snapshot(p)
    assert [r["id"] for r in calls] == [identity], "Only currently due call obligations belong in this tick"
    assert len(decoded) <= 10, len(decoded)


@pytest.mark.asyncio
async def test_old_terminal_receipt_cannot_finish_a_rebound_ending_call(workspace, app, carrier, monkeypatch):
    call, service, p = start(workspace), app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), state="ENDING", provider_id="original-leg")

    async def reconcile(command_id, provider_id):
        assert provider_id == "original-leg"
        with service.db.transaction() as s:
            update(get(s, "call_attempts", call["id"]), provider_id="replacement-leg")
        return {"call_control_id": "original-leg", "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    await service.recover(p)
    with service.db.transaction(False) as s:
        current = get(s, "call_attempts", call["id"])
        assert current.data["state"] == "ENDING", "A stale receipt cannot release the current live obligation"
        assert current.data["provider_id"] == "replacement-leg"
