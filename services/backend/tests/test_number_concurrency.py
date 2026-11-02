"""Two reps calling at once never share a caller number that is reserved or on a call."""

from uuid import uuid4

from app.db import MODELS, put
from helpers import principal
from sqlalchemy import select

PASSWORD = "a-test-only-password-123"
CALIFORNIA = "+14155550100"


def call(ws, method, path, json=None):
    response = ws["client"].request(method, path, headers=ws["headers"], json=json)
    assert response.status_code == 200, (path, response.text)
    return response.json()


def get(ws, path):
    return call(ws, "GET", path)


def post(ws, path, json=None):
    return call(ws, "POST", path, {} if json is None else json)


def event(ws, call_id, event_type, **payload):
    body = {"event_id": str(uuid4()), "event_type": event_type, "call_id": call_id, "payload": payload}
    return post(ws, "/v1/simulation/event", body)


def run_jobs(ws, rounds=1):
    for _ in range(rounds):
        post(ws, "/v1/admin/jobs/run")


def import_leads(ws, rows):
    return post(ws, "/v1/leads/import", {"campaign_id": ws["campaign_id"], "rows": rows})


def rep(ws, email, agent):
    post(ws, "/v1/admin/users", {"email": email, "name": "Rep", "role": "rep", "password": PASSWORD})
    response = ws["client"].post("/v1/auth/session", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {**ws, "headers": {"Authorization": f"Bearer {response.json()['access_token']}"}, "agent": agent}


def go_ready(ws, agent="agent-test-id"):
    body = {"status": "ready", "audio_ready": True, "telephony_ready": True, "agent_call_control_id": agent}
    post(ws, "/v1/rep/presence", body)


def next_lead(ws):
    return ws["client"].post("/v1/queue/next", headers=ws["headers"], json={"campaign_id": ws["campaign_id"]})


def dial(ws):
    go_ready(ws, ws.get("agent", "agent-test-id"))
    response = next_lead(ws)
    assert response.status_code == 200, response.text
    reservation = response.json()
    return post(
        ws,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": str(uuid4())},
    )


def california_only(ws, extra=1):
    """Leave only California leads, which only the 415 number (or another California one) can serve."""
    for lead in get(ws, "/v1/workspace/leads")["items"]:
        if not lead["phone"].startswith("+1415"):
            post(ws, "/v1/suppressions", {"phone": lead["phone"], "reason": "opt_out"})
    import_leads(ws, [{"name": f"Lead {i}", "phone": f"+1415555012{i}", "state": "CA"} for i in range(extra)])


def add_california_number(app, ws, phone="+14155550109"):
    service, p = app.state.service, principal(ws)
    with service.db.transaction() as s:
        put(
            s,
            "number_inventory",
            p["tenant_id"],
            p["seller_id"],
            {
                "phone": phone,
                "region": "415",
                "lifecycle": "healthy",
                "capacity": 1,
                "daily_limit": 100,
                "identity_configured": True,
                "callback_tested": True,
                "registration_reference": "SIMULATION",
                "synthetic": True,
            },
            key=phone,
            now=service.clock(),
        )


def number_phone(ws, call):
    return get(ws, f"/v1/calls/{call['id']}")["number_id"]


def test_two_reps_calling_at_once_use_different_numbers(workspace, app):
    california_only(workspace)
    add_california_number(app, workspace)
    other = rep(workspace, "second@example.test", "agent-second")
    first, second = dial(workspace), dial(other)
    assert first["lead_id"] != second["lead_id"]
    assert number_phone(workspace, first) != number_phone(workspace, second)


def test_a_number_on_a_call_is_not_used_until_that_call_ends(workspace, app, carrier):
    california_only(workspace)
    other = rep(workspace, "second@example.test", "agent-second")
    first = dial(workspace)
    go_ready(other, "agent-second")
    busy = next_lead(other)
    assert busy.status_code == 409, busy.text
    assert {"NUMBER_UNHEALTHY", "NUMBER_NOT_AUTHORIZED"} & set(busy.json()["detail"]["reasons"])
    assert len(carrier.dials) == 1
    event(workspace, first["id"], "call.hangup", hangup_cause="no_answer")
    run_jobs(workspace, rounds=2)
    second = dial(other)
    assert second["lead_id"] != first["lead_id"]
    assert number_phone(workspace, second) == number_phone(workspace, first)


def test_claiming_one_lead_never_rebinds_another_leads_caller_number(workspace, app):
    california_only(workspace, extra=2)
    add_california_number(app, workspace)
    service, p = app.state.service, principal(workspace)
    first = dial(workspace)
    event(workspace, first["id"], "call.hangup", hangup_cause="no_answer")
    run_jobs(workspace, rounds=2)
    second = dial(workspace)
    assert second["lead_id"] != first["lead_id"]
    with service.db.transaction(False) as s:
        bindings = {
            row.data["lead_id"]: row.data["number_id"]
            for row in s.scalars(select(MODELS["lead_number_bindings"]))
            if row.tenant_id == p["tenant_id"]
        }
    assert bindings[first["lead_id"]] == get(workspace, f"/v1/calls/{first['id']}")["number_id"]
    assert bindings[second["lead_id"]] == get(workspace, f"/v1/calls/{second['id']}")["number_id"]


def test_a_claim_binds_its_own_lead_when_other_bound_leads_share_the_queue_page(workspace, app):
    california_only(workspace)
    add_california_number(app, workspace)
    service, p = app.state.service, principal(workspace)
    leads = [
        lead for lead in get(workspace, "/v1/workspace/leads")["items"] if lead["phone"].startswith("+1415")
    ]
    numbers = {n["phone"]: n["id"] for n in get(workspace, "/v1/workspace")["numbers"]}
    waiting = leads[0]["id"]
    with service.db.transaction() as s:
        put(
            s,
            "lead_number_bindings",
            p["tenant_id"],
            p["seller_id"],
            {"lead_id": waiting, "number_id": numbers["+14155550109"]},
            key=waiting,
            now=service.clock(),
        )
    import_leads(
        workspace,
        [{"name": "Asked first", "phone": "+14155550177", "state": "CA", "requested_contact": True}],
    )
    placed = dial(workspace)
    assert placed["lead_id"] != waiting
    with service.db.transaction(False) as s:
        bindings = {
            row.data["lead_id"]: row.data["number_id"]
            for row in s.scalars(select(MODELS["lead_number_bindings"]))
            if row.tenant_id == p["tenant_id"]
        }
    assert bindings[waiting] == numbers["+14155550109"]
    assert bindings[placed["lead_id"]] == get(workspace, f"/v1/calls/{placed['id']}")["number_id"]
