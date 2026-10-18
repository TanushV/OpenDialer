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


@pytest.mark.parametrize("operation", ["token", "dtmf", "voicemail", "webhook"])
def test_control_http_handlers_do_not_move_sql_back_onto_the_loop(workspace, app, clock, operation):
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from test_final_attempt_voicemail import mailbox, voicemail_policy

    client, headers = workspace["client"], workspace["headers"]
    if operation == "token":
        request = partial(client.post, "/v1/telephony/token", headers=headers)
    else:
        if operation == "voicemail":
            voicemail_policy(app, workspace, 1)
        call = start(workspace)
        if operation == "voicemail":
            mailbox(app, workspace, call)
            request = partial(
                client.post, f"/v1/calls/{call['id']}/voicemail", headers=headers, json={"asset_id": "vm"}
            )
        elif operation == "dtmf":
            request = partial(
                client.post, f"/v1/calls/{call['id']}/dtmf", headers=headers, json={"digits": "42#"}
            )
        else:
            private = Ed25519PrivateKey.generate()
            public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            app.state.service.vault.write(
                workspace["tenant_id"], {"telnyx_public_key": base64.b64encode(public).decode()}
            )
            body = json.dumps(
                {
                    "data": {
                        "id": "loop-signature",
                        "event_type": "call.hangup",
                        "payload": {"call_control_id": call["provider_id"]},
                    }
                }
            ).encode()
            timestamp = str(int(clock().timestamp()))
            signature = base64.b64encode(private.sign(timestamp.encode() + b"|" + body)).decode()
            request = partial(
                client.post,
                "/webhooks/telnyx",
                content=body,
                headers={"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp},
            )
    violations, total = [], []

    def inspected(connection, cursor, statement, *rest):
        total.append(1)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        violations.append(statement.split()[0])

    event.listen(app.state.db.engine, "before_cursor_execute", inspected)
    try:
        response = request()
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", inspected)
    assert response.status_code == 200, response.text
    assert total
    assert not violations, f"{operation} HTTP handler executed {len(violations)} SQL statements on the loop"
