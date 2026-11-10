"""Workspace access (local, simulation, one user), definitions, event feed and leads over HTTP only."""

from datetime import timedelta

from journey import call, get, hang_up, import_leads, post, start, wrap_up

OWNER = {"seller_name": "Example Seller", "campaign_name": "Acceptance campaign"}
LOCAL = {"X-Local-Token": "test-local-secret"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def lead_row(ws, lead_id):
    return next(item for item in get(ws, "/v1/workspace/leads")["items"] if item["id"] == lead_id)


def test_setup_and_the_local_session(client):
    """R3.4: one user, no passwords or roles. The launch token is the only way in; a session lives in memory."""
    assert client.get("/health").json()["initialized"] is False
    assert client.post("/v1/local/session", headers=LOCAL).status_code == 409
    assert client.post("/v1/setup", headers={"X-Local-Token": "wrong"}, json=OWNER).status_code == 401
    assert client.post("/v1/setup", headers=LOCAL, json=OWNER).status_code == 200
    assert client.post("/v1/setup", headers=LOCAL, json=OWNER).status_code == 409
    health = client.get("/health").json()
    assert (health["initialized"], health["mode"], health["simulation"]) == (True, "local", True)
    assert client.post("/v1/local/session", headers={"X-Local-Token": "wrong"}).status_code == 401
    session = client.post("/v1/local/session", headers=LOCAL)
    assert session.status_code == 200 and "role" not in session.json()
    token = bearer(session.json()["access_token"])
    assert client.get("/v1/workspace").status_code == 401
    assert client.get("/v1/workspace", headers=bearer("not-a-session")).status_code == 401
    assert "role" not in client.get("/v1/workspace", headers=token).json()["user"]
    assert client.get("/v1/quiescence", headers=token).json() == {"can_disconnect": True, "reasons": []}
    bounds = client.get("/v1/definitions", headers=token).json()["bounds"]
    assert bounds["policy"]["max_attempts"]["max"] == 20
    assert client.get("/v1/definitions").status_code == 401
    for gone in ("/v1/auth/session", "/v1/auth/logout", "/v1/admin/users"):
        assert client.post(gone, headers=token, json={}).status_code in {404, 405}


def test_workspace_snapshot_sections_search_and_event_feed(workspace):
    ws = workspace
    snapshot = get(ws, "/v1/workspace")
    assert snapshot["campaign"]["id"] == ws["campaign_id"] and snapshot["policy"]["id"] == ws["policy_id"]
    assert {"leads", "calls", "tasks", "numbers", "readiness", "event_cursor"} <= snapshot.keys()
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


def test_wrong_party_holds_the_lead_until_its_endpoint_is_corrected(workspace):
    ws = workspace
    started = start(ws)
    hang_up(ws, started["id"])
    wrap_up(ws, started["id"], outcome="wrong_party")
    lead_id = started["lead_id"]
    held = lead_row(ws, lead_id)
    assert held["status"] == "needs_correction" and held["suppressed"] is True
    fix = {"phone": "+12025550199", "evidence": "CRM ticket 42"}
    call(ws, "PATCH", f"/v1/leads/{lead_id}/endpoint", json={"phone": "bad", "evidence": "x"}, status=422)
    call(ws, "PATCH", f"/v1/leads/{lead_id}/endpoint", json={"phone": fix["phone"]}, status=422)
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


def test_an_active_call_holds_updates_until_it_is_wrapped_up(workspace):
    ws = workspace
    started = start(ws)
    quiet = get(ws, "/v1/quiescence")
    assert quiet["can_disconnect"] is False and "ACTIVE_CALL" in quiet["reasons"]
    hang_up(ws, started["id"])
    wrap_up(ws, started["id"], outcome="not_interested")
    assert get(ws, "/v1/quiescence")["can_disconnect"] is True


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
