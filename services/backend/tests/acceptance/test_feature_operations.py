"""Numbers, costs and admin over HTTP: inventory, routes, health, planning, billing, settings, backup and webhooks."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from journey import (
    answered_by_human,
    call,
    get,
    hang_up,
    import_leads,
    post,
    reserve,
    run_jobs,
    start,
    wrap_up,
)

PT, ET, CT = "+14155550101", "+12125550102", "+15125550103"
PASSWORD = "a-test-only-password-456"


def queue(ws):
    response = reserve(ws)
    post(ws, "/v1/rep/presence", {"status": "paused"})
    return response.status_code, response.json()


def offered(ws):
    status, body = queue(ws)
    assert status == 200, body
    return body["lead"]["phone"]


def refused(ws):
    """Why Next call was refused: the blocker code, or the queue's reasons when leads ran out."""
    status, body = queue(ws)
    assert status == 409, body
    if body["detail"]["code"] != "NO_ELIGIBLE_LEADS":
        return {body["detail"]["code"]}
    return set(body["detail"]["reasons"])


def numbers(ws):
    return {n["phone"]: n for n in get(ws, "/v1/workspace/numbers")["items"]}


def put_settings(ws, status=200, **values):
    version = get(ws, "/v1/admin/settings")["version"]
    body = {"request_id": str(uuid4()), "expected_version": version, **values}
    response = ws["client"].put("/v1/admin/settings", headers=ws["headers"], json=body)
    assert response.status_code == status, response.text
    return response.json()


def ledger_rows(ws):
    return get(ws, "/v1/admin/billing/ledger", params={"status": "all"})["entries"]


def call_entry(ws, call_id):
    return next(e for e in ledger_rows(ws) if e["kind"] == "call_authorization" and e["object_id"] == call_id)


def test_inventory_sync_flags_changes_and_marks_missing_numbers(workspace, carrier):
    def owned(identity, phone, connection="simulated-connection"):
        return {
            "id": identity,
            "phone_number": phone,
            "connection_id": connection,
            "status": "active",
            "call_recording_enabled": False,
        }

    carrier.numbers = {"carrier-new": owned("carrier-new", "+15125550199")}
    assert post(workspace, "/v1/admin/numbers/sync")["provider_count"] == 1
    assert "+15125550199" not in numbers(workspace)
    post(workspace, "/v1/admin/numbers/add", {"phone": "+15125550199"})
    carrier.numbers["carrier-415"] = owned("carrier-415", "+14155550100")
    changed = post(workspace, "/v1/admin/numbers/sync")
    assert changed["updated"] == 1
    assert numbers(workspace)["+14155550100"]["lifecycle"] == "investigating"
    summary = get(workspace, "/v1/admin/numbers/summary")["inventory"]
    assert summary["total"] == 3 and summary["investigating"] == 1
    carrier.numbers.clear()
    assert post(workspace, "/v1/admin/numbers/sync")["missing"] == 2
    assert numbers(workspace)["+15125550199"]["lifecycle"] == "paused_for_outbound"
    assert get(workspace, "/v1/admin/numbers/summary")["inventory"]["paused_outbound"] == 2
    duplicate = {"phone_number": "+15125550199", "status": "active"}
    carrier.numbers = {"a": {"id": "a", **duplicate}, "b": {"id": "b", **duplicate}}
    post(workspace, "/v1/admin/numbers/sync", status=502)
    assert get(workspace, "/v1/admin/numbers/summary")["inventory"]["paused_outbound"] == 2


def test_adding_an_owned_number_routes_it_and_refuses_bad_input(workspace, carrier):
    put_settings(workspace, telnyx_connection_id="conn-sim-route")
    carrier.numbers = {
        "carrier-650": {
            "id": "carrier-650",
            "phone_number": "+16505550123",
            "connection_id": None,
            "status": "active",
        }
    }
    post(workspace, "/v1/admin/numbers/add", {"phone": "not-a-number"}, status=422)
    post(workspace, "/v1/admin/numbers/add", {"phone": "+16505550124"}, status=404)
    post(workspace, "/v1/admin/numbers/add", {"phone": "+16505550123"})
    row = numbers(workspace)["+16505550123"]
    assert row["identity_configured"] is True and row["configuration_status"] == "confirmed"
    assert carrier.numbers["carrier-650"]["connection_id"] == "conn-sim-route"


