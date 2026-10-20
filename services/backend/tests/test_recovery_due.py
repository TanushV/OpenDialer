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
    assert service._recovery_snapshot(p)[1] == [], (
        "Another worker tick must not immediately repeat the same checks"
    )


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


def test_orphan_due_index_backfills_without_changing_saved_call_content(workspace, app, clock):
    from sqlalchemy import inspect, text

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        call = put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "state": "HUMAN",
                "started_at": clock().isoformat(),
                "max_call_seconds": 300,
            },
        )
        identity, version, content = call.id, call.version, dict(call.data)
    with service.db.engine.begin() as connection:
        connection.execute(
            text("UPDATE call_attempts SET op_due_at=NULL, op_index_version=2 WHERE id=:id"), {"id": identity}
        )
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        saved = get(s, "call_attempts", identity)
        from app.jobs.queue import aware

        assert saved.op_due_at is not None
        assert aware(saved.op_due_at) == clock() + timedelta(seconds=300)
        assert (saved.id, saved.version, saved.data) == (identity, version, content)
    assert "ix_call_attempts_recovery_due" in {
        i["name"] for i in inspect(service.db.engine).get_indexes("call_attempts")
    }


@pytest.mark.asyncio
async def test_a_failed_orphan_check_does_not_block_the_remaining_due_calls(
    workspace, app, clock, carrier, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(2):
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "state": "ENDING",
                    "started_at": clock().isoformat(),
                    "max_call_seconds": 300,
                    "provider_id": f"ending-{i}",
                },
                now=clock() + timedelta(seconds=i),
            )
    seen = []

    async def reconcile(command_id, provider_id):
        seen.append(provider_id)
        raise TimeoutError()

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    await service.recover(p)
    assert set(seen) == {"ending-0", "ending-1"}
    assert service._recovery_snapshot(p)[1] == []
    clock.now += timedelta(seconds=31)
    assert len(service._recovery_snapshot(p)[1]) == 2


@pytest.mark.asyncio
async def test_changed_deadline_before_watchdog_dispatch_prevents_obsolete_hangup(
    workspace, app, clock, monkeypatch
):
    call, service, p = start(workspace), app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(
            get(s, "call_attempts", call["id"]),
            state="HUMAN",
            started_at=(clock() - timedelta(minutes=2)).isoformat(),
            max_call_seconds=60,
        )
    snapshot = service._recovery_snapshot

    def changed(*args):
        result = snapshot(*args)
        with service.db.transaction() as s:
            update(get(s, "call_attempts", call["id"]), max_call_seconds=600)
        return result

    monkeypatch.setattr(service, "_recovery_snapshot", changed)
    await service.recover(p)
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == "HUMAN"


@pytest.mark.asyncio
async def test_failed_status_read_cannot_disable_the_expired_call_watchdog(
    workspace, app, clock, carrier, monkeypatch
):
    call, service, p = start(workspace), app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(
            get(s, "call_attempts", call["id"]),
            state="ENDING",
            started_at=(clock() - timedelta(minutes=2)).isoformat(),
            max_call_seconds=60,
        )
    ended = []

    async def reconcile(*args):
        raise TimeoutError()

    async def hangup(provider_id, command_id):
        ended.append(provider_id)

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    monkeypatch.setattr(carrier, "hangup", hangup)
    await service.recover(p)
    assert ended == [call["provider_id"]]


@pytest.mark.asyncio
async def test_confirmed_original_terminal_leg_releases_its_claim(workspace, app, carrier, monkeypatch):
    from app.db import ActiveClaim
    from sqlalchemy import select

    call, service, p = start(workspace), app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), state="ENDING")

    async def reconcile(command_id, provider_id):
        return {"call_control_id": provider_id, "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    await service.recover(p)
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == "ENDED"
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is None


@pytest.mark.database
def test_concurrent_recovery_ticks_claim_each_due_call_once(workspace, app, clock):
    from concurrent.futures import ThreadPoolExecutor

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        identities = {
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "state": "ENDING",
                    "started_at": clock().isoformat(),
                    "max_call_seconds": 300,
                    "provider_id": f"concurrent-{i}",
                },
            ).id
            for i in range(4)
        }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service._recovery_snapshot(p)[1], range(2)))
    claimed = [call["id"] for batch in results for call in batch]
    assert len(claimed) == len(set(claimed)) == len(identities)
    assert set(claimed) == identities


def test_recovery_deadline_uses_the_saved_lifetime_without_a_hidden_limit(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "state": "HUMAN",
                "started_at": clock().isoformat(),
                "max_call_seconds": 100000,
            },
        )
    assert service._recovery_snapshot(p)[1] == []
