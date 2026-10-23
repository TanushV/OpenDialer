"""Calling, tasks, holds, telephony, signed inbound return calls and stuck-call recovery over HTTP only."""

import base64
import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from journey import answered_by_human, call, event, get, hang_up, post, run_jobs, start, wrap_up

REP_PASSWORD = "rep-test-password-123"
VOICEMAIL = {
    "max_attempts": 1,
    "permissions": ["live", "transcription", "prerecorded"],
    "voicemail_enabled": True,
    "voicemail_optout_supported": True,
    "approved_assets": ["vm-1"],
    "detached_topology_verified": True,
    "late_human_recovery_destination": "sip:recovery@example.test",
}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def add_user(ws, email, sip=None):
    body = {"email": email, "name": "Rep", "role": "rep", "password": REP_PASSWORD, "sip_username": sip}
    made = post(ws, "/v1/admin/users", body)
    response = ws["client"].post("/v1/auth/session", json={"email": email, "password": REP_PASSWORD})
    assert response.status_code == 200, response.text
    return made["id"], bearer(response.json()["access_token"])


def ready_as(ws, headers=None, agent="agent-test-id"):
    body = {"status": "ready", "audio_ready": True, "telephony_ready": True, "agent_call_control_id": agent}
    return call(ws, "POST", "/v1/rep/presence", json=body, headers=headers or {})


def pause(ws, headers=None):
    return call(ws, "POST", "/v1/rep/presence", json={"status": "paused"}, headers=headers or {})


def next_lead(ws, headers=None, status=200):
    body = {"campaign_id": ws["campaign_id"]}
    return call(ws, "POST", "/v1/queue/next", json=body, status=status, headers=headers or {})


def start_as(ws, headers=None, key="call-key-001"):
    packet = next_lead(ws, headers)
    body = {"reservation_id": packet["reservation_id"], "idempotency_key": key}
    return call(ws, "POST", "/v1/calls/start", json=body, headers=headers or {})


def offer(ws, headers=None):
    """One queue round for this rep: ready, take the next lead, release it."""
    ready_as(ws, headers)
    lead = next_lead(ws, headers)["lead"]["id"]
    pause(ws, headers)
    return lead


def lead_row(ws, lead_id):
    return next(item for item in get(ws, "/v1/workspace/leads")["items"] if item["id"] == lead_id)


def lead_named(ws, name):
    return next(item for item in get(ws, "/v1/workspace/leads")["items"] if item["name"] == name)


def task_row(ws, task_id):
    return next(item for item in get(ws, "/v1/workspace/tasks")["items"] if item["id"] == task_id)


def inbound_task(ws, provider_id):
    return next(
        item for item in get(ws, "/v1/workspace/tasks")["items"] if item.get("provider_id") == provider_id
    )


def edit_task(ws, task, code=200, headers=None, **values):
    body = {"request_id": str(uuid4()), "expected_version": task["version"], **values}
    return call(ws, "PATCH", f"/v1/tasks/{task['id']}", json=body, status=code, headers=headers or {})


def recovery_command(ws, call_id):
    return next(item for item in get(ws, "/v1/admin/recovery")["commands"] if item["call_id"] == call_id)


def mailbox(ws, call_id):
    event(ws, call_id, "call.answered")
    event(ws, call_id, "call.machine.detection.ended:machine")
    event(ws, call_id, "call.machine.greeting.ended:beep_detected")
    return get(ws, f"/v1/calls/{call_id}")


def publish(ws, **policy):
    current = get(ws, "/v1/workspace")["policy"]
    return post(
        ws, f"/v1/admin/policies/{current['id']}/publish", {"expected_version": current["version"], **policy}
    )


def transfers(carrier):
    return [a["payload"]["to"] for a in carrier.actions if a["action"] == "transfer"]


def inbound(call_control_id, caller, to="+14155550100"):
    return {"call_control_id": call_control_id, "to": to, "from": caller, "direction": "incoming"}


