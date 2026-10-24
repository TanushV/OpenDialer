"""Compliance over HTTP: opt-outs, registries, consent, holds, calling windows, policy, approval and preflight."""

from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import save_operating_policy
from journey import get, import_leads, post, reserve, run_jobs

PT, ET, CT = (
    "+14155550101",
    "+12125550102",
    "+15125550103",
)  # Jordan (Pacific), Morgan (Eastern), Casey (Central)
PHONES = (PT, ET, CT)


def queue(ws):
    """Pull the queue's next lead, then pause so the unused reservation is released."""
    response = reserve(ws)
    post(ws, "/v1/rep/presence", {"status": "paused"})
    return response.status_code, response.json()


def offered(ws):
    status, body = queue(ws)
    assert status == 200, body
    return body["lead"]["phone"]


def refused(ws):
    """The refusal reasons the queue reports when no lead is dialable."""
    status, body = queue(ws)
    assert status == 409 and body["detail"]["code"] == "NO_ELIGIBLE_LEADS", body
    return set(body["detail"]["reasons"])


def settings(ws, **values):
    version = get(ws, "/v1/admin/settings")["version"]
    response = ws["client"].put(
        "/v1/admin/settings",
        headers=ws["headers"],
        json={"request_id": str(uuid4()), "expected_version": version, **values},
    )
    assert response.status_code == 200, response.text
    return response.json()


def publish(ws, **changes):
    policy = get(ws, "/v1/workspace")["policy"]
    return post(
        ws, f"/v1/admin/policies/{policy['id']}/publish", {"reason": "Reviewed acceptance change", **changes}
    )


def suppress_all_but(ws, keep):
    for phone in PHONES:
        if phone != keep:
            post(ws, "/v1/suppressions", {"phone": phone, "reason": "opt_out"})


def endpoints(ws):
    return {lead["phone"]: lead["endpoint_id"] for lead in get(ws, "/v1/workspace/leads")["items"]}


def test_internal_opt_out_blocks_the_next_lead_before_any_dial(workspace, carrier):
    first = offered(workspace)
    post(workspace, "/v1/suppressions", {"phone": first, "reason": "opt_out"})
    assert offered(workspace) != first
    flags = {lead["phone"]: lead["suppressed"] for lead in get(workspace, "/v1/workspace/leads")["items"]}
    assert flags[first] is True
    suppress_all_but(workspace, keep=None)
    assert "INTERNAL_SUPPRESSION" in refused(workspace)
    assert carrier.dials == []


def test_temporary_hold_stops_a_lead_until_a_reviewed_release(workspace):
    held = offered(workspace)
    suppress_all_but(workspace, keep=held)
    post(
        workspace,
        "/v1/suppressions",
        {"phone": held, "reason": "temporary_hold", "event_id": "interim-optout"},
    )
    assert "INTERNAL_SUPPRESSION" in refused(workspace)
    post(workspace, "/v1/admin/suppressions/release-hold", {"phone": held, "reason": "   "}, status=422)
    post(
        workspace,
        "/v1/admin/suppressions/release-hold",
        {"phone": held, "reason": "Final transcript showed no opt-out request"},
    )
    assert offered(workspace) == held


@pytest.mark.parametrize("reason", ["opt_out", "wrong_party"])
def test_a_permanent_denial_is_never_released_as_a_hold(workspace, reason):
    phone = offered(workspace)
    post(workspace, "/v1/suppressions", {"phone": phone, "reason": reason})
    post(
        workspace,
        "/v1/admin/suppressions/release-hold",
        {"phone": phone, "reason": "Reviewed again"},
        status=409,
    )
    assert offered(workspace) != phone


def test_national_registry_snapshot_blocks_a_listed_number_until_replaced(workspace, clock):
    listed = offered(workspace)
    suppress_all_but(workspace, keep=listed)
    source = {
        "kind": "national",
        "subscription_reference": "own-national-registry-2026",
        "npas": ["212", "415", "512"],
        "refreshed_at": clock.now.isoformat(),
    }
    listing = {**source, "numbers": [listed], "request_id": str(uuid4())}
    receipt = post(workspace, "/v1/admin/registry/import", listing)
    assert receipt["published"] is True and receipt["count"] == 1
    assert post(workspace, "/v1/admin/registry/import", listing)["id"] == receipt["id"]
    assert "REGISTRY_LISTED" in refused(workspace)
    post(
        workspace,
        "/v1/admin/registry/import",
        {**source, "numbers": ["+13125550199"], "request_id": str(uuid4())},
        status=422,
    )
    post(
        workspace,
        "/v1/admin/registry/import",
        {**source, "numbers": [], "refreshed_at": (clock.now + timedelta(days=1)).isoformat()},
        status=422,
    )
    replaced = post(
        workspace, "/v1/admin/registry/import", {**source, "numbers": [], "request_id": str(uuid4())}
    )
    assert replaced["count"] == 0
    assert offered(workspace) == listed


