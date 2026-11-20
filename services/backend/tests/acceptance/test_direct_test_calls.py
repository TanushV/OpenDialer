"""Test phones share the calling engine without becoming outreach contacts."""

from datetime import UTC, datetime

import pytest
from app import ops
from app.platform.db import MODELS, Job, update
from helpers import principal
from journey import call, get, post, ready
from sqlalchemy import delete, select

PHONE = "+12025550185"


def outreach_records(app, ws):
    service = app.state.service
    p = principal(ws)
    with service.db.transaction(False) as session:
        return {
            kind: {row.id: dict(row.data) for row in ops.scoped(service, session, kind, p)}
            for kind in (
                "campaigns",
                "leads",
                "lead_number_bindings",
                "script_versions",
                "policy_decisions",
                "compliance_decisions",
                "feature_snapshots",
            )
        }


@pytest.mark.parametrize("from_list", [False, True])
def test_test_call_uses_an_isolated_recipient_after_hours(workspace, app, carrier, clock, from_list):
    ws = workspace
    p = principal(ws)
    service = app.state.service
    campaign = ws["campaign_id"]
    prospect = post(
        ws,
        "/v1/leads/import",
        {"campaign_id": campaign, "list_name": "Outreach", "rows": [{"name": "Prospect", "phone": PHONE}]},
    )["leads"][0]
    before = outreach_records(app, ws)
    clock.now = datetime(2026, 10, 15, 1, tzinfo=UTC)
    service.require_prepared_scripts = True
    request = {"campaign_id": campaign, "test_only": True}
    if from_list:
        call(ws, "PUT", "/v1/settings/test-list", json={"rows": [{"phone": PHONE, "name": "Test phone"}]})
        request["test_run_id"] = post(ws, "/v1/settings/test-list/start")["id"]
    else:
        call(ws, "PUT", "/v1/admin/settings", json={"test_phone": PHONE})
        post(ws, "/v1/settings/test-call")
    ready(ws)
    held = post(ws, "/v1/queue/next", request)
    assert held["lead"]["phone"] == PHONE
    dial = {"reservation_id": held["reservation_id"], "idempotency_key": "isolated-test"}
    placed = post(ws, "/v1/calls/start", dial)
    assert placed["is_test"] and [leg["to"] for leg in carrier.dials] == [PHONE]
    assert post(ws, "/v1/calls/start", dial)["id"] == placed["id"]
    post(
        ws,
        "/v1/simulation/event",
        {
            "event_id": "test-no-answer",
            "call_id": placed["id"],
            "event_type": "call.hangup",
            "payload": {"hangup_cause": "no_answer"},
        },
    )
    saved = get(ws, f"/v1/calls/{placed['id']}")
    assert saved["state"] == "FINALIZED" and saved["outcome"] == "no_answer"
    assert post(ws, "/v1/queue/next", request, status=409)
    assert len(carrier.dials) == 1
    assert outreach_records(app, ws) == before
    assert placed["lead_id"] != prospect["id"]
    with service.db.transaction(False) as session:
        assert not any(
            job.payload.get("call_id") == placed["id"]
            for job in session.scalars(select(Job).where(Job.tenant_id == p["tenant_id"]))
        )


def test_calling_setup_can_test_audio_without_an_outreach_list_or_callback_proof(workspace, app, carrier):
    ws = workspace
    service = app.state.service
    p = principal(ws)
    call(ws, "PUT", "/v1/admin/settings", json={"test_phone": PHONE})
    with service.db.transaction() as session:
        for kind in ("leads", "campaigns"):
            session.execute(delete(MODELS[kind]).where(MODELS[kind].tenant_id == p["tenant_id"]))
        update(ops.required(service, session, "sellers", p["seller_id"], p), callback_test=None)
    started = post(ws, "/v1/settings/test-call")
    ready(ws)
    assert get(ws, "/v1/calling/readiness")["callable_now"] == 1
    held = post(ws, "/v1/calls/test/reserve")
    assert get(ws, f"/v1/test-contacts/{started['lead_id']}")["phone"] == PHONE
    placed = post(
        ws,
        "/v1/calls/start",
        {
            "reservation_id": held["reservation_id"],
            "idempotency_key": "test-before-outreach",
        },
    )
    assert [d["to"] for d in carrier.dials] == [PHONE]
    assert get(ws, f"/v1/calls/{placed['id']}/review")["call"]["id"] == placed["id"]
    assert not any(outreach_records(app, ws)[kind] for kind in ("campaigns", "leads"))


def test_saved_legacy_test_phones_carry_over_without_touching_old_reviews(workspace, app):
    ws = workspace
    old = post(
        ws,
        "/v1/leads/import",
        {
            "campaign_id": ws["campaign_id"],
            "list_name": "Test calls",
            "rows": [{"name": "Saved test phone", "phone": PHONE}],
            "prepare": False,
        },
    )["leads"][0]
    service = app.state.service
    p = principal(ws)
    with service.db.transaction() as session:
        update(ops.required(service, session, "leads", old["id"], p), test_list_contact=True)
    before = outreach_records(app, ws)
    carried = get(ws, "/v1/settings/test-list")["contacts"]
    assert [(c["phone"], c["name"]) for c in carried] == [(PHONE, "Saved test phone")]
    assert carried[0]["id"] != old["id"]
    assert get(ws, "/v1/settings/test-list")["contacts"] == carried
    assert outreach_records(app, ws) == before
