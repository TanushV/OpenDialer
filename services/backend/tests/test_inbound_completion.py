"""Real inbound handling, not a task mislabelled as a live queue (A11)."""

import asyncio
import base64
import json
from datetime import timedelta

import pytest
from app.db import ActiveClaim, doc, get, put, rows, update
from conftest import ready, reserve, start
from sqlalchemy import select


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def incoming(w, *, provider_id="inbound-1", event_id="incoming-1", phone="+14155550101"):
    return asyncio.run(
        w["client"].app.state.service.inbound_callback(
            w["tenant_id"],
            {"call_control_id": provider_id, "to": "+14155550100", "from": phone},
            event_id,
        )
    )


def dispatch(w):
    service = w["client"].app.state.service
    asyncio.run(service.dispatch_controls(principal(w)))
    asyncio.run(service.dispatch_controls(principal(w)))


def webhook(w, event_type, payload, event_id="receipt-1"):
    service = w["client"].app.state.service
    return asyncio.run(
        service._apply_webhook(
            w["tenant_id"],
            {"event_type": event_type, "event_id": event_id, "received_at": service.clock().isoformat()},
            payload,
        )
    )


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
        json={"expected_version": task["version"], "owner_id": peer},
    )
    assert result.status_code == 409
    service = workspace["client"].app.state.service
    with service.db.transaction(False) as s:
        assert (
            s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == task["id"])).rep_id
            == workspace["user_id"]
        )