def test_paused_numbers_block_dialing_until_a_reviewed_resume(workspace, clock):
    board = numbers(workspace)
    for phone in ("+14155550100", "+12125550100"):
        patched = call(
            workspace,
            "PATCH",
            f"/v1/admin/numbers/{board[phone]['id']}",
            json={"lifecycle": "paused_for_outbound", "reason": "Carrier maintenance window"},
        )
        assert patched["lifecycle"] == "paused_for_outbound"
    assert get(workspace, "/v1/admin/numbers/summary")["inventory"]["ready"] == 0
    assert refused(workspace) & {"NUMBER_UNHEALTHY", "CALLER_NUMBER_PAUSED"}
    number = numbers(workspace)["+14155550100"]
    # R10.7: one click puts a paused number back to work once its route, test call and health allow it.
    resumed = post(workspace, f"/v1/admin/numbers/{number['id']}/resume")
    assert resumed["lifecycle"] == "limited_real_use"
    assert offered(workspace) in (PT, ET, CT)


def test_external_reputation_flags_numbers_until_cleared_and_review_shows_it(workspace, clock):
    now = clock.now
    ready_before = get(workspace, "/v1/admin/numbers/summary")["inventory"]["ready"]

    def reputation(label, observed):
        return {
            "source": "carrier-reputation-report",
            "ownership_reference": "own-report-2026-10",
            "observed_at": observed.isoformat(),
            "expires_at": (now + timedelta(days=7)).isoformat(),
            "spam_label": label,
        }

    def import_reputation(phone, value):
        number = numbers(workspace)[phone]
        body = {
            "external_reputation": value,
            "request_id": str(uuid4()),
            "expected_version": number["version"],
        }
        return call(workspace, "PATCH", f"/v1/admin/numbers/{number['id']}", json=body)

    for phone in ("+14155550100", "+12125550100"):
        import_reputation(phone, reputation("spam_likely", now - timedelta(hours=1)))
    assert get(workspace, "/v1/admin/numbers/summary")["inventory"]["ready"] == ready_before - 2
    review = get(workspace, f"/v1/admin/numbers/{numbers(workspace)['+14155550100']['id']}/health")
    assert review["health"]["status"] == "investigating" and review["number"]["outbound_ready"] is False
    flagged = post(
        workspace, f"/v1/admin/numbers/{numbers(workspace)['+14155550100']['id']}/resume", status=409
    )
    assert "flagged" in flagged["detail"]
    assert refused(workspace) & {"NUMBER_UNHEALTHY", "CALLER_NUMBER_PAUSED"}
    future = reputation("clear", now + timedelta(hours=1))
    call(
        workspace,
        "PATCH",
        f"/v1/admin/numbers/{numbers(workspace)['+14155550100']['id']}",
        json={
            "external_reputation": future,
            "request_id": str(uuid4()),
            "expected_version": numbers(workspace)["+14155550100"]["version"],
        },
        status=422,
    )
    for phone in ("+14155550100", "+12125550100"):
        import_reputation(phone, reputation("clear", now - timedelta(minutes=30)))
    assert get(workspace, "/v1/admin/numbers/summary")["inventory"]["ready"] == ready_before
    restored = get(workspace, f"/v1/admin/numbers/{numbers(workspace)['+14155550100']['id']}/health")
    assert restored["number"]["outbound_ready"] is True


def test_expired_label_evidence_requires_an_explicit_clear(workspace, clock):
    phone = "+14155550100"
    number = numbers(workspace)[phone]

    def save_label(label, status=200):
        current = numbers(workspace)[phone]
        return call(
            workspace,
            "PATCH",
            f"/v1/admin/numbers/{number['id']}",
            status=status,
            json={
                "request_id": str(uuid4()),
                "expected_version": current["version"],
                "external_reputation": {
                    "source": "carrier display",
                    "ownership_reference": "same number",
                    "observed_at": clock.now.isoformat(),
                    "expires_at": (clock.now + timedelta(minutes=1)).isoformat(),
                    "spam_label": label,
                },
            },
        )

    save_label("Spam Likely")
    clock.now += timedelta(minutes=2)
    assert not numbers(workspace)[phone]["outbound_ready"]
    save_label(None, status=422)
    save_label("Clear")
    assert numbers(workspace)[phone]["outbound_ready"]


def test_number_search_quotes_and_purchases_stay_blocked(workspace, carrier):
    found = post(workspace, "/v1/admin/numbers/search", {"region": "415"})
    quote = found["numbers"][0]
    assert (
        quote["phone"] == "+14155550188" and quote["currency"] == "USD" and quote["monthly_cost_usd"] == 1.0
    )
    assert found["simulation"] is True
    post(workspace, "/v1/admin/numbers/search", {"region": "123"}, status=422)
    assert carrier.purchases == []


