"""A10/A28-A30/A36-A39/A45-A48: complete local rep continuity, authored first."""

from datetime import timedelta

import pytest
from app.db import update
from conftest import start


def p(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def finish(w, call, outcome="callback_requested", **data):
    c, h = w["client"], w["headers"]
    path = "/v1/calls/" + call["id"]
    assert c.post(path + "/hangup", headers=h).status_code == 200
    result = c.post(path + "/disposition", headers=h, json={"outcome": outcome, **data})
    assert result.status_code == 200, result.text
    return result


def test_repeated_wrapup_updates_one_task_and_one_outcome(workspace, clock):
    call = start(workspace)
    due = (clock.now + timedelta(days=1)).isoformat()
    finish(workspace, call, callback_at=due)
    c, h = workspace["client"], workspace["headers"]
    result = c.post(
        "/v1/calls/" + call["id"] + "/disposition",
        headers=h,
        json={"outcome": "callback_requested", "callback_at": due},
    )
    assert result.status_code == 200
    tasks = [t for t in c.get("/v1/workspace", headers=h).json()["tasks"] if t["call_id"] == call["id"]]
    assert len(tasks) == 1


def test_reschedule_task_updates_lead_due_and_preserves_owner(workspace, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    c, h = workspace["client"], workspace["headers"]
    task = next(t for t in c.get("/v1/workspace", headers=h).json()["tasks"] if t["call_id"] == call["id"])
    due = (clock.now + timedelta(days=2)).isoformat()
    response = c.patch(
        "/v1/tasks/" + task["id"],
        headers=h,
        json={
            "status": "open",
            "due_at": due,
            "timezone": "America/Los_Angeles",
            "expected_version": task["version"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["due_at"] == due
    lead = next(x for x in c.get("/v1/workspace", headers=h).json()["leads"] if x["id"] == call["lead_id"])
    assert lead["callback_due_at"] == due
    assert response.json()["owner_id"] == workspace["user_id"]


def test_stale_task_edit_cannot_overwrite_newer_confirmation(workspace, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    c, h = workspace["client"], workspace["headers"]
    task = next(t for t in c.get("/v1/workspace", headers=h).json()["tasks"] if t["call_id"] == call["id"])
    assert (
        c.patch(
            "/v1/tasks/" + task["id"],
            headers=h,
            json={"status": "completed", "expected_version": task["version"]},
        ).status_code
        == 200
    )
    assert (
        c.patch(
            "/v1/tasks/" + task["id"], headers=h, json={"status": "open", "expected_version": task["version"]}
        ).status_code
        == 409
    )


def test_rep_booked_meeting_holds_cold_outreach_without_calendar_dependency(workspace, app):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    packet = c.get("/v1/leads/" + call["lead_id"] + "/packet", headers=h).json()
    assert packet["lead"]["outreach_hold"] == "meeting_booked"
    assert packet["prospect_brief"]["meeting"]["source"] == "rep_reported"
    assert packet["prospect_brief"]["meeting"]["invitation_sent"] is False
    assert packet["prospect_brief"]["meeting"]["held"] is not True


def test_packet_contains_sparse_statistical_context_and_retained_brief(workspace):
    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    result = c.get("/v1/leads/" + lead["id"] + "/packet", headers=h).json()
    assert result["prospect_brief"]["evidence_status"] == "sparse"
    prediction = result["statistical_guidance"]
    assert prediction["samples"] == 0
    assert 0 < prediction["human_probability"] < 1
    assert prediction["uncertainty"]
    assert prediction["why_now"]


def test_template_has_gatekeeper_and_iphone_screening_and_is_filled_ahead(workspace):
    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    response = c.put(
        "/v1/admin/operating-policy",
        headers=h,
        json={
            "script_template": {
                "opener": "Hello {name}, this is {seller}. {relevance}",
                "gatekeeper": "Could you point me to the person responsible for {topic}?",
                "screening_intro": "{seller}, calling about {topic}.",
                "discovery_questions": ["How are you handling {topic}?"],
            },
            "preparation": {
                "ahead_count": 10,
                "batch_size": 20,
                "off_hours_start": 18,
                "off_hours_end": 8,
                "timezone": "America/Los_Angeles",
            },
        },
    )
    assert response.status_code == 200, response.text
    c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead["id"]]})
    c.post("/v1/admin/jobs/run", headers=h)
    packet = c.get("/v1/leads/" + lead["id"] + "/packet", headers=h).json()
    assert packet["script"]["gatekeeper"]
    assert packet["script"]["screening_intro"]
    assert packet["script"]["template_version"]
    assert "{seller}" not in packet["script"]["opener"]
    assert packet["preparation"]["status"] in {"ready", "fallback"}


def test_wrapup_queues_packet_refresh_without_manual_admin_action(workspace, app):
    from app.db import Job
    from sqlalchemy import select

    call = start(workspace)
    finish(workspace, call, "right_party")
    with app.state.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.tenant_id == workspace["tenant_id"])))
        assert any(
            j.queue in {"research", "scripts", "prepare"}
            and call["lead_id"] in (j.payload.get("lead_ids") or [j.payload.get("lead_id")])
            for j in jobs
        )


