"""E10/C039: control crash interleavings preserve the original operation and owner."""

from datetime import timedelta

import pytest
from app.db import get, update
from app.providers.simulated import UncertainCarrierResult
from conftest import disposition_body, reserve, start
from test_dnc_recovery import screening  # noqa: F401 - pytest fixture
from test_remediation_privacy import principal

OPERATIONS = [
    "answer",
    "bridge",
    "transfer",
    "transcription_start",
    "gather",
    "speak",
    "transcription_stop",
    "playback_stop",
    "record_stop",
    "hangup",
    "send_dtmf",
    "playback_start",
]
PHASES = ["before_send", "after_send", "before_publication", "expired_lease", "stale_owner"]


def test_operation_policy_covers_each_effect_without_a_generic_retry():
    from app.telephony import recovery

    policies = getattr(recovery, "OPERATION_POLICIES", {})
    expected = set(OPERATIONS) | {
        "dial",
        "agent",
        "number_configure",
        "number_purchase",
        "dnc_screen",
        "inference",
        "review",
        "local_task",
        "local_outcome",
    }
    assert set(policies) == expected
    for policy in policies.values():
        assert policy.handler and policy.proof and policy.uncertain_action
    for operation in {"hangup", "playback_stop", "transcription_stop", "record_stop"}:
        assert policies[operation].repeat == "safe_stop"
    assert policies["transfer"].repeat == "original_command_window"
    assert policies["dnc_screen"].repeat == "explicit_charge_acknowledgement"
    assert policies["number_purchase"].repeat == "never"