def test_the_ai_budget_is_per_lead_list_and_never_blocks_calling(workspace):
    budget = call(workspace, "PUT", "/v1/admin/budget", json={"per_list_usd": 0})
    assert budget["per_list_usd"] == 0
    assert "BUDGET_EXHAUSTED" not in get(workspace, "/v1/admin/preflight")["blockers"]
    assert offered(workspace) in (PT, ET, CT)
    call(workspace, "PUT", "/v1/admin/budget", json={"per_list_usd": -1}, status=422)
    raised = call(workspace, "PUT", "/v1/admin/budget", json={"per_list_usd": 30})
    assert raised["per_list_usd"] == 30
    assert get(workspace, "/v1/admin/settings")["budget"]["per_list_usd"] == 30


def test_provider_keys_are_stored_but_never_echoed_back(workspace):
    secret = "synthetic-openrouter-secret-not-real"
    body = {
        "request_id": str(uuid4()),
        "expected_version": 0,
        "openrouter_api_key": secret,
        "telnyx_connection_id": "conn-sim-settings",
    }
    saved = workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body)
    assert saved.status_code == 200, saved.text
    assert secret not in saved.text
    assert (
        saved.json()["configured"]["openrouter_api_key"] is True and "openrouter_api_key" not in saved.json()
    )
    assert saved.json()["telnyx_connection_id"] == "conn-sim-settings" and saved.json()["version"] == 1
    replay = workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body)
    assert replay.status_code == 200 and replay.json()["version"] == 1
    conflict = workspace["client"].put(
        "/v1/admin/settings",
        headers=workspace["headers"],
        json={**body, "openrouter_api_key": "other-secret"},
    )
    assert conflict.status_code == 409
    stale = {"request_id": str(uuid4()), "expected_version": 0, "coach_provider": "openrouter"}
    assert (
        workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=stale).status_code
        == 409
    )
    assert (
        workspace["client"]
        .put("/v1/admin/settings", headers=workspace["headers"], json={"expected_version": 1})
        .status_code
        == 422
    )
    fetched = get(workspace, "/v1/admin/settings")
    assert fetched["configured"]["openrouter_api_key"] is True and secret not in json.dumps(fetched)
    put_settings(workspace, status=422, webhook_url="https://user:pass@ingress.example.test/webhooks/telnyx")
    put_settings(workspace, status=422, webhook_url="http://ingress.example.test/webhooks/telnyx")


def test_backup_and_restore_keep_business_data_and_hold_outbound_until_reconciled(workspace, carrier):
    leads = sorted(lead["phone"] for lead in get(workspace, "/v1/workspace/leads")["items"])
    backup = post(workspace, "/v1/admin/backup")
    assert len(backup["checksum"]) == 64
    carrier.numbers = {
        "n650": {"id": "n650", "phone_number": "+16505550123", "connection_id": None, "status": "active"}
    }
    post(workspace, "/v1/admin/numbers/add", {"phone": "+16505550123"})
    post(workspace, "/v1/suppressions", {"phone": PT, "reason": "opt_out"})
    restored = post(workspace, "/v1/admin/restore", {"backup_id": backup["backup_id"]})
    assert restored["restored"] is True and restored["outbound_paused"] is True
    items = get(workspace, "/v1/workspace/leads")["items"]
    assert sorted(lead["phone"] for lead in items) == leads
    assert next(lead for lead in items if lead["phone"] == PT)["suppressed"] is True
    assert "+16505550123" in numbers(workspace)
    assert "RESTORE_RECONCILIATION_REQUIRED" in get(workspace, "/v1/admin/preflight")["blockers"]
    assert "RESTORE_RECONCILIATION_REQUIRED" in refused(workspace)
    post(workspace, "/v1/admin/restore/reconcile", {}, status=422)
    post(
        workspace,
        "/v1/admin/restore/reconcile",
        {"review_reference": "Reviewed synthetic empty carrier state"},
    )
    assert "RESTORE_RECONCILIATION_REQUIRED" not in get(workspace, "/v1/admin/preflight")["blockers"]
    assert offered(workspace) in (ET, CT)
    post(workspace, "/v1/admin/restore", {"backup_id": str(uuid4())}, status=404)
    post(workspace, "/v1/admin/restore", {"backup_id": "../../etc"}, status=422)