class Telnyx:
    """Signed provider webhooks through the public route, verified and processed by the job lanes."""

    def __init__(self, ws, clock):
        self.ws, self.clock = ws, clock
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        call(ws, "PUT", "/v1/admin/settings", json={"telnyx_public_key": base64.b64encode(public).decode()})

    def deliver(self, event_type, payload, event_id=None):
        body = json.dumps(
            {"data": {"id": event_id or str(uuid4()), "event_type": event_type, "payload": payload}}
        ).encode()
        stamp = str(int(self.clock.now.timestamp()))
        signature = base64.b64encode(self.key.sign(stamp.encode() + b"|" + body)).decode()
        headers = {"telnyx-signature-ed25519": signature, "telnyx-timestamp": stamp}
        response = self.ws["client"].post(
            "/webhooks/telnyx", content=body, headers={**headers, "content-type": "application/json"}
        )
        assert response.status_code == 200, response.text
        return response.json()


def test_presence_heartbeats_idle_pause_and_stale_work_intervals(workspace, clock):
    ws = workspace
    next_lead(ws, status=409)
    ready_as(ws)
    call(ws, "POST", "/v1/rep/heartbeat", json={"activity": "ready"})
    open_rows = [i for i in get(ws, "/v1/rep/work")["intervals"] if i["ended_at"] is None]
    assert len(open_rows) == 1 and open_rows[0]["source"] == "declared_presence"
    clock.now += timedelta(minutes=5)
    call(ws, "POST", "/v1/rep/heartbeat", json={"activity": "ready"})
    stale = [i for i in get(ws, "/v1/rep/work")["intervals"] if i.get("close_reason") == "heartbeat_expired"]
    assert len(stale) == 1 and stale[0]["ended_at"]
    call(ws, "POST", "/v1/rep/heartbeat", json={"activity": "idle"})
    assert get(ws, "/v1/workspace")["user"]["presence"]["status"] == "paused"
    next_lead(ws, status=409)
    call(ws, "POST", "/v1/rep/heartbeat", json={"activity": "ready"}, status=409)


def test_an_unstarted_reservation_lapses_and_frees_the_rep_and_lead(workspace, clock):
    ws = workspace
    ready_as(ws)
    first = next_lead(ws)
    assert "RESERVATION_EXISTS" in str(next_lead(ws, status=409))
    clock.now += timedelta(minutes=3)
    ready_as(ws)
    again = next_lead(ws)
    assert again["reservation_id"] != first["reservation_id"]
    body = {"reservation_id": first["reservation_id"], "idempotency_key": "lapsed-key"}
    call(ws, "POST", "/v1/calls/start", json=body, status=409)
    body = {"reservation_id": again["reservation_id"], "idempotency_key": "fresh-key"}
    assert call(ws, "POST", "/v1/calls/start", json=body)["lead_id"] == again["lead"]["id"]


def test_due_callback_is_offered_first_and_opt_out_removes_a_lead(workspace, clock):
    ws = workspace
    started = start(ws)
    lead_a = started["lead_id"]
    answered_by_human(ws, started["id"])
    hang_up(ws, started["id"])
    due = (clock.now + timedelta(minutes=61)).isoformat()
    saved = wrap_up(
        ws, started["id"], outcome="callback_requested", callback_at=due, callback_note="Call Thursday"
    )
    assert saved["canonical_task_id"] and saved["callback_review"] is None
    assert lead_a not in [offer(ws) for _ in range(2)]
    clock.now += timedelta(minutes=62)
    ready_as(ws)
    assert next_lead(ws)["lead"]["id"] == lead_a
    pause(ws)
    post(ws, "/v1/suppressions", {"phone": lead_row(ws, lead_a)["phone"], "reason": "opt_out"})
    assert lead_row(ws, lead_a)["suppressed"] is True
    assert lead_a not in [offer(ws) for _ in range(3)]
    for lead in get(ws, "/v1/workspace/leads")["items"]:
        if lead["id"] != lead_a:
            post(ws, "/v1/suppressions", {"phone": lead["phone"], "reason": "opt_out"})
    ready_as(ws)
    assert "NO_ELIGIBLE_LEADS" in str(next_lead(ws, status=409))


