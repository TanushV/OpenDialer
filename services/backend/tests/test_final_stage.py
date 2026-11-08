"""The final stage may pair an unanswered next-to-last attempt with one immediate, rep-pressed retry."""

from datetime import timedelta
from uuid import uuid4

import pytest
from app.db import MODELS, update
from sqlalchemy import select

PASSWORD = "a-test-only-password-123"


def call(ws, method, path, json=None, status=200):
    response = ws["client"].request(method, path, headers=ws["headers"], json=json)
    assert response.status_code == status, (path, response.text)
    return response.json()


def post(ws, path, json=None, status=200):
    return call(ws, "POST", path, {} if json is None else json, status)


def go_ready(ws, agent="agent-test-id"):
    body = {"status": "ready", "audio_ready": True, "telephony_ready": True, "agent_call_control_id": agent}
    post(ws, "/v1/rep/presence", body)


def next_lead(ws, status=200):
    go_ready(ws, ws.get("agent", "agent-test-id"))
    return post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]}, status)


def dial(ws):
    reservation = next_lead(ws)
    body = {"reservation_id": reservation["reservation_id"], "idempotency_key": str(uuid4())}
    return post(ws, "/v1/calls/start", body)


def event(ws, call_id, event_type, **payload):
    body = {"event_id": str(uuid4()), "event_type": event_type, "call_id": call_id, "payload": payload}
    return post(ws, "/v1/simulation/event", body)


def finish(ws):
    for _ in range(2):
        post(ws, "/v1/admin/jobs/run")


def one_lead_in_final_stage(app, ws, attempts=2):
    """Leave one California lead whose next dial is its next-to-last of four."""
    for lead in call(ws, "GET", "/v1/workspace/leads")["items"]:
        if not lead["phone"].startswith("+1415"):
            post(ws, "/v1/suppressions", {"phone": lead["phone"], "reason": "opt_out"})
    with app.state.db.transaction() as s:
        for lead in s.scalars(select(MODELS["leads"])):
            update(lead, attempt_count=attempts)


def unanswered(ws):
    placed = dial(ws)
    event(ws, placed["id"], "call.hangup", hangup_cause="no_answer")
    finish(ws)
    return placed


def test_an_unanswered_next_to_last_attempt_offers_the_same_rep_one_immediate_final_attempt(
    workspace, app, carrier
):
    one_lead_in_final_stage(app, workspace)
    first = unanswered(workspace)
    assert len(carrier.dials) == 1, "Nothing dials until the rep presses Space again"
    second = dial(workspace)
    assert second["lead_id"] == first["lead_id"] and len(carrier.dials) == 2
    saved = call(workspace, "GET", f"/v1/calls/{second['id']}")
    assert (saved["attempt_ordinal"], saved["attempt_allowance"]) == (4, 4)
    # The final attempt keeps a mailbox open for the rep's own message.
    event(workspace, second["id"], "call.answered")
    event(workspace, second["id"], "call.machine.detection.ended:machine")
    assert call(workspace, "GET", f"/v1/calls/{second['id']}")["voicemail_turn"] is True
    event(workspace, second["id"], "call.hangup")
    finish(workspace)
    assert next_lead(workspace, 409)["detail"]["code"] == "NO_ELIGIBLE_LEADS"


def test_the_offer_closes_after_five_minutes_and_belongs_to_the_rep_who_dialed(workspace, app, clock):
    one_lead_in_final_stage(app, workspace)
    post(
        workspace,
        "/v1/admin/users",
        {"email": "b@example.test", "name": "B", "role": "rep", "password": PASSWORD},
    )
    token = workspace["client"].post(
        "/v1/auth/session", json={"email": "b@example.test", "password": PASSWORD}
    )
    other = {
        **workspace,
        "headers": {"Authorization": f"Bearer {token.json()['access_token']}"},
        "agent": "b",
    }
    unanswered(workspace)
    assert next_lead(other, 409)["detail"]["code"] == "NO_ELIGIBLE_LEADS"
    post(workspace, "/v1/rep/presence", {"status": "paused"})
    clock.now += timedelta(minutes=5, seconds=1)
    assert next_lead(workspace, 409)["detail"]["code"] == "NO_ELIGIBLE_LEADS"


def test_no_second_attempt_after_a_conversation_or_outside_the_final_stage(workspace, app):
    one_lead_in_final_stage(app, workspace)
    spoken = dial(workspace)
    event(workspace, spoken["id"], "call.answered")
    event(workspace, spoken["id"], "human.confirmed")
    event(workspace, spoken["id"], "call.hangup")
    finish(workspace)
    assert next_lead(workspace, 409)["detail"]["code"] == "NO_ELIGIBLE_LEADS"
    with app.state.db.transaction() as s:
        for lead in s.scalars(select(MODELS["leads"])):
            update(lead, attempt_count=1, next_attempt_at=None, last_attempt_at=None)
    unanswered(workspace)  # The second of four is not in the final stage.
    assert next_lead(workspace, 409)["detail"]["code"] == "NO_ELIGIBLE_LEADS"


def sign_in(ws):
    token = ws["client"].post(
        "/v1/auth/session", json={"email": "operator@example.test", "password": PASSWORD}
    )
    ws["headers"] = {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest.mark.slow
def test_never_called_leads_come_before_retries_once_both_are_due(workspace, clock):
    """Coverage is re-anchored after each attempt, so tried leads cannot crowd out first calls."""
    for lead in call(workspace, "GET", "/v1/workspace/leads")["items"]:
        if not lead["phone"].startswith("+1415"):
            post(workspace, "/v1/suppressions", {"phone": lead["phone"], "reason": "opt_out"})
    clock.now += timedelta(hours=23)
    sign_in(workspace)
    tried = unanswered(workspace)
    post(
        workspace,
        "/v1/leads/import",
        {
            "campaign_id": workspace["campaign_id"],
            "rows": [{"name": "Fresh", "phone": "+14155550166", "state": "CA"}],
        },
    )
    clock.now += timedelta(hours=25)
    sign_in(workspace)
    offered = next_lead(workspace)["lead"]
    assert offered["phone"] == "+14155550166" and offered["id"] != tried["lead_id"]