def test_prepaid_screening_needs_a_key_and_seller_authorization_and_never_spends_on_opt_outs(workspace):
    post(workspace, "/v1/admin/dnc/screen", {"phones": [ET]}, status=409)
    settings(workspace, dnc_api_key="synthetic-dnc-key-not-used")
    post(workspace, "/v1/admin/dnc/screen", {"phones": [ET]}, status=403)
    post(workspace, "/v1/suppressions", {"phone": PT, "reason": "opt_out"})
    checked = post(workspace, "/v1/admin/dnc/screen", {"phones": [PT]})
    assert checked["authorization_required"] is True
    assert checked["results"][0]["internal_suppressed"] is True
    screens = get(workspace, "/v1/admin/dnc/screens")
    assert screens["screens"] == [] and screens["refresh"] is None and screens["limited"] is False
    post(
        workspace,
        "/v1/admin/dnc/screens/missing-screen/retry",
        {
            "expected_version": 1,
            "idempotency_key": "explicit-retry-1",
            "accept_possible_duplicate_charge": True,
        },
        status=404,
    )


def test_state_registry_required_by_policy_blocks_until_a_current_state_source_clears_the_lead(
    workspace, clock
):
    publish(workspace, state_registry_required=True)
    assert get(workspace, "/v1/admin/preflight")["screening"]["state_registry_required"] is True
    assert "STATE_REGISTRY_MISSING" in refused(workspace)
    import_leads(
        workspace,
        [
            {
                "name": "Riley Ridge",
                "phone": "+14155550177",
                "company": "Ridge Example",
                "domain": "ridge.example.test",
                "timezone": "America/Los_Angeles",
                "timezone_confidence": "confirmed",
                "region": "415",
                "jurisdiction": "CA",
            }
        ],
    )
    state = {
        "kind": "state",
        "jurisdiction": "CA",
        "subscription_reference": "own-state-list",
        "npas": ["415"],
    }
    state["refreshed_at"] = clock.now.isoformat()
    post(workspace, "/v1/admin/registry/import", {**state, "numbers": ["+14155550177"]})
    assert "STATE_REGISTRY_LISTED" in refused(workspace)
    post(workspace, "/v1/admin/registry/import", {**state, "numbers": []})
    assert offered(workspace) == "+14155550177"


def test_consent_evidence_is_required_by_policy_and_a_revocation_blocks_again(workspace, clock):
    publish(workspace, consent_required=True)
    assert "CONSENT_MISSING" in refused(workspace)
    consent = {
        "endpoint_id": endpoints(workspace)[PT],
        "permissions": ["live"],
        "revoked": False,
        "evidence_reference": "Signed opt-in form dated 2026-10-01",
        "idempotency_key": "consent-grant-1",
    }
    granted = post(workspace, "/v1/admin/consent", consent)
    assert granted["revoked"] is False and granted["permissions"] == ["live"]
    assert post(workspace, "/v1/admin/consent", consent)["id"] == granted["id"]
    post(workspace, "/v1/admin/consent", {**consent, "evidence_reference": "Different form"}, status=409)
    future = (clock.now + timedelta(days=1)).isoformat()
    post(
        workspace,
        "/v1/admin/consent",
        {**consent, "idempotency_key": "consent-future", "acquired_at": future},
        status=422,
    )
    assert offered(workspace) == PT
    revocation = {
        **consent,
        "revoked": True,
        "evidence_reference": "Lead asked to stop calls",
        "idempotency_key": "consent-revoke-1",
    }
    post(workspace, "/v1/admin/consent", revocation)
    assert "CONSENT_REVOKED" in refused(workspace)