def test_call_start_is_idempotent_bridges_the_agent_leg_and_hangs_up_once(workspace, carrier):
    ws = workspace
    started = start(ws, "start-key-1")
    dial = carrier.dials[0]
    assert dial["to"] == lead_row(ws, started["lead_id"])["phone"]
    assert dial["link_to"] == "agent-test-id" and dial["bridge_on_answer"] is True
    provider = carrier.calls[started["id"]]["call_control_id"]
    again = post(
        ws, "/v1/calls/start", {"reservation_id": started["reservation_id"], "idempotency_key": "start-key-1"}
    )
    assert again["id"] == started["id"] and len(carrier.dials) == 1
    post(
        ws,
        "/v1/calls/start",
        {"reservation_id": "someone-else", "idempotency_key": "start-key-1"},
        status=409,
    )
    assert event(ws, started["id"], "call.answered", event_id="evt-answer-1")["duplicate"] is False
    assert event(ws, started["id"], "call.answered", event_id="evt-answer-1")["duplicate"] is True
    human = answered_by_human(ws, started["id"])
    assert (human["state"], human["provider_id"]) == ("HUMAN", provider)
    assert post(ws, f"/v1/calls/{started['id']}/hangup")["state"] == "ENDED"
    assert post(ws, f"/v1/calls/{started['id']}/hangup")["state"] == "ENDED"
    assert [h["provider_id"] for h in carrier.hangups] == [provider]


def test_keypad_digits_reach_the_carrier_once_per_request(workspace, carrier):
    ws = workspace
    started = start(ws)
    body = {"digits": "1#", "idempotency_key": "keypad-1"}
    sent = post(ws, f"/v1/calls/{started['id']}/dtmf", body)
    assert (sent["sent"], sent["status"]) == (True, "completed")
    assert post(ws, f"/v1/calls/{started['id']}/dtmf", body)["command_id"] == sent["command_id"]
    assert [a["payload"]["digits"] for a in carrier.actions if a["action"] == "send_dtmf"] == ["1#"]
    assert get(ws, f"/v1/calls/{started['id']}/dtmf/keypad-1")["sent"] is True
    get(ws, f"/v1/calls/{started['id']}/dtmf/unknown-key", 404)
    post(ws, f"/v1/calls/{started['id']}/dtmf", {"digits": "2", "idempotency_key": "keypad-1"}, status=409)
    post(ws, f"/v1/calls/{started['id']}/dtmf", {"digits": "abc", "idempotency_key": "keypad-2"}, status=422)


def test_final_attempt_voicemail_is_monitored_detached_and_opt_out_suppresses(workspace, carrier, clock):
    ws = workspace
    publish(ws, **VOICEMAIL)
    started = start(ws)
    assert mailbox(ws, started["id"])["state"] == "VOICEMAIL_READY"
    queued = post(ws, f"/v1/calls/{started['id']}/voicemail", {"asset_id": "vm-1", "detach": True})
    assert queued["state"] == "VM_PENDING"
    assert carrier.playbacks[-1]["media_name"] == "vm-1"
    assert any(a["action"] == "gather" for a in carrier.actions)
    event(ws, started["id"], "call.playback.started")
    call(ws, "POST", "/v1/admin/control/run")
    assert get(ws, f"/v1/calls/{started['id']}")["detached"] is True
    assert any(h["provider_id"] == "agent-test-id" for h in carrier.hangups)
    ready_as(ws)
    assert next_lead(ws)["reservation_id"]
    pause(ws)
    event(ws, started["id"], "call.dtmf.received", digit="9")
    ended = get(ws, f"/v1/calls/{started['id']}")
    assert (ended["state"], ended["end_reason"]) == ("ENDING", "voicemail_optout")
    assert lead_row(ws, started["lead_id"])["suppressed"] is True
    clock.now += timedelta(hours=2)
    assert started["lead_id"] not in [offer(ws) for _ in range(3)]


def test_voicemail_before_the_final_attempt_is_refused(workspace):
    ws = workspace
    publish(ws, **{**VOICEMAIL, "max_attempts": 4})
    started = start(ws)
    post(ws, f"/v1/calls/{started['id']}/voicemail", {"asset_id": "vm-1"}, status=409)
    mailbox(ws, started["id"])
    refused = post(ws, f"/v1/calls/{started['id']}/voicemail", {"asset_id": "vm-1"}, status=403)
    assert "FINAL_ATTEMPT_REQUIRED" in str(refused)