@pytest.mark.parametrize("detach", [False, True])
def test_voicemail_on_early_attempt_is_blocked_even_with_all_other_permissions(workspace, app, detach):
    call = start(workspace)
    service = app.state.service
    with service.db.transaction() as s:
        update(
            service.required(s, "policy_versions", workspace["policy_id"], p(workspace)),
            permissions=["live", "prerecorded"],
            voicemail_enabled=True,
            voicemail_optout_supported=True,
            approved_assets=["vm"],
            detached_topology_verified=True,
            late_human_recovery_destination="sip:recovery@sip.telnyx.com",
            max_attempts=4,
        )
        update(service.required(s, "call_attempts", call["id"], p(workspace)), state="VOICEMAIL_READY")
    result = workspace["client"].post(
        "/v1/calls/" + call["id"] + "/voicemail",
        headers=workspace["headers"],
        json={"asset_id": "vm", "detach": detach},
    )
    assert result.status_code == 403, result.text
    assert "FINAL_ATTEMPT" in result.text


def test_final_attempt_voicemail_still_requires_real_mailbox(workspace, app):
    call = start(workspace)
    service = app.state.service
    with service.db.transaction() as s:
        update(service.required(s, "policy_versions", workspace["policy_id"], p(workspace)), max_attempts=1)
    result = workspace["client"].post(
        "/v1/calls/" + call["id"] + "/voicemail",
        headers=workspace["headers"],
        json={"asset_id": "vm", "detach": False},
    )
    assert result.status_code == 409 and "MAILBOX" in result.text


def test_metrics_measure_actual_work_time_not_attempt_elapsed(workspace, clock):
    from conftest import ready

    c, h = workspace["client"], workspace["headers"]
    ready(workspace)
    for _ in range(30):
        clock.now += timedelta(minutes=1)
        assert c.post("/v1/rep/heartbeat", headers=h, json={"activity": "ready"}).status_code == 200
    c.post(
        "/v1/rep/presence", headers=h, json={"status": "paused", "audio_ready": True, "telephony_ready": True}
    )
    metrics = c.get("/v1/metrics", headers=h).json()
    assert metrics["rep_work_seconds"] == 1800
    clock.now += timedelta(minutes=10)
    assert c.get("/v1/metrics", headers=h).json()["rep_work_seconds"] == 1800


def test_dashboard_health_distinguishes_unknown_from_bad(workspace):
    result = workspace["client"].get("/v1/metrics", headers=workspace["headers"]).json()
    assert result["health"]["connect_rate"]["status"] == "insufficient_data"
    assert result["health"]["connect_rate"]["samples"] == 0
    assert result["warnings"] == []


def test_exact_commitment_date_is_retained_and_vague_time_is_not_guessed():
    from app.grounding import accept_review
    from app.postcall.inference import analysis_review_request, validate_analysis
    from test_grounding_review import review_response

    segment = {
        "segment_id": "s",
        "side": "remote",
        "is_final": True,
        "text": "Call me on October 20, 2026 at 2 PM Pacific time.",
    }
    body = {
        "summary": "The prospect requested a callback.",
        "summary_evidence": ["s"],
        "problems": [],
        "objections": [],
        "unknowns": [],
        "score_items": [],
        "commitments": [
            {
                "owner": "rep",
                "action": "Call back",
                "due_at": "2026-10-20T21:00:00+00:00",
                "verbatim_time_expression": segment["text"],
                "evidence": ["s"],
                "needs_time_confirmation": False,
            }
        ],
    }
    request = analysis_review_request(body, [segment])
    result = validate_analysis(
        body, [segment], review=accept_review(request, review_response(request), "date-review")
    )
    assert result["commitments"][0]["due_at"] == "2026-10-20T21:00:00+00:00"
    assert result["commitments"][0]["needs_time_confirmation"] is False
    vague = {**segment, "text": "Maybe some afternoon next month."}
    body["commitments"][0].update(verbatim_time_expression=vague["text"], needs_time_confirmation=True)
    body["summary"] = "Follow-up timing remains tentative."
    request = analysis_review_request(body, [vague])
    result = validate_analysis(
        body, [vague], review=accept_review(request, review_response(request), "vague-date-review")
    )
    assert result["commitments"][0]["due_at"] is None


def test_duplicate_rubric_dimensions_are_rejected_instead_of_inflating_score():
    from app.postcall.inference import validate_analysis

    segment = {"segment_id": "s", "side": "rep", "is_final": True, "text": "Hello from Example Seller."}
    item = {
        "dimension": "identity_relevance",
        "score": 2,
        "status": "SCORED",
        "evidence": ["s"],
        "explanation": "Identity supplied",
    }
    with pytest.raises(ValueError):
        validate_analysis(
            {
                "summary": "The rep introduced the seller.",
                "summary_evidence": ["s"],
                "problems": [],
                "objections": [],
                "commitments": [],
                "unknowns": [],
                "score_items": [item, item],
            },
            [segment],
        )


def test_inbound_unavailable_rep_has_actual_fallback_action(workspace, app, carrier):
    import asyncio

    service = app.state.service
    service.vault.write(
        workspace["tenant_id"], {"inbound_fallback_destination": "sip:reception@example.test"}
    )
    result = asyncio.run(
        service.inbound_callback(
            workspace["tenant_id"],
            {"call_control_id": "incoming-fallback", "to": "+14155550100", "from": "+14155550101"},
            "fallback-event",
        )
    )
    asyncio.run(service.dispatch_controls(p(workspace)))
    assert result["route"] in {"business_fallback", "callback_queue"}
    assert any(
        a["action"] in {"transfer", "speak", "playback_start"} and a["provider_id"] == "incoming-fallback"
        for a in carrier.actions
    )
