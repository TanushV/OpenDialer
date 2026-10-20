"""Real inbound handling, not a task mislabelled as a live queue (A11)."""

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from app.db import ActiveClaim, doc, get, put, rows, update
from conftest import ready, reserve, start
from sqlalchemy import select


@pytest.mark.parametrize("matched", [False, True])
def test_signed_events_do_not_decrypt_unrelated_task_history(workspace, app, monkeypatch, matched):
    from test_context_reads import count_decrypted

    call = start(workspace)
    task = incoming(workspace, provider_id="indexed-inbound", event_id="indexed-create") if matched else None
    service = app.state.service
    with service.db.transaction() as s:
        for i in range(300):
            put(
                s,
                "followup_tasks",
                workspace["tenant_id"],
                workspace["seller_id"] if i % 2 else "another-seller",
                {"status": "completed", "inbound": True, "inbound_state": "ended", "provider_id": f"old-{i}"},
                key=f"old-callback-{i}",
            )
    decoded = count_decrypted(app, monkeypatch)
    webhook(
        workspace,
        "call.hangup" if matched else "call.answered",
        {
            "call_control_id": "indexed-inbound" if matched else call["provider_id"],
            **(
                {}
                if matched
                else {
                    "client_state": base64.b64encode(
                        json.dumps({"call_id": call["id"], "kind": "prospect"}).encode()
                    ).decode()
                }
            ),
        },
        event_id="indexed-delivery",
    )
    assert len(decoded) < 100, "A signed event must not decrypt task history"
    if matched:
        assert saved(workspace, "followup_tasks", task["id"])["caller_ended"] is True
    else:
        assert saved(workspace, "call_attempts", call["id"])["state"] == "ANSWERED_UNCLASSIFIED"


def test_inbound_provider_lookup_has_a_tenant_scoped_index(app):
    from sqlalchemy import inspect

    indexes = inspect(app.state.db.engine).get_indexes("followup_tasks")
    assert any(i["column_names"] == ["tenant_id", "op_provider_id"] for i in indexes)


def test_duplicate_inbound_provider_identity_requires_review_without_mutation(workspace, app):
    from fastapi import HTTPException

    identities = []
    with app.state.db.transaction() as session:
        for seller in (workspace["seller_id"], "other-seller"):
            row = put(
                session,
                "followup_tasks",
                workspace["tenant_id"],
                seller,
                {
                    "inbound": True,
                    "provider_id": "ambiguous-provider",
                    "inbound_state": "ringing",
                    "status": "open",
                },
            )
            identities.append((row.id, dict(row.data)))
    with pytest.raises(HTTPException) as error:
        webhook(workspace, "call.hangup", {"call_control_id": "ambiguous-provider"})
    assert error.value.status_code == 409 and "ambiguous" in str(error.value.detail)
    with app.state.db.transaction(False) as session:
        assert [
            (identity, get(session, "followup_tasks", identity).data) for identity, _ in identities
        ] == identities


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def deliver(w, event_type, payload, event_id=None):
    """The production path: a durable signed inbox receipt, then the webhook worker."""
    service = w["client"].app.state.service
    event = {"id": event_id or str(uuid4()), "event_type": event_type, "payload": payload}
    body = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    receipt = asyncio.run(service.accept_webhook(w["tenant_id"], event, body, verified=True))
    if receipt.get("duplicate"):
        return receipt
    return asyncio.run(service.process_webhook(w["tenant_id"], receipt["receipt_id"]))


def incoming(w, *, provider_id="inbound-1", event_id="incoming-1", phone="+14155550101"):
    payload = {"call_control_id": provider_id, "to": "+14155550100", "from": phone, "direction": "incoming"}
    result = deliver(w, "call.initiated", payload, event_id)
    service = w["client"].app.state.service
    with service.db.transaction(False) as s:
        task = service._by_key(s, principal(w), "followup_tasks", "callback:" + provider_id)
    return doc(task) if task else result


def dispatch(w):
    service = w["client"].app.state.service
    asyncio.run(service.dispatch_controls(principal(w)))
    asyncio.run(service.dispatch_controls(principal(w)))