def test_wrapup_callback_task_is_rescheduled_snoozed_reassigned_and_completed(workspace, clock):
    ws = workspace
    rep_id, _ = add_user(ws, "rep@example.test")
    started = start(ws)
    hang_up(ws, started["id"])
    due = clock.now + timedelta(hours=1)
    saved = wrap_up(
        ws, started["id"], outcome="callback_requested", callback_at=due.isoformat(), callback_note="Pricing"
    )
    before = task_row(ws, saved["canonical_task_id"])
    assert (before["status"], before["kind"], before["call_id"]) == ("open", "callback", started["id"])
    assert datetime.fromisoformat(before["due_at"]) == due
    task = edit_task(ws, before, due_at=(due + timedelta(hours=1)).isoformat(), timezone="America/Chicago")
    assert lead_row(ws, started["lead_id"])["callback_due_at"] == task["due_at"]
    edit_task(ws, before, code=409, due_at=(due + timedelta(hours=2)).isoformat())
    task = edit_task(ws, task, status="snoozed", snoozed_until=(clock.now + timedelta(hours=4)).isoformat())
    assert task_row(ws, task["id"])["status"] == "snoozed"
    task = edit_task(ws, task, owner_id=rep_id)
    assert task_row(ws, task["id"])["owner_id"] == rep_id
    task = edit_task(ws, task, status="completed")
    assert task_row(ws, task["id"])["status"] == "completed"


def test_undated_callback_holds_the_lead_until_confirmed_then_can_be_cancelled(workspace, clock):
    ws = workspace
    started = start(ws)
    hang_up(ws, started["id"])
    saved = wrap_up(ws, started["id"], outcome="callback_requested", callback_note="Will confirm a time")
    task = task_row(ws, saved["canonical_task_id"])
    assert task["status"] == "needs_confirmation"
    lead_id = started["lead_id"]
    assert lead_row(ws, lead_id)["outreach_hold"] == "callback_time_confirmation"
    assert lead_id not in [offer(ws) for _ in range(3)]
    when = (clock.now + timedelta(days=1)).isoformat()
    task = edit_task(ws, task, due_at=when, timezone="America/Los_Angeles", confirmation_state="confirmed")
    assert task["status"] == "open" and lead_row(ws, lead_id)["outreach_hold"] is None
    edit_task(ws, task, status="cancelled")
    assert task_row(ws, task["id"])["status"] == "cancelled"


def test_outreach_holds_keep_a_lead_out_of_the_queue_until_released(workspace, clock):
    ws = workspace
    jordan = lead_named(ws, "Jordan Ellis")
    meeting = {"expected_version": jordan["version"], "request_id": str(uuid4())}
    booked = post(
        ws,
        f"/v1/leads/{jordan['id']}/meeting",
        {
            **meeting,
            "status": "booked",
            "due_at": (clock.now + timedelta(days=2)).isoformat(),
            "timezone": "America/Los_Angeles",
            "reference": "cal-42",
        },
    )
    assert booked["outreach_hold"] == "meeting_booked"
    assert jordan["id"] not in [offer(ws) for _ in range(2)]
    jordan = lead_named(ws, "Jordan Ellis")
    released = post(
        ws,
        f"/v1/leads/{jordan['id']}/meeting",
        {
            "expected_version": jordan["version"],
            "request_id": str(uuid4()),
            "status": "cancelled",
            "next_action": "resume",
        },
    )
    assert released["outreach_hold"] is None
    jordan = lead_named(ws, "Jordan Ellis")
    post(
        ws,
        f"/v1/leads/{jordan['id']}/conversions",
        {
            "event_id": "conv-jordan-1",
            "kind": "meeting_held",
            "occurred_at": (clock.now - timedelta(minutes=10)).isoformat(),
            "source_reference": "CRM 77",
        },
    )
    assert lead_named(ws, "Jordan Ellis")["status"] == "converted"
    call(ws, "PUT", "/v1/admin/optimization", json={"conversion_goal": "sale_won"})
    jordan = lead_named(ws, "Jordan Ellis")
    assert (jordan["status"], jordan["outreach_hold"]) == ("eligible", "goal_changed")
    assert jordan["id"] not in [offer(ws) for _ in range(2)]
    review = {"expected_version": jordan["version"], "request_id": str(uuid4())}
    call(
        ws,
        "PATCH",
        f"/v1/leads/{jordan['id']}/outreach",
        json={**review, "next_action": "resume", "reason": "Goal reviewed with the seller"},
    )
    assert lead_named(ws, "Jordan Ellis")["outreach_hold"] is None
    for name in ("Morgan Reed", "Casey Nguyen"):
        post(ws, "/v1/suppressions", {"phone": lead_named(ws, name)["phone"], "reason": "opt_out"})
    ready_as(ws)
    assert next_lead(ws)["lead"]["id"] == jordan["id"]