@pytest.mark.asyncio
async def test_agent_reconciliation_requires_current_unexpired_lease(
    workspace, app, carrier, clock, monkeypatch
):
    from app.db import put

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        leg = put(
            s,
            "call_legs",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "agent",
                "rep_id": p["user_id"],
                "provider_id": "known-agent",
                "state": "uncertain",
            },
        )
        identity, version = leg.id, leg.version

    async def delayed(*_):
        clock.now += timedelta(seconds=31)
        return {"call_control_id": "known-agent", "is_alive": True}

    monkeypatch.setattr(carrier, "reconcile", delayed)
    await service.reconcile_agent(p, identity, version)
    with service.db.transaction(False) as s:
        row = get(s, "call_legs", identity)
        assert row.data["state"] == "uncertain"
        assert row.data["lease_owner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["live_coach", "live_coach_review"])
@pytest.mark.parametrize("phase", PHASES)
async def test_inference_attempt_crash_matrix(workspace, app, clock, monkeypatch, role, phase):
    from uuid import uuid4

    from test_live_claim_review import review_fixture, usage

    service, p, call, requests, _ = review_fixture(workspace, app, monkeypatch)
    configured = service.configured_coach
    crossed = []

    def provider_for(principal):
        provider, route = configured(principal)
        method = "suggest" if role == "live_coach" else "review"
        original = getattr(provider, method)

        async def interrupted(request, **kwargs):
            crossed.append(method)
            if phase == "before_send":
                raise TimeoutError("Synthetic connection failure")
            result = await original(request, **kwargs)
            if phase == "after_send":
                raise TimeoutError("Synthetic provider response interruption")
            if phase == "expired_lease":
                clock.now += timedelta(seconds=301)
            if phase == "stale_owner":
                with service.db.transaction() as s:
                    update(
                        service.approved_playbook(s, p), product_description="Prior access claims withdrawn."
                    )
            return result

        setattr(provider, method, interrupted)
        return provider, route

    monkeypatch.setattr(service, "configured_coach", provider_for)
    if phase == "before_publication":

        def lost(*args, **kwargs):
            raise RuntimeError("Synthetic publication interruption")

        monkeypatch.setattr(service, "_publish_help", lost)
    try:
        result = await service.coach_turn(
            p, call["id"], urgent=True, question="Help me answer", request_id=str(uuid4())
        )
        fallback_allowed = role == "live_coach" and phase in {"before_send", "after_send"}
        assert result["action"] == "NO_ACTION" or (
            fallback_allowed and result.get("say_or_ask") != "Our tool can be used through a web browser."
        )
    except RuntimeError:
        assert phase == "before_publication"
    assert crossed == ["suggest" if role == "live_coach" else "review"]
    charges = [row for row in usage(service, p) if row["work_kind"] == role]
    assert len(charges) == 1
    assert charges[0]["status"] in {"settled", "uncertain", "released"}
    if phase != "before_send":
        assert charges[0]["status"] == "settled" and charges[0]["cost_usd"] > 0
    with service.db.transaction(False) as s:
        if not (role == "live_coach" and phase in {"before_send", "after_send"}):
            assert not service.scoped(s, "coach_events", p)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", PHASES)
async def test_prior_purchase_read_recovery_matrix(workspace, app, clock, carrier, monkeypatch, phase):
    from app.db import doc
    from fastapi import HTTPException
    from test_number_orders import legacy_order

    service, p = app.state.service, principal(workspace)
    order = legacy_order(workspace, app, provider_order_id="original-order")
    reads = []

    async def original_order(identity):
        reads.append(identity)
        if phase == "after_send" and len(reads) == 1:
            raise TimeoutError("Lost read response")
        if phase == "stale_owner" and len(reads) == 1:
            with service.db.transaction() as s:
                update(get(s, "command_outbox", order["id"]), lease_owner="replacement-owner")
        return {
            "id": identity,
            "status": "success",
            "phone_numbers": [
                {"phone_number": n, "status": "success", "requirements_met": True} for n in order["numbers"]
            ],
        }

    monkeypatch.setattr(carrier, "get_number_order", original_order, raising=False)
    finish = service._finish_order_check

    def lost_publication(*args, **kwargs):
        raise RuntimeError("Synthetic order receipt publication loss")

    if phase == "before_publication":
        monkeypatch.setattr(service, "_finish_order_check", lost_publication)
    body = {"expected_version": order["version"]}
    if phase in {"before_send", "expired_lease"}:
        service._claim_order_check(p, order["id"], body)
    else:
        try:
            await service.reconcile_number_order(p, order["id"], body)
        except RuntimeError:
            assert phase == "before_publication"
        except HTTPException as exc:
            assert exc.status_code in {409, 502}
    monkeypatch.setattr(service, "_finish_order_check", finish)
    clock.now += timedelta(seconds=121)
    with service.db.transaction(False) as s:
        current = doc(get(s, "command_outbox", order["id"]))
    recovered = await service.reconcile_number_order(p, order["id"], {"expected_version": current["version"]})
    assert recovered["id"] == order["id"] and recovered["status"] == "completed"
    assert recovered["committed_usd"] == 2
    assert reads and set(reads) == {"original-order"} and not carrier.purchases


@pytest.mark.parametrize("phase", PHASES)
def test_local_task_edit_crash_matrix(workspace, app, clock, monkeypatch, phase):
    from contextlib import contextmanager
    from uuid import uuid4

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    with service.db.transaction(False) as s:
        version = get(s, "call_attempts", call["id"]).version
    body = {
        "outcome": "callback_requested",
        "contact_role": "right_party",
        "callback_at": (clock.now + timedelta(days=1)).isoformat(),
        "request_id": str(uuid4()),
        "expected_version": version,
    }
    transaction = service.db.transaction
    crashed = []

    @contextmanager
    def rollback_before_commit(write=True, *args, **kwargs):
        with transaction(write, *args, **kwargs) as s:
            yield s
            if write and not crashed:
                crashed.append(True)
                raise RuntimeError("Synthetic local commit interruption")

    if phase == "before_publication":
        monkeypatch.setattr(service.db, "transaction", rollback_before_commit)
    if phase != "before_send":
        try:
            response = workspace["client"].post(
                f"/v1/calls/{call['id']}/disposition", headers=workspace["headers"], json=body
            )
            assert response.status_code == 200, response.text
        except RuntimeError:
            assert phase == "before_publication"
    monkeypatch.setattr(service.db, "transaction", transaction)
    clock.now += timedelta(seconds=121)
    # Local writes have no external lease to renew: request digest/version is the fence.
    for _ in range(2):
        result = workspace["client"].post(
            f"/v1/calls/{call['id']}/disposition", headers=workspace["headers"], json=body
        )
        assert result.status_code == 200, result.text
    with service.db.transaction(False) as s:
        tasks = [r for r in service.scoped(s, "followup_tasks", p) if r.data.get("call_id") == call["id"]]
        assert len(tasks) == 1 and tasks[0].data["due_at"] == body["callback_at"]
    changed = workspace["client"].post(
        f"/v1/calls/{call['id']}/disposition",
        headers=workspace["headers"],
        json=disposition_body(
            workspace["client"], workspace["headers"], call["id"], **{**body, "outcome": "no_answer"}
        ),
    )
    assert changed.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", PHASES)
async def test_prepaid_screen_crash_matrix(workspace, app, clock, screening, monkeypatch, phase):  # noqa: F811 - fixture
    import app.compliance.screening as protocol
    from app.providers.dnc import DNCError, PrepaidDNC
    from fastapi import HTTPException

    service, p, calls, original_check, _ = screening
    intent, _, config = protocol.claim(service, p, "+14155550101")
    key = intent["request_key"]

    async def check(provider, phone):
        result = await original_check(provider, phone)
        if phase == "after_send":
            raise DNCError("Synthetic lost lookup response")
        if phase == "stale_owner":
            with service.db.transaction() as s:
                update(get(s, "registry_snapshots", intent["id"]), lease_owner="replacement-owner")
        return result

    monkeypatch.setattr(PrepaidDNC, "check", check)
    original_finish = protocol.finish

    def interrupted(*args, **kwargs):
        raise RuntimeError("Synthetic loss before publication")

    if phase == "before_publication":
        monkeypatch.setattr(protocol, "finish", interrupted)
    if phase not in {"before_send", "expired_lease"}:
        try:
            await protocol.execute(service, p, intent, config)
        except RuntimeError:
            assert phase == "before_publication"
        except HTTPException as exc:
            assert exc.status_code in {409, 503}
    monkeypatch.setattr(protocol, "finish", original_finish)
    clock.now += timedelta(seconds=121)
    # A view can identify uncertainty; it cannot buy a replacement lookup.
    response = workspace["client"].get("/v1/admin/dnc/screens", headers=workspace["headers"])
    assert response.status_code == 200
    with pytest.raises(HTTPException) as denied:
        protocol.claim(service, p, "+14155550101")
    assert denied.value.status_code == 409
    assert len(calls) == (0 if phase in {"before_send", "expired_lease"} else 1)
    with service.db.transaction(False) as s:
        row = get(s, "registry_snapshots", intent["id"])
        assert row.data["request_key"] == key
        assert row.data.get("listed") is not False
        holds = [r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("request_key") == key]
        # An answered lookup that lost publication keeps its charge as an estimate.
        assert len(holds) == 1 and holds[0]["status"] in {"reserved", "uncertain", "estimated"}
        assert holds[0]["status"] != "estimated" or holds[0]["cost_usd"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", PHASES)
async def test_owned_number_configuration_crash_matrix(workspace, app, clock, carrier, monkeypatch, phase):
    from app.db import doc
    from fastapi import HTTPException
    from test_number_reconciliation import configure_fixture

    service, p, number_id = configure_fixture(workspace, app)
    sent, reads = [], []
    receipt = {"id": "owned", "connection_id": "desired"}

    async def configure(identity, connection):
        sent.append((identity, connection))
        if phase == "after_send":
            raise TimeoutError("Synthetic lost applied response")
        if phase == "stale_owner":
            with service.db.transaction() as s:
                row = next(
                    r
                    for r in service.scoped(s, "command_outbox", p)
                    if r.data.get("kind") == "number_configure"
                )
                update(
                    row,
                    lease_owner="replacement-owner",
                    lease_until=(clock.now + timedelta(seconds=30)).isoformat(),
                )
        return receipt

    async def retrieve(identity):
        reads.append(identity)
        return receipt

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    monkeypatch.setattr(carrier, "get_number", retrieve, raising=False)
    original_finish = service._finish_number_configuration

    def interrupted(*args, **kwargs):
        raise RuntimeError("Synthetic crash after configuration receipt")

    if phase == "before_publication":
        monkeypatch.setattr(service, "_finish_number_configuration", interrupted)
    if phase in {"before_send", "expired_lease"}:
        service._claim_number_configuration(p, number_id, "desired")
    else:
        try:
            await service.configure_owned_number(p, number_id)
        except RuntimeError:
            assert phase == "before_publication"
        except HTTPException as exc:
            assert exc.status_code in {409, 503}
    monkeypatch.setattr(service, "_finish_number_configuration", original_finish)
    with service.db.transaction(False) as s:
        before = next(
            doc(r) for r in service.scoped(s, "command_outbox", p) if r.data.get("kind") == "number_configure"
        )
        if phase == "stale_owner":
            assert before["lease_owner"] == "replacement-owner" and before["status"] != "completed"
    clock.now += timedelta(seconds=121)
    await service.configure_owned_number(p, number_id)
    with service.db.transaction(False) as s:
        after = get(s, "command_outbox", before["id"])
        assert after.data["status"] == "completed" and after.data["provider_id"] == "owned"
    assert sent == ([] if phase in {"before_send", "expired_lease"} else [("owned", "desired")])
    assert reads == ["owned"] and not carrier.purchases


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dial", "agent"])
@pytest.mark.parametrize("phase", PHASES)
async def test_dial_and_agent_crash_matrix(workspace, app, clock, carrier, monkeypatch, operation, phase):
    from app.db import doc
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    if operation == "agent":
        service.settings.simulation = False
        monkeypatch.setattr(service, "carrier", lambda _: carrier)
        service.vault.write(
            p["tenant_id"], {"sip_username": "test-rep", "telephony_credential_id": "test-cred"}
        )

        async def verified(_, **__):
            return True

        monkeypatch.setattr(carrier, "verify_incoming_only_credential", verified, raising=False)
    else:
        reservation = reserve(workspace).json()["reservation_id"]

    def saved():
        with service.db.transaction(False) as s:
            table = "call_legs" if operation == "agent" else "command_outbox"
            rows = [doc(row) for row in service.scoped(s, table, p) if row.data.get("kind") == operation]
            assert len(rows) == 1
            return table, rows[0]

    original, submitted = carrier.dial, []

    async def interrupted(payload):
        if phase == "before_send":
            raise UncertainCarrierResult("Synthetic interruption before submission")
        receipt = await original(payload)
        submitted.append(payload["command_id"])
        if phase == "after_send":
            raise UncertainCarrierResult("Synthetic lost response")
        if phase == "stale_owner":
            table, row = saved()
            with service.db.transaction() as s:
                update(
                    get(s, table, row["id"]),
                    lease_owner="replacement-owner",
                    lease_until=(clock.now + timedelta(seconds=60)).isoformat(),
                )
        return receipt

    monkeypatch.setattr(carrier, "dial", interrupted)
    method = "_agent_completed" if operation == "agent" else "_dial_completed"
    original_finish = getattr(service, method)

    def interrupted_finish(*args, **kwargs):
        raise RuntimeError("Synthetic interruption before receipt publication")

    if phase in {"before_publication", "expired_lease"}:
        monkeypatch.setattr(service, method, interrupted_finish)
    try:
        if operation == "agent":
            await service.create_agent(p)
        else:
            result = workspace["client"].post(
                "/v1/calls/start",
                headers=workspace["headers"],
                json={"reservation_id": reservation, "idempotency_key": "matrix-original"},
            )
            assert result.status_code in {200, 409, 503}, result.text
    except (RuntimeError, HTTPException):
        assert phase in {"before_send", "after_send", "before_publication", "expired_lease", "stale_owner"}
    table, before = saved()
    if phase == "stale_owner":
        assert before["lease_owner"] == "replacement-owner"
        assert before.get("status", before.get("state")) not in {"completed", "ringing"}
        return
    monkeypatch.setattr(service, method, original_finish)
    clock.now += timedelta(seconds=121)
    await service.recover(p)
    _, after = saved()
    assert after["id"] == before["id"]
    assert after.get("status", after.get("state")) not in {"sending", "submitting"}
    assert len(submitted) <= 1 and len(carrier.dials) <= 1
    if phase != "before_send":
        assert len(submitted) == 1, "The fixture must actually cross the external submission boundary"
    assert after.get("provider_id") in {
        None,
        before.get("provider_id"),
        *(r["call_control_id"] for r in carrier.calls.values()),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", OPERATIONS)
@pytest.mark.parametrize("phase", PHASES)
async def test_all_operation_crash_phases(workspace, app, clock, carrier, monkeypatch, action, phase):
    service, p = app.state.service, principal(workspace)
    if action == "playback_start":
        from test_final_attempt_voicemail import voicemail_policy

        voicemail_policy(app, workspace, 1)
    call = start(workspace)
    with service.db.transaction() as s:
        payload = {"text": "Neutral callback message"} if action == "speak" else {}
        if action == "playback_start":
            update(get(s, "call_attempts", call["id"]), state="VM_PENDING")
            payload = {"media_name": "vm"}
        row = service.control_intent(s, p, call["id"], action, payload)
        if action == "send_dtmf":
            update(
                row,
                requested_by=p["user_id"],
                target=call["provider_id"],
                expires_at=(clock.now + timedelta(seconds=30)).isoformat(),
                payload={**row.data["payload"], "digits": "42#"},
            )
        identity, original_key = row.id, row.external_key
    original = {"hangup": carrier.hangup, "playback_start": carrier.playback}.get(action, carrier.action)
    submissions = []

    async def interrupted(provider_id, action, payload):
        submissions.append((provider_id, action, dict(payload)))
        if action == "hangup":
            result = await original(provider_id, payload["command_id"])
        elif action == "playback_start":
            result = await original(provider_id, payload)
        else:
            result = await original(provider_id, action, payload)
        if phase == "stale_owner":
            with service.db.transaction() as s:
                update(
                    get(s, "command_outbox", identity),
                    lease_owner="replacement-owner",
                    lease_until=(clock.now + timedelta(seconds=60)).isoformat(),
                )
        if phase == "after_send":
            raise UncertainCarrierResult("Synthetic lost response")
        return result

    monkeypatch.setattr(carrier, "action", interrupted)
    if action == "playback_start":

        async def playback(target, payload):
            return await interrupted(target, "playback_start", payload)

        monkeypatch.setattr(carrier, "playback", playback)
    elif action == "hangup":

        async def hangup(target, command_id):
            return await interrupted(target, "hangup", {"command_id": command_id})

        monkeypatch.setattr(carrier, "hangup", hangup)
    if phase == "before_publication":
        finish = service._control_completed

        def lose_publication(*args, **kwargs):
            raise RuntimeError("Synthetic crash after carrier receipt before publication")

        monkeypatch.setattr(service, "_control_completed", lose_publication)
    if phase == "before_send":
        # Crash after the durable claim, before any carrier submission.
        assert service._claim_control(p, identity) is not None
        assert submissions == []
    elif phase == "expired_lease":
        with service.db.transaction() as s:
            update(
                get(s, "command_outbox", identity),
                status="sending",
                lease_owner="dead-owner",
                lease_until=(clock.now - timedelta(seconds=1)).isoformat(),
            )
    else:
        try:
            await service.dispatch_controls(p, command_id=identity)
        except RuntimeError:
            assert phase == "before_publication"
    if phase == "before_publication":
        monkeypatch.setattr(service, "_control_completed", finish)
    if phase not in {"before_send", "expired_lease"}:
        assert len(submissions) == 1, "The crash fixture must reach the actual carrier boundary"
    if phase == "stale_owner":
        with service.db.transaction(False) as s:
            row = get(s, "command_outbox", identity)
            assert row.data.get("lease_owner") == "replacement-owner" or row.data["status"] == "cancelled"
        return
    clock.now += timedelta(seconds=121)
    await service.recover(p)
    await service.dispatch_controls(p, command_id=identity)
    with service.db.transaction(False) as s:
        row = get(s, "command_outbox", identity)
        assert row.external_key == original_key
        assert row.data["status"] != "sending", (
            "Expired ownership must become recovered or explicitly reviewable"
        )
        assert row.data.get("target") in {None, call["provider_id"]}
        if row.data["status"] in {"uncertain", "review_required"}:
            assert row.data.get("recovery_reason") or row.data.get("error") or row.data.get("next_attempt_at")
    if action not in {"hangup", "transcription_stop", "playback_stop", "record_stop"}:
        assert len(submissions) <= 1, (
            "An uncertain start/transfer cannot be blindly sent again after dedupe expiry"
        )
    assert all(target == call["provider_id"] for target, _, _ in submissions)