def webhook(w, event_type, payload, event_id=None):
    return deliver(w, event_type, payload, event_id)


def saved(w, table, identity):
    service = w["client"].app.state.service
    with service.db.transaction(False) as s:
        return doc(get(s, table, identity))


def previous_call(w):
    call = start(w)
    w["client"].post(f"/v1/calls/{call['id']}/hangup", headers=w["headers"])
    service = w["client"].app.state.service
    with service.db.transaction() as s:
        update(get(s, "users", w["user_id"]), sip_username="responsible")
        phone = get(s, "leads", call["lead_id"]).data["phone"]
    ready(w)
    return call, phone


def teammate(w, clock, *, other_seller=False):
    service = w["client"].app.state.service
    with service.db.transaction() as s:
        user = put(
            s,
            "users",
            w["tenant_id"],
            "different-seller" if other_seller else w["seller_id"],
            {
                "name": "Teammate",
                "role": "rep",
                "sip_username": "teammate",
                "presence": {
                    "status": "ready",
                    "audio_ready": True,
                    "telephony_ready": True,
                    "at": clock().isoformat(),
                },
            },
            now=clock(),
        )
        return user.id


def configure(w, **values):
    c, h = w["client"], w["headers"]
    current = c.get("/v1/admin/callback-routing", headers=h)
    assert current.status_code == 200, current.text
    result = c.put(
        "/v1/admin/callback-routing",
        headers=h,
        json={
            "expected_version": current.json()["version"],
            **values,
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


def transfer(carrier):
    return next(a for a in reversed(carrier.actions) if a["action"] == "transfer")


def target_payload(carrier, provider_id="destination-leg"):
    return {
        "call_control_id": provider_id,
        "client_state": transfer(carrier)["payload"]["target_leg_client_state"],
        "direction": "outgoing",
    }


def test_default_fallback_answers_then_speaks_and_ends_without_losing_task(workspace, carrier):
    task = incoming(workspace)
    dispatch(workspace)
    assert [a["action"] for a in carrier.actions] == ["answer", "speak"]
    speech = carrier.actions[-1]["payload"]
    assert speech["target_legs"] == "self" and speech["service_level"] == "basic"
    assert "Example Seller" not in speech["payload"]  # neutral; no guessed relationship
    assert saved(workspace, "followup_tasks", task["id"])["status"] == "open"
    webhook(workspace, "call.speak.ended", {"call_control_id": "inbound-1"})
    dispatch(workspace)
    assert [h["provider_id"] for h in carrier.hangups] == ["inbound-1"]
    webhook(workspace, "call.hangup", {"call_control_id": "inbound-1"}, "end")
    state = saved(workspace, "followup_tasks", task["id"])
    assert state["inbound_state"] == "ended" and state["status"] == "open"
    assert state["callback_phone"] == "+14155550101"


def test_duplicate_provider_call_with_different_event_ids_has_one_task_and_effect(workspace, carrier):
    one = incoming(workspace)
    two = incoming(workspace, event_id="different-delivery")
    assert one["id"] == two["id"]
    dispatch(workspace)
    assert len([a for a in carrier.actions if a["action"] == "answer"]) == 1


def test_uncertain_answer_never_submits_transfer_or_message(workspace, carrier, monkeypatch):
    async def timeout(*_):
        raise TimeoutError("no acknowledgement")

    monkeypatch.setattr(carrier, "action", timeout)
    task = incoming(workspace)
    dispatch(workspace)
    service = workspace["client"].app.state.service
    with service.db.transaction(False) as s:
        commands = [r.data for r in rows(s, "command_outbox") if r.data.get("call_id") == task["id"]]
    assert any(r["action"] == "answer" and r["status"] == "uncertain" for r in commands)
    assert all(r["status"] == "pending" for r in commands if r["action"] != "answer")


def test_business_fallback_is_configured_versioned_and_does_not_fake_completion(workspace, carrier):
    policy = configure(workspace, destination="sip:reception@example.test", ring_seconds=15)
    task = incoming(workspace)
    dispatch(workspace)
    action = transfer(carrier)
    assert task["route"] == "business_fallback"
    assert action["payload"]["to"] == policy["destination"]
    assert action["payload"]["timeout_secs"] == 15
    assert action["payload"]["time_limit_secs"] > 15
    webhook(workspace, "call.bridged", target_payload(carrier))
    assert saved(workspace, "followup_tasks", task["id"])["status"] == "open"
    stale = workspace["client"].put(
        "/v1/admin/callback-routing",
        headers=workspace["headers"],
        json={"expected_version": policy["version"] - 1, "destination": ""},
    )
    assert stale.status_code == 409


@pytest.mark.parametrize(
    "destination", ["http://bad.test", "sip:a@127.0.0.1", "+19005550100", "sip:a@x.test\r\nX:bad"]
)
def test_fallback_destination_is_bounded_and_cannot_target_private_or_premium_routes(workspace, destination):
    c, h = workspace["client"], workspace["headers"]
    result = c.put(
        "/v1/admin/callback-routing", headers=h, json={"expected_version": 0, "destination": destination}
    )
    assert result.status_code == 422


def test_routing_reserves_rep_and_endpoint_until_terminal_event(workspace, carrier):
    call, phone = previous_call(workspace)
    task = incoming(workspace, phone=phone)
    assert task["route"] == "responsible_rep"
    assert task["previous_call_id"] == call["id"]
    assert reserve(workspace).status_code == 409
    dispatch(workspace)
    webhook(workspace, "call.bridged", target_payload(carrier))
    state = saved(workspace, "followup_tasks", task["id"])
    assert state["status"] == "completed" and state["completion_source"] == "inbound_rep_connected"
    assert reserve(workspace).status_code == 409
    webhook(workspace, "call.hangup", {"call_control_id": "inbound-1"}, "caller-left")
    dispatch(workspace)
    webhook(workspace, "call.hangup", target_payload(carrier), "rep-left")
    assert reserve(workspace).status_code == 200


@pytest.mark.parametrize("unavailable", ["stale", "disabled", "busy", "audio"])
def test_unavailable_owner_routes_to_fresh_same_seller_teammate(workspace, carrier, clock, unavailable):
    _, phone = previous_call(workspace)
    peer = teammate(workspace, clock)
    service = workspace["client"].app.state.service
    with service.db.transaction() as s:
        user = get(s, "users", workspace["user_id"])
        presence = dict(user.data["presence"])
        if unavailable == "stale":
            presence["at"] = (clock() - timedelta(minutes=5)).isoformat()
        elif unavailable == "audio":
            presence["audio_ready"] = False
        elif unavailable == "busy":
            presence["status"] = "busy"
        update(user, presence=presence, disabled=unavailable == "disabled")
    task = incoming(workspace, phone=phone)
    assert task["route"] == "teammate" and task["routed_rep_id"] == peer
    dispatch(workspace)
    assert "teammate@" in transfer(carrier)["payload"]["to"]


def test_other_seller_rep_is_never_rung_and_ambiguous_contact_is_not_guessed(workspace, carrier, clock):
    teammate(workspace, clock, other_seller=True)
    service = workspace["client"].app.state.service
    with service.db.transaction() as s:
        lead = next(r for r in rows(s, "leads") if r.data["phone"] == "+14155550101")
        put(s, "leads", lead.tenant_id, lead.seller_id, dict(lead.data), now=clock())
    task = incoming(workspace)
    dispatch(workspace)
    assert task["lead_id"] is None and task["ambiguous"] is True
    assert not any(a["action"] == "transfer" for a in carrier.actions)


def test_failed_rep_leg_tries_teammate_then_message_only_after_confirmed_failure(workspace, carrier, clock):
    _, phone = previous_call(workspace)
    peer = teammate(workspace, clock)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    first = target_payload(carrier, "first-rep")
    webhook(workspace, "call.hangup", first, "failed-first")
    dispatch(workspace)
    assert saved(workspace, "followup_tasks", task["id"])["routed_rep_id"] == peer
    second = target_payload(carrier, "second-rep")
    webhook(workspace, "call.hangup", second, "failed-second")
    dispatch(workspace)
    assert carrier.actions[-1]["action"] == "speak"
    before = len(carrier.actions)
    webhook(workspace, "call.hangup", first, "late-duplicate-first")
    dispatch(workspace)
    assert len(carrier.actions) == before


def test_ended_caller_cancels_unsent_commands_and_late_answer_cannot_revive(workspace, carrier):
    task = incoming(workspace)
    webhook(workspace, "call.hangup", {"call_control_id": "inbound-1"})
    webhook(workspace, "call.answered", {"call_control_id": "inbound-1"}, "late-answer")
    dispatch(workspace)
    assert not carrier.actions
    assert saved(workspace, "followup_tasks", task["id"])["inbound_state"] == "ended"


def test_missing_ring_receipt_times_out_without_blind_second_transfer(workspace, carrier, clock):
    _, phone = previous_call(workspace)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    count = len([a for a in carrier.actions if a["action"] == "transfer"])
    clock.now += timedelta(minutes=2)
    dispatch(workspace)
    assert len([a for a in carrier.actions if a["action"] == "transfer"]) == count
    assert any(h["provider_id"] == "inbound-1" for h in carrier.hangups)
    assert saved(workspace, "followup_tasks", task["id"])["review_required"]


def test_private_metadata_cannot_forge_an_unrelated_transfer_leg(workspace, carrier):
    _, phone = previous_call(workspace)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    payload = target_payload(carrier)
    context = json.loads(base64.b64decode(payload["client_state"]))
    context["attempt"] = "unissued-attempt"
    payload["client_state"] = base64.b64encode(json.dumps(context).encode()).decode()
    result = webhook(workspace, "call.bridged", payload)
    assert result.get("unmatched")
    assert saved(workspace, "followup_tasks", task["id"])["status"] == "open"


def test_live_callback_cannot_be_reassigned_by_task_editor(workspace, clock):
    _, phone = previous_call(workspace)
    peer = teammate(workspace, clock)
    task = incoming(workspace, phone=phone)
    result = workspace["client"].patch(
        f"/v1/tasks/{task['id']}",
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": task["version"], "owner_id": peer},
    )
    assert result.status_code == 409
    service = workspace["client"].app.state.service
    with service.db.transaction(False) as s:
        assert (
            s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == task["id"])).rep_id
            == workspace["user_id"]
        )