def test_telephony_token_and_agent_leg_are_reported_by_the_authority(workspace):
    ws = workspace
    token = post(ws, "/v1/telephony/token")
    assert token["simulation"] is True and token["token"]
    agent = post(ws, "/v1/telephony/agent")["agent_call_control_id"]
    assert get(ws, f"/v1/telephony/agent/{agent}")["state"] == "answered"
    get(ws, "/v1/telephony/agent/agent-unknown", 404)
    assert ready_as(ws, agent=agent)["agent_call_control_id"] == agent


def test_return_call_routes_to_the_rep_who_called_and_completes_the_callback(workspace, clock, carrier):
    ws = workspace
    rep_id, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    started = start_as(ws, rep, "rep-call-1")
    answered_by_human(ws, started["id"])
    hang_up(ws, started["id"])
    wrap_up(ws, started["id"], outcome="not_interested")
    telnyx = Telnyx(ws, clock)
    ready_as(ws, rep)
    telnyx.deliver("call.initiated", inbound("in-1", lead_row(ws, started["lead_id"])["phone"]), "evt-in-1")
    run_jobs(ws)
    task = inbound_task(ws, "in-1")
    assert (task["route"], task["routed_rep_id"]) == ("responsible_rep", rep_id)
    call(ws, "POST", "/v1/admin/control/run")
    assert transfers(carrier) == ["sip:rep-line-1@sip.telnyx.com"]
    attempt = task["routing_attempts"][0]
    leg = {"inbound_task": task["id"], "attempt": attempt["id"]}
    state = base64.b64encode(json.dumps(leg).encode()).decode()
    telnyx.deliver("call.bridged", {"call_control_id": "leg-1", "client_state": state}, "evt-leg-1")
    run_jobs(ws)
    done = task_row(ws, task["id"])
    assert (done["status"], done["completion_source"], done["owner_id"]) == (
        "completed",
        "inbound_rep_connected",
        rep_id,
    )
    ending = post(ws, f"/v1/callbacks/{task['id']}/hangup", headers=rep)
    assert ending["inbound_state"] == "ending"
    call(ws, "POST", "/v1/admin/control/run")
    assert "in-1" in [h["provider_id"] for h in carrier.hangups]