def test_rnd_result_showing_reassignment_blocks_its_endpoint_and_no_result_does_not(workspace):
    suppress_all_but(workspace, keep=PT)
    endpoint = endpoints(workspace)[PT]
    rnd = {"endpoint_id": endpoint, "reference_date": "2026-09-01", "subscription_reference": "own-rnd-2026"}
    post(workspace, "/v1/admin/rnd/results", {**rnd, "result": "no"})
    assert offered(workspace) == PT
    post(workspace, "/v1/admin/rnd/results", {**rnd, "result": "yes", "provider_receipt": "rnd-receipt-1"})
    assert "INVALID_ENDPOINT" in refused(workspace)
    post(workspace, "/v1/admin/rnd/results", {**rnd, "result": "maybe"}, status=422)
    post(workspace, "/v1/admin/rnd/results", {**rnd, "subscription_reference": ""}, status=422)
    post(
        workspace,
        "/v1/admin/rnd/results",
        {**rnd, "endpoint_id": "missing-endpoint", "result": "no"},
        status=404,
    )


def test_a_lead_outside_its_local_window_is_not_offered_until_the_window_opens(workspace, clock):
    publish(workspace, local_start_hour=12, local_end_hour=13)
    assert "OUTSIDE_CALLING_WINDOW" in refused(workspace)  # 11:00 Pacific, 14:00 Eastern, 13:00 Central
    clock.now += timedelta(minutes=90)  # 12:30 Pacific, 15:30 Eastern, 14:30 Central
    assert offered(workspace) == PT