def test_recheck_rep_pause_before_transfer_submission(workspace, carrier, clock):
    _, phone = previous_call(workspace)
    teammate(workspace, clock)
    task = incoming(workspace, phone=phone)
    workspace["client"].post("/v1/rep/presence", headers=workspace["headers"], json={"status": "paused"})
    dispatch(workspace)
    assert all("responsible@" not in a["payload"].get("to", "") for a in carrier.actions)
    assert saved(workspace, "followup_tasks", task["id"])["route"] == "teammate"


def test_answer_receipt_reconciles_uncertain_ack_before_routing(workspace, carrier, monkeypatch):
    action = carrier.action

    async def lost_answer(provider_id, kind, payload):
        await action(provider_id, kind, payload)
        if kind == "answer":
            raise TimeoutError("answer accepted, response lost")

    monkeypatch.setattr(carrier, "action", lost_answer)
    incoming(workspace)
    dispatch(workspace)
    assert not any(a["action"] == "speak" for a in carrier.actions)
    webhook(workspace, "call.answered", {"call_control_id": "inbound-1"})
    dispatch(workspace)
    assert len([a for a in carrier.actions if a["action"] == "speak"]) == 1


@pytest.mark.parametrize("context", [[], "not-an-object", 12])
def test_unmatched_client_state_is_not_a_processing_failure(workspace, context):
    encoded = base64.b64encode(json.dumps(context).encode()).decode()
    result = webhook(workspace, "call.bridged", {"call_control_id": "unknown", "client_state": encoded})
    assert result.get("unmatched")