def test_return_calls_reach_a_teammate_then_the_fallback_line_then_a_spoken_message(
    workspace, clock, carrier
):
    ws = workspace
    rep_id, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    telnyx = Telnyx(ws, clock)
    assert get(ws, "/v1/admin/callback-routing")["version"] == 0
    routing = "/v1/admin/callback-routing"
    saved = call(
        ws,
        "PUT",
        routing,
        json={"expected_version": 0, "destination": "sip:front-desk@pbx.example.test", "ring_seconds": 15},
    )
    assert saved["version"] == 1 and saved["ring_seconds"] == 15
    call(
        ws,
        "PUT",
        routing,
        json={"expected_version": 0, "destination": "sip:other@pbx.example.test"},
        status=409,
    )
    call(ws, "PUT", routing, json={"expected_version": 1, "destination": "+14155550100"}, status=422)
    call(ws, "PUT", routing, json={"expected_version": 1, "destination": "not a destination"}, status=422)
    telnyx.deliver("call.initiated", inbound("in-1", "+12025550177"), "evt-in-1")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    task = inbound_task(ws, "in-1")
    assert (task["route"], task["routed_rep_id"], task["status"]) == ("teammate", rep_id, "open")
    assert transfers(carrier) == ["sip:rep-line-1@sip.telnyx.com"]
    assert [a["payload"]["timeout_secs"] for a in carrier.actions if a["action"] == "transfer"] == [15]
    pause(ws, rep)
    telnyx.deliver("call.initiated", inbound("in-2", "+12025550188"), "evt-in-2")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    assert inbound_task(ws, "in-2")["route"] == "business_fallback"
    assert transfers(carrier)[-1] == "sip:front-desk@pbx.example.test"
    call(ws, "PUT", routing, json={"expected_version": 1, "destination": ""})
    telnyx.deliver("call.initiated", inbound("in-3", "+12025550199"), "evt-in-3")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    assert inbound_task(ws, "in-3")["route"] == "callback_queue"
    speech = [a["payload"]["payload"] for a in carrier.actions if a["action"] == "speak"]
    assert "Goodbye" in speech[-1]
    assert get(ws, routing)["version"] == 2


def test_unsent_return_transfer_is_cancelled_and_a_stop_reaches_the_carrier(workspace, clock, carrier):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    Telnyx(ws, clock).deliver("call.initiated", inbound("in-9", "+12025550166"), "evt-in-9")
    run_jobs(ws)
    items = [i for i in get(ws, "/v1/admin/recovery")["commands"] if i["provider_id"] == "in-9"]
    transfer = next(i for i in items if i["action"] == "transfer")
    answer = next(i for i in items if i["action"] == "answer")
    assert transfer["can_cancel"] is True
    call(
        ws,
        "POST",
        f"/v1/admin/recovery/commands/{transfer['id']}",
        json={"expected_version": transfer["version"], "action": "cancel_unsent"},
    )
    assert transfers(carrier) == []
    call(
        ws,
        "POST",
        f"/v1/admin/recovery/commands/{answer['id']}",
        json={"expected_version": answer["version"], "action": "end_call"},
    )
    assert "in-9" in [h["provider_id"] for h in carrier.hangups]


def test_uncertain_dial_is_reconciled_by_its_carrier_reference_without_a_redial(workspace, carrier):
    ws = workspace
    carrier.timeout_after_accept = True
    started = start(ws, "uncertain-key")
    carrier.timeout_after_accept = False
    assert started["state"] == "RECONCILING"
    call(ws, "POST", "/v1/admin/recovery")
    item = recovery_command(ws, started["id"])
    assert item["status"] == "review_required" and item["can_end"] is False
    reference = carrier.calls[started["id"]]["call_control_id"]
    body = {"expected_version": item["version"], "action": "reconcile", "provider_id": reference}
    call(ws, "POST", f"/v1/admin/recovery/commands/{item['id']}", json=body)
    assert len(carrier.dials) == 1
    assert get(ws, f"/v1/calls/{started['id']}")["provider_id"] == reference


def test_dial_without_carrier_evidence_is_confirmed_not_placed_by_the_operator(workspace, carrier, clock):
    ws = workspace
    carrier.timeout_after_accept = True
    started = start(ws, "silent-key")
    carrier.timeout_after_accept = False
    clock.now += timedelta(seconds=150)
    call(ws, "POST", "/v1/admin/recovery")
    item = recovery_command(ws, started["id"])
    assert (item["status"], item["can_confirm_not_placed"]) == ("unconfirmed", True)
    assert get(ws, f"/v1/calls/{started['id']}")["state"] == "DIAL_UNCONFIRMED"
    body = {"expected_version": item["version"], "action": "confirm_not_placed"}
    call(ws, "POST", f"/v1/admin/recovery/commands/{item['id']}", json=body, status=422)
    body["reason"] = "Carrier account shows no leg"
    call(ws, "POST", f"/v1/admin/recovery/commands/{item['id']}", json=body)
    final = get(ws, f"/v1/calls/{started['id']}")
    assert final["state"] == "ENDED" and final["not_placed_reason"] == "Carrier account shows no leg"


