from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from conftest import ready, reserve, start


def test_health_is_redacted_and_unauthenticated_access_is_rejected(client):
    assert client.get("/health").status_code == 200
    r = client.get("/v1/workspace")
    assert r.status_code == 401
    assert "test-local-secret" not in client.get("/health").text
    assert client.post("/v1/local/session").status_code == 401


def test_local_setup_idempotency_and_scoped_session(workspace):
    c, h = workspace["client"], workspace["headers"]
    snapshot = c.get("/v1/workspace", headers=h)
    assert snapshot.status_code == 200
    assert snapshot.json()["mode"] == "local"
    assert snapshot.json()["simulation"] is True
    assert "api_key" not in snapshot.text
    assert c.post("/v1/setup", json={}).status_code in (401, 409, 422)
    r = c.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"})
    assert r.status_code == 200


def test_import_normalizes_dedupes_and_preserves_suppression(workspace):
    c, h = workspace["client"], workspace["headers"]
    suppressed = "+14155550177"
    assert (
        c.post("/v1/suppressions", headers=h, json={"phone": suppressed, "reason": "opt_out"}).status_code
        == 200
    )
    rows = [
        {"name": "Ada", "phone": "415-555-0177", "timezone": "America/Los_Angeles"},
        {"name": "Ada again", "phone": suppressed},
        {"name": "Bad", "phone": "bad"},
    ]
    r = c.post("/v1/leads/import", headers=h, json={"campaign_id": workspace["campaign_id"], "rows": rows})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
    assert r.json()["duplicates"] == 1
    assert r.json()["invalid"] == 1
    imported = r.json()["leads"][0]
    assert imported["suppressed"] is True


def test_claim_cannot_double_reserve_and_is_kept_during_active_call(workspace, clock):
    call = start(workspace)
    clock.now += timedelta(minutes=5)
    r = reserve(workspace)
    assert r.status_code == 409
    assert "LIVE_OBLIGATION" in r.text
    assert (
        workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"]).status_code
        == 200
    )


def test_concurrent_requests_create_one_endpoint_claim(workspace):
    ready(workspace)

    def get_next(_):
        return workspace["client"].post(
            "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(get_next, range(6)))
    assert sum(r.status_code == 200 for r in responses) == 1
    assert all(r.status_code in (200, 409) for r in responses)


def test_permission_is_rechecked_after_queueing(workspace, carrier):
    packet = reserve(workspace).json()
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/suppressions", headers=h, json={"phone": packet["lead"]["phone"], "reason": "opt_out"})
    r = c.post(
        "/v1/calls/start",
        headers=h,
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "suppressed-key"},
    )
    assert r.status_code in (403, 409)
    assert carrier.dials == []


def test_policy_change_after_reservation_blocks_start(workspace, carrier):
    packet = reserve(workspace).json()
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/admin/policies/{workspace['policy_id']}/publish",
        headers=h,
        json={"approved": False, "reason": "Stop campaign"},
    )
    r = c.post(
        "/v1/calls/start",
        headers=h,
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "policy-changed"},
    )
    assert r.status_code in (403, 409)
    assert not carrier.dials


def test_call_start_idempotency_and_explicit_identity(workspace, carrier):
    packet = reserve(workspace).json()
    body = {"reservation_id": packet["reservation_id"], "idempotency_key": "one-real-intent"}
    c, h = workspace["client"], workspace["headers"]
    a = c.post("/v1/calls/start", headers=h, json=body)
    b = c.post("/v1/calls/start", headers=h, json=body)
    assert a.status_code == b.status_code == 200
    assert a.json()["id"] == b.json()["id"]
    assert len(carrier.dials) == 1
    dial = carrier.dials[0]
    assert dial["from"] == packet["number"]["phone"]
    assert dial["bridge_on_answer"] is True
    assert dial["prevent_double_bridge"] is True
    assert dial["link_to"] == "agent-test-id"
    assert "bridge_intent" not in dial
    assert dial["time_limit_secs"] > 60


def test_no_audio_prevents_prospect_dial(workspace, carrier):
    ready(workspace, audio_ready=False)
    r = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert r.status_code in (403, 409)
    assert carrier.dials == []


def test_uncertain_dial_is_reconciled_without_second_dial(workspace, carrier):
    carrier.timeout_after_accept = True
    call = start(workspace, "uncertain-key")
    assert call["state"] == "RECONCILING"
    c, h = workspace["client"], workspace["headers"]
    r = c.post("/v1/admin/recovery", headers=h)
    assert r.status_code == 200
    assert len(carrier.dials) == 1
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["provider_id"] is not None


def test_wrong_number_holds_endpoint_but_keeps_lead_for_correction(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    r = c.post(f"/v1/calls/{call['id']}/disposition", headers=h, json={"outcome": "wrong_party"})
    assert r.status_code == 200
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    assert lead["status"] == "needs_correction"
    assert lead["suppressed"]


def test_rep_cannot_forge_destination_or_clearance(workspace):
    packet = reserve(workspace).json()
    r = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={
            "reservation_id": packet["reservation_id"],
            "idempotency_key": "forged",
            "to": "+19005550101",
            "from": "+14155550999",
            "dnc_clear": True,
        },
    )
    assert r.status_code == 422


def test_optout_cancels_pending_commands_and_local_outbox_blocks_new_calls(workspace):
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        "/v1/rep/presence",
        headers=h,
        json={
            "status": "ready",
            "audio_ready": True,
            "telephony_ready": True,
            "pending_optouts": 1,
            "agent_call_control_id": "agent-test-id",
        },
    )
    assert r.status_code == 200
    assert (
        c.post("/v1/queue/next", headers=h, json={"campaign_id": workspace["campaign_id"]}).status_code == 409
    )


def test_events_have_durable_cursor_and_no_replay_duplicates(workspace):
    start(workspace)
    c, h = workspace["client"], workspace["headers"]
    events = c.get("/v1/events?after=0", headers=h).json()
    assert events["events"]
    assert c.get(f"/v1/events?after={events['cursor']}", headers=h).json()["events"] == []
    assert all(e["tenant_id"] == workspace["tenant_id"] for e in events["events"])


def test_budget_exhaustion_does_not_break_hangup(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/budget", headers=h, json={"daily_limit_usd": 0, "monthly_limit_usd": 0})
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    r = c.post("/v1/research/batches", headers=h, json={"lead_ids": [call["lead_id"]]})
    assert r.status_code == 403


def test_nonexistent_and_cross_scope_objects_do_not_leak(workspace):
    c, h = workspace["client"], workspace["headers"]
    assert c.get("/v1/calls/00000000-0000-0000-0000-000000000000", headers=h).status_code == 404
    assert c.get("/v1/workspace", headers={"Authorization": "Bearer invalid"}).status_code == 401
