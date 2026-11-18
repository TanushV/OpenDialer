"""November 9: Test call is one direct action that always reaches the rep's own phone, and an empty queue says why.

The owner pressed Test call with +12038211101 saved and got "No one left to call": that number already existed as an
archived lead, so it never joined the Test line list, and at 3 AM Eastern the calling window refused it anyway.
"""

from datetime import UTC, datetime, timedelta

import httpx
from app import ops
from app.administration import no_lead_detail
from app.calling import dial
from conftest import ready
from helpers import finish

PHONE = "+12038211101"


def start(workspace):
    client, headers = workspace["client"], workspace["headers"]
    started = client.post("/v1/settings/test-call", headers=headers)
    assert started.status_code == 200, started.text
    ready(workspace)
    packet = client.post("/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]})
    return started.json(), packet


def save_phone(workspace, phone=PHONE):
    r = workspace["client"].put(
        "/v1/admin/settings", headers=workspace["headers"], json={"test_phone": phone}
    )
    assert r.status_code == 200, r.text


def test_a_test_call_reaches_the_rep_outside_calling_hours(workspace, clock):
    clock.now = datetime(
        2026, 10, 15, 1, 0, tzinfo=UTC
    )  # 9 PM Eastern, 6 PM Pacific: outside every calling window
    save_phone(workspace)
    started, packet = start(workspace)
    assert packet.status_code == 200, packet.text
    assert packet.json()["lead"]["id"] == started["lead_id"]
    assert packet.json()["lead"]["phone"] == PHONE


def test_a_carrier_rejection_finishes_the_test_and_exposes_the_error(workspace, carrier, monkeypatch):
    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace)
    _, packet = start(workspace)
    assert packet.status_code == 200, packet.text

    async def refuse(payload):
        request = httpx.Request("POST", "https://api.telnyx.com/v2/calls")
        response = httpx.Response(
            403,
            json={"errors": [{"code": "40001", "detail": "Private carrier detail"}]},
            request=request,
        )
        response.raise_for_status()

    with monkeypatch.context() as patch:
        patch.setattr(carrier, "dial", refuse)
        result = client.post(
            "/v1/calls/start",
            headers=headers,
            json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": "rejected-test"},
        )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "FINALIZED"
    assert result.json()["dial_error"] == {
        "message": "Telnyx rejected the dial (HTTP 403, code 40001).",
        "fix": "settings",
    }
    assert "Private carrier detail" not in result.text
    snapshot = client.get("/v1/workspace", headers=headers).json()
    assert not snapshot["test_phone"]
    assert snapshot["user"]["presence"]["status"] == "paused"
    assert not carrier.dials
    _, retry = start(workspace)
    assert retry.status_code == 200, retry.text


def test_a_blocker_between_reserving_and_dispatch_is_final_not_a_delayed_dial(
    workspace, app, carrier, monkeypatch
):
    from app.platform.db import workspace as scope
    from helpers import principal

    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace)
    _, packet = start(workspace)
    original = dial._dispatch_call

    async def block_then_dispatch(service, p, call_id):
        service.configs.write(scope(p), {"channel_limited_until": "2099-01-01T00:00:00+00:00"})
        return await original(service, p, call_id)

    with monkeypatch.context() as patch:
        patch.setattr(dial, "_dispatch_call", block_then_dispatch)
        stopped = client.post(
            "/v1/calls/start",
            headers=headers,
            json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": "cap-race"},
        )
    assert stopped.status_code == 200, stopped.text
    result = stopped.json()
    assert result["state"] == "FINALIZED"
    assert "account call limit reached" in result["dial_error"]["message"]
    service = app.state.service
    p = principal(workspace)
    service.configs.write(scope(p), {"channel_limited_until": None})
    with service.db.transaction(False) as session:
        intent = ops._by_key(service, session, p, "command_outbox", result["id"])
        assert intent.data["status"] == "cancelled"
    assert not carrier.dials


def test_the_test_phone_can_be_called_again_even_if_its_crm_record_is_stale(workspace, app, carrier, clock):
    from app.platform.db import update
    from helpers import principal

    service = app.state.service
    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace)
    _, packet = start(workspace)
    assert packet.status_code == 200, packet.text
    with service.db.transaction() as s:
        lead = ops.required(service, s, "leads", packet.json()["lead"]["id"], principal(workspace))
        update(
            lead,
            source="crm",
            crm_synced_at=(clock.now - timedelta(days=2)).isoformat(),
            conversion_goal_achieved=True,
            attempt_count=8,
            last_attempt_at=clock.now.isoformat(),
            next_attempt_at=(clock.now + timedelta(days=1)).isoformat(),
        )
    client.post("/v1/queue/release", headers=headers)
    clock.now = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    for index in range(2):
        _, packet = start(workspace)
        assert packet.status_code == 200, packet.text
        assert packet.json()["lead"]["phone"] == PHONE
        dial = client.post(
            "/v1/calls/start",
            headers=headers,
            json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": f"repeat-{index}"},
        )
        assert dial.status_code == 200, dial.text
        assert "time_limit_secs" not in carrier.dials[-1]
        assert carrier.dials[-1]["answering_machine_detection"] == "disabled"
        ended = client.post(
            "/v1/simulation/event",
            headers=headers,
            json={
                "event_id": f"no-answer-{index}",
                "call_id": dial.json()["id"],
                "event_type": "call.hangup",
                "payload": {"hangup_cause": "no_answer"},
            },
        )
        assert ended.status_code == 200, ended.text
        assert not ops.in_test_session(service, principal(workspace))


