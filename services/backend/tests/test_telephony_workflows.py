from datetime import timedelta

from conftest import reserve, start


def event(w, call, name, payload=None):
    from uuid import uuid4

    return w["client"].post(
        "/v1/simulation/event",
        headers=w["headers"],
        json={"event_id": str(uuid4()), "event_type": name, "call_id": call["id"], "payload": payload or {}},
    )


def test_answer_enqueues_permissioned_stt_without_waiting_for_inference(workspace, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/admin/control/run", headers=h)
    assert any(
        a["action"] == "transcription_start" and a["payload"]["transcription_tracks"] == "both"
        for a in carrier.actions
    )


def test_agent_leg_is_server_created_and_live_ready_requires_verified_answer(workspace):
    c, h = workspace["client"], workspace["headers"]
    r = c.post("/v1/telephony/agent", headers=h)
    assert r.status_code == 200, r.text
    assert (
        c.get("/v1/telephony/agent/" + r.json()["agent_call_control_id"], headers=h).json()["state"]
        == "answered"
    )


def test_detached_voicemail_releases_rep_only_after_playback_and_retains_number_capacity(workspace, carrier):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/admin/policies/{workspace['policy_id']}/publish",
        headers=h,
        json={
            "approved": True,
            "permissions": ["live", "transcription", "prerecorded"],
            "voicemail_enabled": True,
            "voicemail_optout_supported": True,
            "approved_assets": ["approved-vm"],
            "detached_topology_verified": True,
            "topology_test_reference": "synthetic-fixture",
            "late_human_recovery_destination": "sip:recovery@sip.telnyx.com",
        },
    )
    for name in [
        "call.answered",
        "call.machine.detection.ended:machine",
        "call.machine.greeting.ended:beep_detected",
    ]:
        event(workspace, call, name)
    r = c.post(
        f"/v1/calls/{call['id']}/voicemail", headers=h, json={"asset_id": "approved-vm", "detach": True}
    )
    assert r.status_code == 200, r.text
    assert reserve(workspace).status_code == 409
    event(workspace, call, "call.playback.started")
    state = c.get(f"/v1/calls/{call['id']}", headers=h).json()
    assert state["detached"]
    next_packet = reserve(workspace)
    assert next_packet.status_code == 200, next_packet.text
    assert next_packet.json()["number"]["id"] != call["number_id"]
    event(workspace, call, "call.machine.detection.ended:human")
    c.post("/v1/admin/control/run", headers=h)
    assert any(a["action"] == "playback_stop" for a in carrier.actions)
    assert any(a["action"] == "transfer" for a in carrier.actions)


def test_voicemail_watchdog_and_optout_stop_old_leg(workspace, clock):
    call = start(workspace)
    service = workspace["client"].app.state.service
    with service.db.transaction() as s:
        from app.db import get, update

        update(
            get(s, "call_attempts", call["id"]),
            state="VM_PLAYING",
            detached=True,
            voicemail_deadline=(clock.now - timedelta(seconds=1)).isoformat(),
        )
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/admin/control/run", headers=h)
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["state"] == "ENDED"


def test_inbound_callback_resolves_seller_and_ambiguous_caller_is_queued(workspace):
    service = workspace["client"].app.state.service
    import asyncio

    result = asyncio.run(
        service.inbound_callback(
            workspace["tenant_id"],
            {"call_control_id": "incoming-test", "to": "+14155550100", "from": "+14155550101"},
            "inbound-event",
        )
    )
    assert result["seller_id"] == workspace["seller_id"]
    assert result["route"] in {"responsible_rep", "callback_queue"}
    assert result["lead_id"] is not None
    unknown = asyncio.run(
        service.inbound_callback(
            workspace["tenant_id"],
            {"call_control_id": "incoming-unknown", "to": "+14155550100", "from": "+14155550888"},
            "unknown-event",
        )
    )
    assert unknown["route"] == "callback_queue" and unknown["lead_id"] is None