def test_rep_can_end_the_current_inbound_call_and_both_known_legs(workspace, carrier):
    _, phone = previous_call(workspace)
    carrier.hangups.clear()
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    webhook(workspace, "call.bridged", target_payload(carrier))
    result = workspace["client"].post(f"/v1/callbacks/{task['id']}/hangup", headers=workspace["headers"])
    assert result.status_code == 200, result.text
    dispatch(workspace)
    assert {h["provider_id"] for h in carrier.hangups} == {"inbound-1", "destination-leg"}
    again = workspace["client"].post(f"/v1/callbacks/{task['id']}/hangup", headers=workspace["headers"])
    assert again.status_code == 200
    dispatch(workspace)
    assert len(carrier.hangups) == 2


def test_connected_callback_completes_only_the_related_unedited_return_task(workspace, carrier, clock):
    call, phone = previous_call(workspace)
    service = workspace["client"].app.state.service
    with service.db.transaction() as s:
        common = {
            "lead_id": call["lead_id"],
            "call_id": call["id"],
            "kind": "callback",
            "status": "open",
            "owner_id": workspace["user_id"],
            "action": "Return call",
            "due_at": clock().isoformat(),
        }
        promised = put(
            s, "followup_tasks", workspace["tenant_id"], workspace["seller_id"], common, now=clock()
        )
        edited = put(
            s,
            "followup_tasks",
            workspace["tenant_id"],
            workspace["seller_id"],
            {**common, "human_edited": True},
            now=clock(),
        )
        promised_id, edited_id = promised.id, edited.id
    incoming(workspace, phone=phone)
    dispatch(workspace)
    webhook(workspace, "call.bridged", target_payload(carrier))
    assert saved(workspace, "followup_tasks", promised_id)["status"] == "completed"
    assert saved(workspace, "followup_tasks", edited_id)["status"] == "open"