def test_a_stale_test_request_cannot_fall_through_to_prospects(workspace):
    client, headers = workspace["client"], workspace["headers"]
    ready(workspace)
    refused = client.post(
        "/v1/queue/next",
        headers=headers,
        json={"campaign_id": workspace["campaign_id"], "test_only": True},
    )
    assert refused.status_code == 409, refused.text
    assert "Test call" in refused.text


def test_a_test_call_revives_an_archived_lead_with_the_same_number(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    imported = client.post(
        "/v1/leads/import",
        headers=headers,
        json={
            "campaign_id": workspace["campaign_id"],
            "list_name": "Old tests",
            "rows": [{"name": "Owner delivery test", "phone": PHONE, "company": "Old"}],
            "prepare": False,
        },
    )
    assert imported.status_code == 200, imported.text
    with app.state.service.db.transaction(False) as s:
        from helpers import principal

        lead = next(
            m
            for m in ops.scoped(app.state.service, s, "leads", principal(workspace))
            if m.data["phone"] == PHONE
        )
    archived = client.post(
        "/v1/leads/bulk", headers=headers, json={"lead_ids": [lead.id], "action": "archive"}
    )
    assert archived.status_code == 200, archived.text
    save_phone(workspace)
    started, packet = start(workspace)
    assert packet.status_code == 200, packet.text
    assert packet.json()["lead"]["id"] == started["lead_id"] == lead.id
    assert packet.json()["lead"]["archived"] is True


def test_the_test_line_list_is_excluded_from_normal_calling(workspace):
    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace, "+12025550199")
    assert client.post("/v1/settings/test-call", headers=headers).is_success
    assert client.delete("/v1/settings/test-call", headers=headers).is_success
    ready(workspace)
    response = client.post(
        "/v1/queue/next",
        headers=headers,
        json={"campaign_id": workspace["campaign_id"], "list_names": ["Test line"]},
    )
    assert response.status_code == 409, response.text


def test_saving_the_test_call_outcome_ends_the_test_session(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace)
    _, packet = start(workspace)
    call = client.post(
        "/v1/calls/start",
        headers=headers,
        json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": "test-call-1"},
    ).json()
    assert call["is_test"] is True
    finish(workspace, call, "no_answer")
    assert ops.in_test_session(app.state.service, {"tenant_id": workspace["tenant_id"]}) is False


def test_ending_test_mode_invalidates_its_pending_dial(workspace, carrier):
    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace, "+12025550199")
    _, packet = start(workspace)
    assert packet.status_code == 200, packet.text
    assert client.delete("/v1/settings/test-call", headers=headers).status_code == 200
    snapshot = client.get("/v1/workspace", headers=headers).json()
    assert snapshot["test_phone"] is None and snapshot["reservation"] is None
    assert snapshot["user"]["presence"]["status"] == "paused"
    refused = client.post(
        "/v1/calls/start",
        headers=headers,
        json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": "ended-test"},
    )
    assert refused.status_code == 409 and refused.json()["detail"].startswith("Reservation expired")
    assert carrier.dials == []
    ready(workspace)
    next_call = client.post("/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]})
    assert next_call.status_code == 200, next_call.text
    assert next_call.json()["lead"]["phone"] != packet.json()["lead"]["phone"]


def test_a_test_session_without_a_test_phone_is_off(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    assert client.put("/v1/admin/settings", headers=headers, json={"test_session": True}).status_code == 409
    # An older workspace saved with the flag but no phone (the owner's, November 9) reads as off.
    app.state.service.configs.write(workspace["tenant_id"], {"test_session": True})
    assert ops.in_test_session(app.state.service, {"tenant_id": workspace["tenant_id"]}) is False


def test_an_empty_queue_says_why(workspace, clock):
    clock.now = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    ready(workspace)
    r = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "NO_ELIGIBLE_LEADS"
    assert detail["message"].startswith("Everyone left is outside calling hours"), detail
    assert detail["next_action"] == "Keep calling on to retry, or pause and return later."


def test_mixed_queue_reasons_keep_the_reason_for_the_fix_visible():
    mixed = {"OUTSIDE_CALLING_WINDOW", "NOT_DUE", "ATTEMPT_LIMIT", "DNC_NOT_CHECKED"}
    detail = no_lead_detail(None, None, None, mixed)
    assert detail["fix"] == "settings"
    assert detail["message"].startswith("HubSpot leads are waiting for do-not-call screening.")
    assert "outside calling hours" in detail["message"]


def test_readiness_says_how_many_can_be_called_or_why_none(workspace, clock):
    client, headers = workspace["client"], workspace["headers"]
    now = client.get("/v1/calling/readiness", headers=headers).json()
    assert now["blockers"] == [] and now["callable_now"] > 0 and now["why_none"] is None
    clock.now = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    later = client.get("/v1/calling/readiness", headers=headers).json()
    assert later["callable_now"] == 0
    assert later["why_none"].startswith("Everyone left is outside calling hours"), later