def test_relay_redelivery_of_a_carrier_event_is_idempotent(workspace, clock):
    """Signatures are checked at the relay's edge; the app dedupes what the relay hands over (R4.7)."""
    from functools import partial

    from app.managed_webhooks import deliver

    client = workspace["client"]
    event = {
        "id": "evt-acceptance-1",
        "event_type": "call.answered",
        "payload": {"call_control_id": "unknown-call"},
    }
    row = {
        "event": event,
        "hash": "hash-1",
        "received_at": clock.now.isoformat(),
        "account_id": "acct",
        "event_id": event["id"],
    }
    service, tenant = client.app.state.service, workspace["tenant_id"]
    first = client.portal.call(partial(deliver, service, tenant, [row]))
    again = client.portal.call(partial(deliver, service, tenant, [row]))
    assert first == again == [{"account_id": "acct", "event_id": "evt-acceptance-1"}]
    receipts = client.portal.call(partial(service.accept_webhook, tenant, event, "hash-1", verified=True))
    assert receipts["received"] is True


HUMAN_LABELS = {"right_party": True, "not_interested": True, "no_answer": False, "busy": False}


@pytest.mark.slow
def test_observed_number_health_reports_delivery_pickup_and_case_mix_from_real_calls(workspace, clock):
    health_policy = get(workspace, "/v1/admin/settings")["optimization"]["health_policy"]
    minimum = health_policy["min_comparable"]
    board = numbers(workspace)
    call(
        workspace,
        "PATCH",
        f"/v1/admin/numbers/{board['+12125550100']['id']}",
        json={"lifecycle": "paused_for_outbound", "reason": "Route acceptance calls through one number"},
    )
    cycle = ["right_party", "not_interested", "no_answer", "busy"]
    outcomes = [cycle[i % 4] for i in range(minimum)] + ["temporary_failure", "temporary_failure"]
    import_leads(
        workspace,
        [
            {
                "name": f"Health Lead {i}",
                "phone": f"+1415555{2500 + i:04d}",
                "company": "Health Example",
                "domain": f"health{i}.example.test",
                "timezone": "America/Los_Angeles",
                "timezone_confidence": "confirmed",
                "region": "415",
            }
            for i in range(len(outcomes))
        ],
    )
    number_id = board["+14155550100"]["id"]
    for index, outcome in enumerate(outcomes):
        placed = start(workspace, key=f"health-call-{index}")
        assert placed["number_id"] == number_id
        if outcome in {"right_party", "not_interested"}:
            answered_by_human(workspace, placed["id"])
            clock.now += timedelta(minutes=2)
            hang_up(workspace, placed["id"])
            wrap_up(workspace, placed["id"], outcome=outcome, contact_role="right_party")
        else:
            clock.now += timedelta(seconds=40)
            hang_up(workspace, placed["id"])
            wrap_up(workspace, placed["id"], outcome=outcome)
        clock.now += timedelta(seconds=20)
    post(workspace, "/v1/admin/optimization/refresh")
    run_jobs(workspace, rounds=2)
    health = get(workspace, f"/v1/admin/numbers/{number_id}/health")["health"]
    answers = sum(HUMAN_LABELS.get(o) is True for o in outcomes)
    classified = sum(o in HUMAN_LABELS for o in outcomes)
    failures = outcomes.count("temporary_failure")
    assert health["samples"] == len(outcomes)
    assert health["human_classified_samples"] == classified and health["human_answers"] == answers
    assert health["pickup_rate"] == pytest.approx(answers / classified)
    assert health["pickup_interval"][0] <= answers / classified <= health["pickup_interval"][1]
    assert health["delivery_samples"] == len(outcomes) and health["delivery_failures"] == failures
    assert health["delivery_success_rate"] == pytest.approx((len(outcomes) - failures) / len(outcomes))
    assert (
        health["delivery_failure_interval"][0]
        <= failures / len(outcomes)
        <= health["delivery_failure_interval"][1]
    )
    assert health["comparable_samples"] == classified and health["excluded_comparison_samples"] == failures
    assert health["case_mix_interval"] is not None
    assert health["case_mix_residual_mean"] == pytest.approx(
        (answers - health["expected_human_answers"]) / classified
    )
    assert health["status"] == "observed" and health["score"] == pytest.approx(
        (len(outcomes) - failures) / len(outcomes)
    )
    assert health["confidence_measure"] == "observed_delivery_success"
    assert health["comparison_method"] == "frozen_pre_call_residual_approximate_normal_interval"


PLANNER_CONTRACT = {
    "lower_bound",
    "optimality",
    "optimality_gap",
    "best_found_additional_numbers",
    "minimum_additional_numbers",
    "proposed_additional_numbers",
    "existing_only_capacity",
    "existing_plus_addition_capacity",
    "forecast_attempts",
    "capacity_shortfall",
    "shortfall_proven",
    "planning_limited",
    "search_complete",
    "contingent_retry_low_mean_high",
    "contingent_capacity",
    "expansion_review",
    "purchased",
    "purchase_allowed",
    "approval_required",
    "expected_conversion_lift",
}