def test_ended_provider_callback_is_reconciled_after_restart_without_replaying_transfer(workspace, carrier):
    _, phone = previous_call(workspace)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    webhook(workspace, "call.initiated", target_payload(carrier))
    carrier.calls["inbound-1"] = {"call_control_id": "inbound-1", "is_alive": False}
    carrier.calls["destination-leg"] = {"call_control_id": "destination-leg", "is_alive": False}
    service = workspace["client"].app.state.service
    asyncio.run(service.recover(principal(workspace)))
    assert saved(workspace, "followup_tasks", task["id"])["inbound_state"] == "ended"
    assert len([a for a in carrier.actions if a["action"] == "transfer"]) == 1


def test_callback_budget_prevents_optional_transfer_and_speech_but_never_hangup(workspace, carrier):
    configure(workspace, destination="sip:reception@example.test")
    service = workspace["client"].app.state.service
    with service.db.transaction() as s:
        for budget in rows(s, "budgets"):
            update(budget, daily_limit_usd=0, monthly_limit_usd=0)
    task = incoming(workspace)
    dispatch(workspace)
    assert not any(a["action"] in {"speak", "transfer"} for a in carrier.actions)
    assert any(h["provider_id"] == "inbound-1" for h in carrier.hangups)
    assert saved(workspace, "followup_tasks", task["id"])["status"] == "open"


def test_callback_charges_remain_uncertain_until_matching_imported_carrier_receipt(workspace, carrier):
    configure(workspace, destination="sip:reception@example.test")
    task = incoming(workspace)
    dispatch(workspace)
    service = workspace["client"].app.state.service
    with service.db.transaction(False) as s:
        entries = [doc(r) for r in rows(s, "usage_ledger") if r.data.get("object_id") == task["id"]]
    assert len(entries) == 1
    hold = entries[0]
    assert hold["status"] == "uncertain" and hold["reserved_usd"] == 0.1
    payload = {
        "source_reference": "carrier-invoice-fixture",
        "rows": [
            {
                "callback_id": task["id"],
                "provider_record_id": "callback-transfer-charge",
                "kind": "callback_transfer",
                "request_key": hold["request_key"],
                "duration_seconds": 0,
                "cost_usd": 0.1,
            }
        ],
    }
    c, h = workspace["client"], workspace["headers"]
    result = c.post("/v1/admin/billing/reconcile", headers=h, json=payload)
    assert result.status_code == 200, result.text
    again = c.post("/v1/admin/billing/reconcile", headers=h, json=payload)
    assert again.json()["duplicates"] == 1
    with service.db.transaction(False) as s:
        entries = [doc(r) for r in rows(s, "usage_ledger") if r.data.get("object_id") == task["id"]]
    assert len(entries) == 1 and entries[0]["reserved_usd"] == 0
    assert entries[0]["cost_usd"] == 0.1 and entries[0]["billing_verified"]