def test_operating_policy_reads_fixed_no_spend_terms_and_saves_versioned_edits(workspace):
    path = "/v1/admin/operating-policy"
    current = get(workspace, path)
    assert current["version"] == 1
    assert current["subscriptions_allowed"] is False and current["recording_enabled"] is False
    assert current["raw_retention_seconds"] == 300 and current["inference_data_collection"] == "deny"
    assert (
        current["dnc"]["prepaid_supported"] is True and current["external_reputation"]["available"] is False
    )
    assert "dnc" in current["costs"] and current["rates_are_estimates"] is True
    saved = save_operating_policy(
        workspace["client"], path, headers=workspace["headers"], json={"dnc_source_max_age_days": 14}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert get(workspace, path)["dnc_source_max_age_days"] == 14
    stale = workspace["client"].put(
        path,
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": 1, "dnc_source_max_age_days": 7},
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    for rejected in (
        {"recording_enabled": True},
        {
            "account_rates": {
                "dnc": {
                    "values": {"estimated_per_query_usd": "0"},
                    "source_reference": "Invoice",
                    "checked_at": "2026-10-14",
                }
            }
        },
        {"dnc_refresh": {"enabled": True}},
    ):
        assert (
            save_operating_policy(
                workspace["client"], path, headers=workspace["headers"], json=rejected
            ).status_code
            == 422
        )
    rate = {
        "values": {"estimated_per_query_usd": "0.004"},
        "source_reference": "Prepaid invoice",
        "checked_at": "2026-10-14",
    }
    assert (
        save_operating_policy(
            workspace["client"], path, headers=workspace["headers"], json={"account_rates": {"dnc": rate}}
        ).status_code
        == 200
    )
    assert (
        float(
            get(workspace, path)["costs"]["dnc"]["account_rate_override"]["values"]["estimated_per_query_usd"]
        )
        == 0.004
    )


def test_policy_publish_validates_the_window_and_refuses_a_stale_editor(workspace):
    current = get(workspace, "/v1/workspace")["policy"]
    path = f"/v1/admin/policies/{current['id']}/publish"
    post(
        workspace, path, {"local_start_hour": 12, "local_end_hour": 12, "reason": "Empty window"}, status=422
    )
    post(workspace, path, {"registry_required": True, "reason": "Not a setting"}, status=422)
    published = post(
        workspace, path, {"max_attempts": 4, "reason": "Reviewed", "expected_version": current["version"]}
    )
    assert get(workspace, "/v1/workspace")["policy"]["id"] == published["id"]
    stale = post(workspace, path, {"max_attempts": 2, "reason": "Second editor"}, status=409)
    assert "POLICY_VERSION_CONFLICT" in str(stale)


def test_seller_approval_and_preflight_gate_the_queue_and_report_the_blocker(workspace):
    check = get(workspace, "/v1/admin/preflight")
    assert check["integrity_ok"] is True and check["integrity_check"] == "quick"
    assert "SELLER_NOT_APPROVED" not in check["blockers"] and "POLICY_NOT_PUBLISHED" not in check["blockers"]
    assert get(workspace, "/v1/admin/preflight?deep=true")["integrity_check"] == "full"
    post(workspace, "/v1/admin/seller/approve", {}, status=422)
    post(
        workspace,
        "/v1/admin/seller/approve",
        {"identity_reference": "Identity record 2026-10-01", "approved": False},
    )
    blocked = get(workspace, "/v1/admin/preflight")
    assert "SELLER_NOT_APPROVED" in blocked["blockers"] and blocked["ready_to_call"] is False
    assert "SELLER_NOT_APPROVED" in refused(workspace)
    post(workspace, "/v1/admin/seller/approve", {"identity_reference": "Identity record 2026-10-01"})
    assert offered(workspace) in PHONES
    capabilities = {item["id"]: item for item in get(workspace, "/v1/admin/preflight")["capabilities"]}
    assert capabilities["telephony"]["state"] == "software_verified"
    assert capabilities["national_dnc"]["state"] == "software_verified"


def test_scheduled_prepaid_refresh_runs_once_per_window_within_its_budgets(
    workspace, app, monkeypatch, clock
):
    import httpx

    ws, service = workspace, app.state.service
    lookups = []

    def provider(request):
        phone = request.url.params["phone"]
        lookups.append((phone, request.headers["x-api-key"]))
        listed = len(lookups) == 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "phone": phone,
                "is_dnc": listed,
                "status": "DNC" if listed else "NOT DNC",
                "credits_remaining": 1000 - len(lookups),
                "timestamp": clock.now.isoformat().replace("+00:00", "Z"),
            },
        )

    monkeypatch.setattr(service, "http", httpx.AsyncClient(transport=httpx.MockTransport(provider)))

    def tick():
        ws["client"].portal.call(service.maintenance_tick)

    # Area code 650 has no national snapshot in the seeded workspace, so its numbers need a check;
    # seeded 415/212/512 numbers already have a current national result and are skipped.
    authorization = {
        "reference": "Seller registry access 2026-10",
        "npas": ["650"],
        "expires_at": (clock.now + timedelta(days=30)).isoformat(),
        "source_access_verified": True,
        "source_refreshed_at": clock.now.isoformat(),
    }
    settings(ws, dnc_api_key="synthetic-dnc-key", dnc_seller_authorization=authorization)
    eligible = ["+16505550201", "+16505550202", "+16505550204"]
    opted_out = "+16505550203"
    rows = [
        {
            "name": f"Refresh {i}",
            "phone": phone,
            "company": "Refresh Co",
            "region": "650",
            "timezone": "America/Los_Angeles",
            "timezone_confidence": "confirmed",
        }
        for i, phone in enumerate([*eligible, opted_out])
    ]
    import_leads(ws, rows)
    post(ws, "/v1/suppressions", {"phone": opted_out, "reason": "opt_out"})
    tick()
    run_jobs(ws)
    assert get(ws, "/v1/admin/dnc/screens")["refresh"] is None, "Off by default: no job, no spend"
    assert lookups == []
    # Two queries fit the run budget (1667 micro-USD each); the third does not.
    refresh = {
        "enabled": True,
        "interval_hours": 24,
        "batch_size": 50,
        "max_run_usd": "0.0034",
        "max_day_usd": "0.01",
    }
    path = "/v1/admin/operating-policy"
    saved = save_operating_policy(ws["client"], path, headers=ws["headers"], json={"dnc_refresh": refresh})
    assert saved.status_code == 200, saved.text
    tick()
    tick()  # The same window schedules one job.
    run_jobs(ws, rounds=3)
    screens = get(ws, "/v1/admin/dnc/screens")
    progress = screens["refresh"]
    assert progress["reason"] == "DNC_REFRESH_BUDGET_EXHAUSTED" and progress["complete"] is True
    assert progress["submitted"] == 2 and len(lookups) == 2
    assert all(key == "synthetic-dnc-key" for _, key in lookups)
    assert {phone for phone, _ in lookups} < {phone[2:] for phone in eligible}, "Never an opt-out"
    completed = [s for s in screens["screens"] if s["status"] == "completed"]
    assert sorted(s["listed"] for s in completed) == [False, True]
    assert all(s["checked_at"] and s["expires_at"] for s in completed)
    tick()
    run_jobs(ws)
    assert len(lookups) == 2, "The window's job has finished; nothing is charged again"
