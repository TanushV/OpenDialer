"""Compliance over HTTP: opt-outs, registries, consent, holds, calling windows, policy, approval and preflight."""

from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import save_operating_policy
from journey import get, import_leads, post, reserve

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


def test_prepaid_screening_needs_a_key_and_seller_authorization_and_never_spends_on_opt_outs(workspace):
    post(workspace, "/v1/admin/dnc/screen", {"phones": [ET]}, status=409)
    settings(workspace, dnc_api_key="synthetic-dnc-key-not-used")
    post(workspace, "/v1/admin/dnc/screen", {"phones": [ET]}, status=403)
    post(workspace, "/v1/suppressions", {"phone": PT, "reason": "opt_out"})
    checked = post(workspace, "/v1/admin/dnc/screen", {"phones": [PT]})
    assert checked["authorization_required"] is True
    assert checked["results"][0]["internal_suppressed"] is True
    screens = get(workspace, "/v1/admin/dnc/screens")
    assert screens["screens"] == [] and "refresh" not in screens and screens["limited"] is False
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
    # R3.6: one writer, so a save from an older read is applied, not refused.
    assert stale.status_code == 200 and get(workspace, path)["dnc_source_max_age_days"] == 7
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


@pytest.mark.slow
def test_calling_needs_no_approval_paperwork_and_screening_is_optional(workspace, clock):
    check = get(workspace, "/v1/admin/preflight")
    assert check["integrity_ok"] is True and check["integrity_check"] == "quick"
    assert get(workspace, "/v1/admin/preflight?deep=true")["integrity_check"] == "full"
    assert check["screening"]["enabled"] is False
    for code in ("SELLER_NOT_APPROVED", "POLICY_NOT_PUBLISHED", "CARRIER_TRAFFIC_APPROVAL_MISSING"):
        assert code not in check["blockers"]
    # A fresh list with no registry coverage and no time zone column is callable when screening is off.
    for phone in PHONES:
        post(workspace, "/v1/suppressions", {"phone": phone, "reason": "opt_out"})
    import_leads(workspace, [{"name": "Avery Plain", "phone": "+14155550188", "company": "Plain Example"}])
    assert offered(workspace) == "+14155550188"
    publish(workspace, dnc_screening=True)
    assert get(workspace, "/v1/admin/preflight")["screening"]["enabled"] is True
    # Turning screening off stops do-not-call checks; the internal opt-out list always applies.
    publish(workspace, dnc_screening=False)
    assert offered(workspace) == "+14155550188"
    capabilities = {item["id"]: item for item in get(workspace, "/v1/admin/preflight")["capabilities"]}
    assert capabilities["telephony"]["state"] == "software_verified"
    assert capabilities["national_dnc"]["blocks"] == "none"


def test_a_lead_without_a_time_zone_uses_every_zone_its_state_and_area_code_allow(workspace, clock):
    for phone in PHONES:
        post(workspace, "/v1/suppressions", {"phone": phone, "reason": "opt_out"})
    # A New York area code with a California address: calls only when both are inside 8:00-17:00.
    import_leads(workspace, [{"name": "Rowan Moved", "phone": "+12125550189", "state": "CA"}])
    assert offered(workspace) == "+12125550189"  # 11:00 Los Angeles, 14:00 New York
    clock.now += timedelta(hours=7, minutes=30)  # 18:30 Los Angeles, 21:30 New York
    assert "OUTSIDE_CALLING_WINDOW" in refused(workspace)