def test_callback_rates_are_visible_without_a_paid_request(workspace, carrier):
    response = workspace["client"].get("/v1/admin/callback-routing", headers=workspace["headers"])
    assert response.json()["costs"]["transfer_per_invocation_usd"] == 0.1
    assert response.json()["costs"]["standard_tts_per_character_usd"] == 0.000009
    assert not carrier.actions


def test_control_retry_does_not_extend_original_inbound_deadline(workspace, carrier, clock, monkeypatch):
    configure(workspace, destination="sip:reception@example.test")
    original = carrier.action

    async def timeout(provider_id, action, payload):
        await original(provider_id, action, payload)
        if action == "transfer":
            raise TimeoutError("uncertain transfer")

    monkeypatch.setattr(carrier, "action", timeout)
    task = incoming(workspace)
    dispatch(workspace)
    deadline = saved(workspace, "followup_tasks", task["id"])["inbound_deadline"]
    clock.now += timedelta(seconds=10)
    service = workspace["client"].app.state.service
    asyncio.run(service.recover(principal(workspace)))
    dispatch(workspace)
    assert saved(workspace, "followup_tasks", task["id"])["inbound_deadline"] == deadline


@pytest.mark.parametrize("cost", ["NaN", "Infinity", True, "bad-number"])
def test_nonfinite_or_invalid_billing_never_corrupts_callback_budget(workspace, cost):
    task = incoming(workspace)
    response = workspace["client"].post(
        "/v1/admin/billing/reconcile",
        headers=workspace["headers"],
        json={
            "source_reference": "untrusted-import",
            "rows": [
                {
                    "callback_id": task["id"],
                    "provider_record_id": "bad-receipt",
                    "cost_usd": cost,
                    "duration_seconds": 0,
                }
            ],
        },
    )
    assert response.status_code == 422


def test_verified_retry_reuses_callback_liability_without_readmitting_spend(
    workspace, carrier, clock, monkeypatch
):
    configure(workspace, destination="sip:reception@example.test")
    original = carrier.action

    async def timeout(provider_id, action, payload):
        await original(provider_id, action, payload)
        if action == "transfer":
            raise TimeoutError("carrier accepted, response lost")

    async def alive(command_id, provider_id=None):
        return {"call_control_id": provider_id, "is_alive": True}

    monkeypatch.setattr(carrier, "action", timeout)
    task = incoming(workspace)
    dispatch(workspace)
    service, p = workspace["client"].app.state.service, principal(workspace)
    with service.db.transaction() as s:
        hold = next(r for r in service.scoped(s, "usage_ledger", p) if r.data.get("object_id") == task["id"])
        before = doc(hold)
        update(service.scoped(s, "budgets", p)[0], daily_limit_usd=0, monthly_limit_usd=0)
    clock.now += timedelta(seconds=10)
    monkeypatch.setattr(carrier, "action", original)
    monkeypatch.setattr(carrier, "reconcile", alive)
    asyncio.run(service.recover(p))
    actions = [a for a in carrier.actions if a["action"] == "transfer"]
    assert len(actions) == 2 and actions[0]["payload"]["command_id"] == actions[1]["payload"]["command_id"]
    with service.db.transaction(False) as s:
        holds = [
            doc(r) for r in service.scoped(s, "usage_ledger", p) if r.data.get("object_id") == task["id"]
        ]
    assert holds == [before]
    assert saved(workspace, "followup_tasks", task["id"])["inbound_state"] == "ringing"