def test_dead_webhook_job_is_listed_and_retried(workspace, clock):
    ws = workspace
    Telnyx(ws, clock).deliver("call.hangup", {"call_control_id": "gone-call"}, "evt-gone")
    for _ in range(4):
        run_jobs(ws)
        clock.now += timedelta(seconds=31)
    job = next(j for j in get(ws, "/v1/admin/recovery")["jobs"] if j["queue"] == "webhook")
    assert (job["status"], job["can_retry"]) == ("dead", True)
    retried = post(ws, f"/v1/admin/recovery/jobs/{job['id']}/retry", {"expected_attempts": job["attempts"]})
    assert retried["status"] == "queued"
    post(ws, f"/v1/admin/recovery/jobs/{job['id']}/retry", {"expected_attempts": job["attempts"]}, status=409)


def test_recovery_routes_refuse_reps_stale_items_and_unknown_identities(workspace):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test")
    for method, path in [
        ("GET", "/v1/admin/recovery"),
        ("POST", "/v1/admin/recovery"),
        ("POST", "/v1/admin/jobs/run"),
        ("POST", "/v1/admin/control/run"),
    ]:
        call(ws, method, path, headers=rep, status=403)
    body = {"expected_version": 1, "action": "cancel_unsent"}
    call(ws, "POST", "/v1/admin/recovery/agents/unknown", json=body, status=422)
    call(ws, "POST", "/v1/admin/recovery/commands/unknown", json=body, status=404)
    call(ws, "POST", "/v1/admin/recovery/jobs/unknown/retry", json={"expected_attempts": 0}, status=404)


@pytest.mark.parametrize("lost", [False, True])
def test_uncertain_outbound_hangup_is_listed_then_stopped_or_reconciled_without_a_redial(
    workspace, carrier, lost
):
    ws = workspace
    started = start(ws)
    answered_by_human(ws, started["id"])
    provider = carrier.calls[started["id"]]["call_control_id"]
    real_hangup = carrier.hangup

    async def uncertain(provider_id, command_id):
        if lost:
            await real_hangup(provider_id, command_id)
            raise TimeoutError("carrier response lost")
        raise ConnectionError("carrier unreachable")

    carrier.hangup = uncertain
    assert post(ws, f"/v1/calls/{started['id']}/hangup")["state"] == "ENDING"
    item = next(
        i
        for i in get(ws, "/v1/admin/recovery")["commands"]
        if i["call_id"] == started["id"] and i["kind"] == "hangup"
    )
    assert (item["status"], item["provider_id"], item["can_end"]) == ("uncertain", provider, True)
    carrier.hangup = real_hangup
    body = {"expected_version": item["version"], "action": "end_call"}
    if lost:
        body = {"expected_version": item["version"], "action": "reconcile", "provider_id": provider}
    call(ws, "POST", f"/v1/admin/recovery/commands/{item['id']}", json=body)
    assert get(ws, f"/v1/calls/{started['id']}")["state"] == "ENDED"
    assert len(carrier.dials) == 1
    assert [h["provider_id"] for h in carrier.hangups] == [provider]


def test_agent_recovery_has_nothing_to_reconcile_in_simulation(workspace):
    ws = workspace
    post(ws, "/v1/telephony/agent")
    assert get(ws, "/v1/admin/recovery")["agents"] == []
    call(
        ws,
        "POST",
        "/v1/admin/recovery/agents/unknown-leg",
        json={"expected_version": 1, "action": "reconcile"},
        status=404,
    )


