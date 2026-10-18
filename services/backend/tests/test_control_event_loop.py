"""A09/A50: slow SQL must not occupy the carrier/live async event loop."""

import asyncio
import threading
import time
from functools import partial

import pytest
from app.db import get, update
from conftest import reserve, start
from sqlalchemy import event


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "dial", "hangup", "controls", "agent", "maintenance"])
async def test_control_operations_keep_all_sql_off_the_live_loop(workspace, app, monkeypatch, operation):
    service, p = app.state.service, principal(workspace)
    if operation == "start":
        reservation = reserve(workspace).json()
        action = partial(
            service.start_call,
            p,
            {"reservation_id": reservation["reservation_id"], "idempotency_key": "loop-start"},
        )
    elif operation == "agent":
        action = partial(service.create_agent, p)
    elif operation == "maintenance":
        action = service.maintenance_tick
        monkeypatch.setattr(service, "_next_optimization", float("inf"), raising=False)
        monkeypatch.setattr(service, "_next_preparation", float("inf"), raising=False)
    else:
        call = start(workspace)
        if operation == "dial":
            with service.db.transaction() as s:
                command = next(
                    r for r in service.scoped(s, "command_outbox", p) if r.data.get("call_id") == call["id"]
                )
                update(command, status="pending")
                lead = get(s, "leads", call["lead_id"])
                update(lead, next_attempt_at=None, last_attempt_at=None)
            action = partial(service.dispatch_call, p, call["id"])
        elif operation == "controls":
            service.event(p, "loop-answer", "call.answered", call["id"])
            action = partial(service.dispatch_controls, p)
        else:
            action = partial(service.hangup, p, call["id"])
    loop_thread = threading.get_ident()
    violations, queries = [], []

    def inspected(connection, cursor, statement, *rest):
        queries.append(statement.split()[0])
        if threading.get_ident() == loop_thread:
            violations.append(statement.split()[0])

    event.listen(service.db.engine, "before_cursor_execute", inspected)
    try:
        await action()
    finally:
        event.remove(service.db.engine, "before_cursor_execute", inspected)
    assert queries, "Fixture must exercise actual persistence"
    assert not violations, f"{operation} executed {len(violations)} SQL statements on the live loop"


@pytest.mark.asyncio
async def test_slow_hangup_storage_does_not_stop_unrelated_async_delivery(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    ticks, running = [], True

    async def heartbeat():
        while running:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.005)

    def delayed(*args):
        time.sleep(0.005)

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    event.listen(service.db.engine, "before_cursor_execute", delayed)
    try:
        await service.hangup(p, call["id"])
        await asyncio.sleep(0.01)
    finally:
        event.remove(service.db.engine, "before_cursor_execute", delayed)
        running = False
        await pulse
    assert len(ticks) >= 5
    assert max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.075
