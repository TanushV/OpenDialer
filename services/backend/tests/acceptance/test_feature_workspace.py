"""Workspace access (local, simulation, hosted roles), definitions, event feed and leads over HTTP only."""

import os
from datetime import timedelta

import pytest
from app.main import create_app
from conftest import disposition_body, fixture_settings
from fastapi.testclient import TestClient
from journey import call, get, hang_up, import_leads, post, start, wrap_up

OWNER = {
    "seller_name": "Example Seller",
    "campaign_name": "Acceptance campaign",
    "email": "operator@example.test",
    "password": "a-test-only-password-123",
}
REP_PASSWORD = "rep-test-password-123"
LOCAL = {"X-Local-Token": "test-local-secret"}
BOOTSTRAP = {"X-Bootstrap-Token": "test-bootstrap-secret"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def add_user(ws, email, role="rep", **extra):
    made = post(
        ws,
        "/v1/admin/users",
        {"email": email, "name": role.title(), "role": role, "password": REP_PASSWORD, **extra},
    )
    response = ws["client"].post("/v1/auth/session", json={"email": email, "password": REP_PASSWORD})
    assert response.status_code == 200, response.text
    return made, bearer(response.json()["access_token"])


def lead_row(ws, lead_id):
    return next(item for item in get(ws, "/v1/workspace/leads")["items"] if item["id"] == lead_id)


@pytest.fixture
def hosted(request, tmp_path, clock, carrier):
    url = f"sqlite:///{tmp_path}/hosted.db"
    if os.getenv("OPENDIALER_TEST_POSTGRES_URL"):
        store = request.getfixturevalue("postgres_store")
        store.reset(store.initialize)
        url = store.url
    settings = fixture_settings(url, tmp_path).model_copy(update={"mode": "connected"})
    with TestClient(create_app(settings, clock=clock, carrier=carrier)) as client:
        yield client


def test_setup_session_and_login_lifecycle(client):
    assert client.get("/health").json()["initialized"] is False
    assert client.post("/v1/local/session", headers=LOCAL).status_code == 409
    assert client.post("/v1/setup", headers={"X-Local-Token": "wrong"}, json=OWNER).status_code == 401
    assert client.post("/v1/setup", headers=LOCAL, json=OWNER).status_code == 200
    assert client.post("/v1/setup", headers=LOCAL, json=OWNER).status_code == 409
    health = client.get("/health").json()
    assert (health["initialized"], health["mode"], health["simulation"]) == (True, "local", True)
    session = client.post("/v1/local/session", headers=LOCAL)
    assert session.status_code == 200 and session.json()["role"] == "owner"
    token = bearer(session.json()["access_token"])
    assert client.get("/v1/workspace").status_code == 401
    assert client.get("/v1/workspace", headers=token).json()["user"]["role"] == "owner"
    assert client.get("/v1/auth/quiescence", headers=token).json() == {"can_disconnect": True, "reasons": []}
    bounds = client.get("/v1/definitions", headers=token).json()["bounds"]
    assert bounds["policy"]["max_attempts"]["max"] == 20
    assert client.get("/v1/definitions").status_code == 401
    wrong = {"email": OWNER["email"], "password": "wrong-password-123"}
    assert client.post("/v1/auth/session", json=wrong).status_code == 401
    login = client.post("/v1/auth/session", json={"email": OWNER["email"], "password": OWNER["password"]})
    assert login.status_code == 200
    login_headers = bearer(login.json()["access_token"])
    assert client.get("/v1/workspace", headers=login_headers).status_code == 200
    assert client.post("/v1/auth/logout", headers=login_headers).json() == {"logged_out": True}
    assert client.get("/v1/workspace", headers=login_headers).status_code == 401


def test_workspace_snapshot_sections_search_and_event_feed(workspace):
    ws = workspace
    snapshot = get(ws, "/v1/workspace")
    assert snapshot["campaign"]["id"] == ws["campaign_id"] and snapshot["policy"]["id"] == ws["policy_id"]
    assert {"leads", "calls", "tasks", "numbers", "readiness", "metrics", "event_cursor"} <= snapshot.keys()
    listing = get(ws, "/v1/workspace/leads")
    assert {lead["name"] for lead in listing["items"]} == {"Jordan Ellis", "Morgan Reed", "Casey Nguyen"}
    assert [lead["name"] for lead in get(ws, "/v1/workspace/leads", params={"q": "harbor"})["items"]] == [
        "Morgan Reed"
    ]
    assert get(ws, "/v1/workspace/leads", params={"q": "no-such-lead"})["items"] == []
    get(ws, "/v1/workspace/unknown-section", 404)
    get(ws, "/v1/workspace/leads", 422, params={"filter": "bogus"})
    cursor = snapshot["event_cursor"]
    started = start(ws)
    feed = get(ws, f"/v1/events?after={cursor}")
    assert ("call.updated", started["id"]) in {(e["type"], e["entity_id"]) for e in feed["events"]}
    assert all(e["sequence"] > cursor for e in feed["events"])
    assert get(ws, f"/v1/events?after={feed['cursor']}")["events"] == []


def test_hosted_owner_admin_and_rep_roles_are_enforced(hosted):
    c = hosted
    assert c.post("/v1/setup", json=OWNER).status_code == 401
    assert c.post("/v1/local/session", headers=LOCAL).status_code == 403
    setup = c.post("/v1/setup", headers=BOOTSTRAP, json=OWNER)
    assert setup.status_code == 200, setup.text
    owner = bearer(setup.json()["access_token"])
    assert (
        c.post("/v1/setup", headers=BOOTSTRAP, json={**OWNER, "email": "other@example.test"}).status_code
        == 409
    )
    assert c.post("/v1/simulation/seed", headers=owner).status_code == 200
    for email, role, sip in [
        ("admin@example.test", "admin", None),
        ("rep@example.test", "rep", "rep-line-1"),
    ]:
        made = c.post(
            "/v1/admin/users",
            headers=owner,
            json={
                "email": email,
                "name": role.title(),
                "role": role,
                "password": REP_PASSWORD,
                "sip_username": sip,
            },
        )
        assert made.status_code == 200, made.text
    dup = {"email": "rep@example.test", "role": "rep", "password": REP_PASSWORD}
    assert c.post("/v1/admin/users", headers=owner, json=dup).status_code == 409
    owner_role = {"email": "x@example.test", "role": "owner", "password": REP_PASSWORD}
    assert c.post("/v1/admin/users", headers=owner, json=owner_role).status_code == 422
    short = {"email": "y@example.test", "role": "rep", "password": "short"}
    assert c.post("/v1/admin/users", headers=owner, json=short).status_code == 422

    def login(email):
        response = c.post("/v1/auth/session", json={"email": email, "password": REP_PASSWORD})
        assert response.status_code == 200, response.text
        return bearer(response.json()["access_token"])

    rep, admin = login("rep@example.test"), login("admin@example.test")
    snapshot = c.get("/v1/workspace", headers=rep).json()
    assert (
        snapshot["user"]["role"] == "rep" and snapshot["mode"] == "connected" and len(snapshot["leads"]) == 3
    )
    refused = [
        ("GET", "/v1/admin/settings", None),
        ("POST", "/v1/admin/users", {"email": "z@example.test", "role": "rep", "password": REP_PASSWORD}),
        ("POST", "/v1/leads/import", {"campaign_id": snapshot["campaign"]["id"], "rows": []}),
        ("POST", "/v1/admin/control/run", None),
        ("GET", "/v1/admin/recovery", None),
    ]
    for method, path, body in refused:
        assert c.request(method, path, headers=rep, json=body).status_code == 403, path
    assert c.get("/v1/admin/settings", headers=admin).status_code == 200
    second = {**OWNER, "seller_name": "Second Seller", "email": "second@example.test"}
    assert c.post("/v1/admin/workspaces", headers=admin, json=second).status_code == 403
    created = c.post("/v1/admin/workspaces", headers=owner, json=second)
    assert created.status_code == 200, created.text
    other = c.get("/v1/workspace", headers=bearer(created.json()["access_token"])).json()
    assert other["seller"]["name"] == "Second Seller" and other["leads"] == []
    assert c.get("/v1/workspace", headers=rep).json()["seller"]["name"] == "Example Seller"
    assert c.post("/v1/auth/logout", headers=rep).json() == {"logged_out": True}
    assert c.get("/v1/workspace", headers=rep).status_code == 401


def test_lead_rows_import_reports_invalid_and_duplicate_rows(workspace):
    ws = workspace
    result = import_leads(
        ws,
        [
            {"name": "Ada Lovelace", "phone": "+12025550111", "company": "Analytical Example"},
            {"name": "No Phone", "phone": "12"},
            {"name": "Jordan Again", "phone": "+14155550101"},
        ],
    )
    assert (result["imported"], result["invalid"], result["duplicates"]) == (1, 1, 1)
    assert result["errors"] == [{"row": 2, "reason": "INVALID_PHONE"}]
    found = get(ws, "/v1/workspace/leads", params={"q": "Ada"})["items"]
    assert [(lead["name"], lead["status"]) for lead in found] == [("Ada Lovelace", "eligible")]
    _, rep = add_user(ws, "rep@example.test")
    post(ws, "/v1/leads/import", {"campaign_id": ws["campaign_id"], "rows": []}, headers=rep, status=403)


def test_wrong_party_holds_the_lead_until_its_endpoint_is_corrected(workspace):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test")
    started = start(ws)
    hang_up(ws, started["id"])
    wrap_up(ws, started["id"], outcome="wrong_party")
    lead_id = started["lead_id"]
    held = lead_row(ws, lead_id)
    assert held["status"] == "needs_correction" and held["suppressed"] is True
    fix = {"phone": "+12025550199", "evidence": "CRM ticket 42"}
    call(ws, "PATCH", f"/v1/leads/{lead_id}/endpoint", json={"phone": "bad", "evidence": "x"}, status=422)
    call(ws, "PATCH", f"/v1/leads/{lead_id}/endpoint", json={"phone": fix["phone"]}, status=422)
    call(ws, "PATCH", f"/v1/leads/{lead_id}/endpoint", json=fix, headers=rep, status=403)
    call(ws, "PATCH", f"/v1/leads/{lead_id}/endpoint", json=fix, status=200)
    fixed = lead_row(ws, lead_id)
    assert fixed["phone"] == "+12025550199" and fixed["status"] == "eligible"
    assert fixed["suppressed"] is False


def test_conversions_record_list_retract_and_move_goal_status(workspace, clock):
    ws = workspace
    lead_id = get(ws, "/v1/workspace/leads")["items"][0]["id"]
    body = {
        "event_id": "crm-meeting-1",
        "kind": "meeting_held",
        "occurred_at": (clock.now - timedelta(minutes=5)).isoformat(),
        "source_reference": "CRM opportunity 9",
    }
    recorded = post(ws, f"/v1/leads/{lead_id}/conversions", body)
    assert recorded["kind"] == "meeting_held" and not recorded.get("retracted")
    assert post(ws, f"/v1/leads/{lead_id}/conversions", body)["id"] == recorded["id"]
    post(
        ws, f"/v1/leads/{lead_id}/conversions", {**body, "source_reference": "A different record"}, status=409
    )
    future = {**body, "event_id": "crm-future-1", "occurred_at": (clock.now + timedelta(days=1)).isoformat()}
    post(ws, f"/v1/leads/{lead_id}/conversions", future, status=422)
    assert lead_row(ws, lead_id)["status"] == "converted"
    assert [row["id"] for row in get(ws, f"/v1/leads/{lead_id}/conversions")] == [recorded["id"]]
    retract = {"reason": "Entered on the wrong lead"}
    retracted = post(ws, f"/v1/leads/{lead_id}/conversions/{recorded['id']}/retract", retract)
    assert retracted["retracted"] is True
    assert lead_row(ws, lead_id)["status"] == "eligible"
    assert get(ws, f"/v1/leads/{lead_id}/conversions")[0]["retracted"] is True


def test_active_call_blocks_logout_until_it_is_wrapped_up(workspace):
    ws = workspace
    started = start(ws)
    quiet = get(ws, "/v1/auth/quiescence")
    assert quiet["can_disconnect"] is False and "ACTIVE_CALL" in quiet["reasons"]
    blocked = post(ws, "/v1/auth/logout", status=409)
    assert blocked["detail"]["code"] == "AUTHORITY_HAS_OBLIGATIONS"
    hang_up(ws, started["id"])
    wrap_up(ws, started["id"], outcome="not_interested")
    assert get(ws, "/v1/auth/quiescence")["can_disconnect"] is True
    assert post(ws, "/v1/auth/logout") == {"logged_out": True}


def hosted_owner(c):
    setup = c.post("/v1/setup", headers=BOOTSTRAP, json=OWNER)
    assert setup.status_code == 200, setup.text
    owner = bearer(setup.json()["access_token"])
    seeded = c.post("/v1/simulation/seed", headers=owner).json()
    return {"client": c, "headers": owner, **setup.json(), **seeded}


def start_as(ws, headers, key):
    packet = call(ws, "POST", "/v1/queue/next", json={"campaign_id": ws["campaign_id"]}, headers=headers)
    body = {"reservation_id": packet["reservation_id"], "idempotency_key": key}
    return call(ws, "POST", "/v1/calls/start", json=body, headers=headers)


def ready_as(ws, headers):
    body = {
        "status": "ready",
        "audio_ready": True,
        "telephony_ready": True,
        "agent_call_control_id": "agent-test-id",
    }
    return call(ws, "POST", "/v1/rep/presence", json=body, headers=headers)


def test_rep_event_feed_hides_other_reps_call_events_in_hosted_mode(hosted):
    ws = hosted_owner(hosted)
    _, rep_a = add_user(ws, "rep-a@example.test")
    _, rep_b = add_user(ws, "rep-b@example.test")
    ready_as(ws, rep_a)
    mine = start_as(ws, rep_a, "rep-a-call")
    assert mine["id"] in {e["entity_id"] for e in get(ws, "/v1/events?after=0", headers=rep_a)["events"]}
    assert mine["id"] in {e["entity_id"] for e in get(ws, "/v1/events?after=0")["events"]}
    seen_by_b = {e["entity_id"] for e in get(ws, "/v1/events?after=0", headers=rep_b)["events"]}
    assert mine["id"] not in seen_by_b


def test_rep_event_feed_hides_other_reps_task_events_in_hosted_mode(hosted):
    ws = hosted_owner(hosted)
    _, rep_a = add_user(ws, "rep-a@example.test")
    _, rep_b = add_user(ws, "rep-b@example.test")
    ready_as(ws, rep_a)
    mine = start_as(ws, rep_a, "rep-a-call")
    hang_up(ws, mine["id"])
    saved = wrap_up(ws, mine["id"], outcome="callback_requested", callback_note="Call back")
    seen_by_b = {e["entity_id"] for e in get(ws, "/v1/events?after=0", headers=rep_b)["events"]}
    assert saved["canonical_task_id"] not in seen_by_b


def test_rep_takes_and_wraps_up_a_call_in_hosted_mode_and_sees_only_their_own(hosted, clock):
    c = hosted
    setup = c.post(
        "/v1/setup",
        headers={"X-Bootstrap-Token": "test-bootstrap-secret"},
        json={
            "seller_name": "Example Team",
            "campaign_name": "Team campaign",
            "email": "owner@example.test",
            "password": "a-test-only-password-123",
        },
    )
    assert setup.status_code == 200, setup.text
    owner = bearer(setup.json()["access_token"])
    seeded = c.post("/v1/simulation/seed", headers=owner).json()
    ws = {"client": c, "headers": owner, **setup.json(), **seeded}
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    owner_call = start(ws)
    hang_up(ws, owner_call["id"])
    wrap_up(ws, owner_call["id"], outcome="not_interested")
    ready_as(ws, rep)
    rep_call = start_as(ws, rep, "hosted-call-1")
    assert rep_call["state"] == "RINGING" and rep_call["lead_id"] != owner_call["lead_id"]
    hang_up(ws, rep_call["id"])
    body = disposition_body(c, rep, rep_call["id"], outcome="callback_requested", callback_note="Call back")
    saved = call(ws, "POST", f"/v1/calls/{rep_call['id']}/disposition", json=body, headers=rep)
    assert saved["canonical_task_id"] and saved["state"] == "FINALIZED"
    mine = {item["id"] for item in get(ws, "/v1/workspace/calls", headers=rep)["items"]}
    assert rep_call["id"] in mine and owner_call["id"] not in mine
    call(ws, "GET", f"/v1/calls/{owner_call['id']}", headers=rep, status=404)
    assert c.get("/v1/admin/recovery", headers=rep).status_code == 403