def test_caller_hanging_up_before_a_rep_answers_ends_the_return_call_and_frees_the_rep(
    workspace, clock, carrier
):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    telnyx = Telnyx(ws, clock)
    telnyx.deliver("call.initiated", inbound("in-1", "+12025550177"), "evt-in-1")
    run_jobs(ws)
    assert inbound_task(ws, "in-1")["route"] == "teammate"
    telnyx.deliver("call.hangup", {"call_control_id": "in-1"}, "evt-in-1-end")
    run_jobs(ws)
    task = inbound_task(ws, "in-1")
    assert (task["inbound_state"], task["caller_ended"]) == ("ended", True)
    assert {a["state"] for a in task["routing_attempts"]} == {"cancelled"}
    call(ws, "POST", "/v1/admin/control/run")
    assert transfers(carrier) == [] and not any(a["action"] == "answer" for a in carrier.actions)
    assert not [i for i in get(ws, "/v1/admin/recovery")["commands"] if i["provider_id"] == "in-1"]
    telnyx.deliver("call.initiated", inbound("in-2", "+12025550188"), "evt-in-2")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    assert inbound_task(ws, "in-2")["route"] == "teammate"
    assert transfers(carrier) == ["sip:rep-line-1@sip.telnyx.com"]


def test_unanswered_rep_leg_falls_through_to_the_business_line_then_a_spoken_message(
    workspace, clock, carrier
):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    telnyx = Telnyx(ws, clock)
    call(
        ws,
        "PUT",
        "/v1/admin/callback-routing",
        json={"expected_version": 0, "destination": "sip:front-desk@pbx.example.test"},
    )
    telnyx.deliver("call.initiated", inbound("in-1", "+12025550177"), "evt-in-1")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    for leg, route in [("leg-1", "business_fallback"), ("leg-2", "callback_queue")]:
        task = inbound_task(ws, "in-1")
        context = {"inbound_task": task["id"], "attempt": task["routing_attempts"][-1]["id"]}
        state = base64.b64encode(json.dumps(context).encode()).decode()
        telnyx.deliver("call.initiated", {"call_control_id": leg, "client_state": state}, f"evt-{leg}")
        telnyx.deliver("call.hangup", {"call_control_id": leg, "client_state": state}, f"evt-{leg}-end")
        run_jobs(ws)
        call(ws, "POST", "/v1/admin/control/run")
        assert inbound_task(ws, "in-1")["route"] == route
    assert transfers(carrier) == ["sip:rep-line-1@sip.telnyx.com", "sip:front-desk@pbx.example.test"]
    assert "Goodbye" in [a["payload"]["payload"] for a in carrier.actions if a["action"] == "speak"][-1]


def test_return_call_nobody_answers_is_ended_by_the_watchdog_at_its_deadline(workspace, clock, carrier):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    Telnyx(ws, clock).deliver("call.initiated", inbound("in-1", "+12025550177"), "evt-in-1")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    assert transfers(carrier) == ["sip:rep-line-1@sip.telnyx.com"]
    clock.now += timedelta(seconds=41)
    call(ws, "POST", "/v1/admin/control/run")
    task = inbound_task(ws, "in-1")
    assert (task["inbound_state"], task["review_required"]) == ("ending", True)
    assert task["recovery_reason"] == "No terminal routing receipt before deadline"
    assert "in-1" in [h["provider_id"] for h in carrier.hangups]
    assert transfers(carrier) == ["sip:rep-line-1@sip.telnyx.com"]


def test_gather_opt_out_during_voicemail_suppresses_and_other_digits_rearm_the_gather(
    workspace, carrier, clock
):
    ws = workspace
    publish(ws, **VOICEMAIL)
    started = start(ws)
    mailbox(ws, started["id"])
    post(ws, f"/v1/calls/{started['id']}/voicemail", {"asset_id": "vm-1", "detach": True})
    event(ws, started["id"], "call.playback.started")
    event(ws, started["id"], "call.gather.ended", digits="1")
    call(ws, "POST", "/v1/admin/control/run")
    assert get(ws, f"/v1/calls/{started['id']}")["state"] == "VM_PLAYING"
    assert lead_row(ws, started["lead_id"])["suppressed"] is False
    assert sum(a["action"] == "gather" for a in carrier.actions) == 2
    event(ws, started["id"], "call.gather.ended", digits="9")
    ended = get(ws, f"/v1/calls/{started['id']}")
    assert (ended["state"], ended["end_reason"]) == ("ENDING", "voicemail_optout")
    assert lead_row(ws, started["lead_id"])["suppressed"] is True
    clock.now += timedelta(hours=2)
    assert started["lead_id"] not in [offer(ws) for _ in range(3)]
