"""Numbers, costs and admin over HTTP: inventory, routes, health, planning, billing, settings, backup and webhooks."""

import base64
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
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
    status, body = queue(ws)
    assert status == 409 and body["detail"]["code"] == "NO_ELIGIBLE_LEADS", body
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


def test_inventory_sync_adds_flags_and_marks_missing_numbers(workspace, carrier):
    carrier.numbers = {
        "carrier-new": {
            "id": "carrier-new",
            "phone_number": "+15125550199",
            "connection_id": "conn-1",
            "status": "active",
        }
    }
    added = post(workspace, "/v1/admin/numbers/sync")
    assert added["added"] == 1 and added["missing"] == 0 and added["provider_count"] == 1
    carrier.numbers["carrier-415"] = {
        "id": "carrier-415",
        "phone_number": "+14155550100",
        "connection_id": "conn-1",
        "status": "active",
    }
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


def test_owned_number_is_added_then_configured_on_the_carrier_route(workspace, carrier):
    added = post(workspace, "/v1/admin/numbers", {"phone": "+16505550123", "region": "650"})
    assert added["lifecycle"] == "purchased" and added["identity_configured"] is False
    post(workspace, "/v1/admin/numbers", {"phone": "+16505550123"}, status=409)
    post(workspace, "/v1/admin/numbers", {"phone": "not-a-number"}, status=422)
    post(workspace, f"/v1/admin/numbers/{added['id']}/configure", status=409)
    put_settings(workspace, telnyx_connection_id="conn-sim-route")
    carrier.numbers = {
        "carrier-650": {
            "id": "carrier-650",
            "phone_number": "+16505550123",
            "connection_id": None,
            "status": "active",
        }
    }
    post(workspace, "/v1/admin/numbers/sync")
    post(workspace, f"/v1/admin/numbers/{numbers(workspace)['+16505550123']['id']}/configure")
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
    assert "NUMBER_UNHEALTHY" in refused(workspace)
    rep = post(
        workspace,
        "/v1/admin/users",
        {"email": "rep-staffing@example.test", "name": "Riley Rep", "role": "rep", "password": PASSWORD},
    )
    window = {
        "start_at": (clock.now - timedelta(hours=1)).isoformat(),
        "end_at": (clock.now + timedelta(hours=8)).isoformat(),
        "reps": 1,
        "rep_ids": [rep["id"]],
    }
    call(workspace, "PUT", "/v1/admin/optimization", json={"staffed_windows": [window]})
    number = numbers(workspace)["+14155550100"]
    resumed = post(
        workspace,
        f"/v1/admin/numbers/{number['id']}/review",
        {
            "request_id": str(uuid4()),
            "expected_version": number["version"],
            "decision": "resume",
            "review_reference": "Maintenance cleared by operator",
            "daily_limit": 1,
        },
    )
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
    review = get(workspace, f"/v1/admin/numbers/{numbers(workspace)['+14155550100']['id']}/review")
    assert review["health"]["status"] == "investigating" and review["number"]["outbound_ready"] is False
    assert {"workload", "policy", "callback_candidates"} <= set(review)
    assert review["workload"]["current_limit"] == numbers(workspace)["+14155550100"]["daily_limit"]
    assert "NUMBER_UNHEALTHY" in refused(workspace)
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


def test_reassignment_moves_an_existing_lead_identity_to_a_verified_number(workspace):
    packet = reserve(workspace).json()
    post(workspace, "/v1/rep/presence", {"status": "paused"})
    lead, old_number = packet["lead"]["id"], packet["number"]["id"]
    new_number = next(
        n for n in numbers(workspace).values() if n["id"] != old_number and n["phone"].startswith("+1")
    )
    binding = get(workspace, f"/v1/admin/numbers/{new_number['id']}/review", params={"lead_id": lead})[
        "binding"
    ]
    assert binding["number_id"] == old_number
    moved = call(
        workspace,
        "PATCH",
        f"/v1/admin/numbers/{new_number['id']}",
        json={
            "reassignment": {
                "lead_id": lead,
                "old_number_id": old_number,
                "reason": "Carrier moved the line; new route verified",
            },
            "request_id": str(uuid4()),
            "expected_version": binding["version"],
        },
    )
    assert moved["reassignment"]["new_number_id"] == new_number["id"]
    current = get(workspace, f"/v1/admin/numbers/{new_number['id']}/review", params={"lead_id": lead})[
        "binding"
    ]
    assert current["number_id"] == new_number["id"]


def test_number_search_quotes_and_purchases_stay_blocked(workspace, carrier):
    found = post(workspace, "/v1/admin/numbers/search", {"region": "415"})
    quote = found["numbers"][0]
    assert (
        quote["phone"] == "+14155550188" and quote["currency"] == "USD" and quote["monthly_cost_usd"] == 1.0
    )
    assert found["simulation"] is True
    post(workspace, "/v1/admin/numbers/search", {"region": "123"}, status=422)
    refusal = post(
        workspace, "/v1/admin/numbers/purchase", {"region": "415", "phone": quote["phone"]}, status=403
    )
    assert "RECURRING_COMMITMENT_DISABLED" in refusal["detail"]
    assert carrier.purchases == []
    assert get(workspace, "/v1/admin/numbers/orders") == {"orders": []}


def test_capacity_proposal_is_advisory_and_follows_the_declared_staffing(workspace, clock):
    campaign = workspace["campaign_id"]
    first = post(workspace, "/v1/admin/numbers/proposal", {"campaign_id": campaign})
    assert first["projection"]["status"] == "pending"
    run_jobs(workspace)
    plan = post(workspace, "/v1/admin/numbers/proposal", {"campaign_id": campaign})
    assert plan["projection"]["status"] == "ready"
    assert (
        plan["purchased"] is False and plan["purchase_allowed"] is False and plan["approval_required"] is True
    )
    assert plan["expected_conversion_lift"] is None
    assert plan["schedule_scope"] == "normalized_simple_windows"
    assert plan["assumptions"]["staffed_time_source"] == "simple_schedule_rep_minutes"
    rep = post(
        workspace,
        "/v1/admin/users",
        {"email": "planner-rep@example.test", "name": "Planner Rep", "role": "rep", "password": PASSWORD},
    )
    window = {
        "start_at": (clock.now - timedelta(hours=1)).isoformat(),
        "end_at": (clock.now + timedelta(hours=8)).isoformat(),
        "reps": 1,
        "rep_ids": [rep["id"]],
    }
    call(workspace, "PUT", "/v1/admin/optimization", json={"staffed_windows": [window]})
    post(workspace, "/v1/admin/numbers/proposal", {"campaign_id": campaign})
    run_jobs(workspace)
    named = post(workspace, "/v1/admin/numbers/proposal", {"campaign_id": campaign})
    assert named["projection"]["status"] == "ready"
    assert named["schedule_scope"] == "declared_staffed_windows"
    assert named["assumptions"]["staffed_time_source"] == "declared_windows"
    assert named["assumptions"]["retry_demand"]
    assert set(named["contingent_retry_low_mean_high"]) >= {"low", "mean", "high"}
    assert (named["existing_only_capacity"], named["forecast_attempts"]) == (2, 3)
    assert named["minimum_additional_numbers"] == 1 and named["capacity_shortfall"] == 1
    assert named["shortfall_proven"] is True and named["optimality"] == "proven_small_case"
    review = named["expansion_review"]
    assert (review["status"], review["reason"], review["purchase_allowed"]) == (
        "eligible",
        "list_or_staffing_changed",
        False,
    )
    post(workspace, "/v1/suppressions", {"phone": ET, "reason": "opt_out"})
    post(workspace, "/v1/suppressions", {"phone": CT, "reason": "opt_out"})
    post(workspace, "/v1/admin/numbers/proposal", {"campaign_id": campaign})
    run_jobs(workspace)
    sufficient = post(workspace, "/v1/admin/numbers/proposal", {"campaign_id": campaign})
    assert sufficient["forecast_attempts"] == 1 and sufficient["minimum_additional_numbers"] == 0
    assert sufficient["expansion_review"]["status"] == "sufficient"


def test_call_cost_is_estimated_then_settled_by_carrier_records(workspace, clock):
    row = start(workspace)
    hold = call_entry(workspace, row["id"])
    assert hold["reserved_usd"] == 0.05
    answered_by_human(workspace, row["id"])
    clock.now += timedelta(minutes=3)
    hang_up(workspace, row["id"])
    wrap_up(workspace, row["id"], outcome="not_interested", contact_role="right_party")
    estimate = call_entry(workspace, row["id"])
    assert estimate["status"] == "estimated" and estimate["estimate_source"] == "call_duration_register_rates"
    assert estimate["net_cost_usd"] > 0 and estimate["billing_verified"] is False
    statement = {
        "source_reference": "Synthetic carrier statement 2026-10",
        "rows": [
            {
                "call_id": row["id"],
                "provider_record_id": "cdr-1",
                "leg_id": "prospect-leg",
                "kind": "voice",
                "duration_seconds": 180,
                "cost_usd": 0.02,
            }
        ],
    }
    imported = post(workspace, "/v1/admin/billing/reconcile", statement)
    assert imported["matched"] == 1 and imported["duplicates"] == 0 and imported["invoice_verified"] is False
    assert post(workspace, "/v1/admin/billing/reconcile", statement)["duplicates"] == 1
    estimate = call_entry(workspace, row["id"])
    record = next(e for e in ledger_rows(workspace) if e["kind"] == "voice" and e["object_id"] == row["id"])
    assert record["status"] == "settled" and record["cost_usd"] == 0.02
    closed = post(
        workspace,
        f"/v1/admin/billing/ledger/{estimate['id']}/reconcile",
        {
            "expected_version": estimate["version"],
            "action": "close_call",
            "provider": estimate["provider"],
            "receipt_id": "statement-close-1",
            "source_reference": "Synthetic carrier statement 2026-10",
            "cost_usd": 0,
            "attest_original": True,
            "provider_record_ids": ["cdr-1"],
            "confirm_complete": True,
        },
    )
    assert closed["entry"]["status"] == "settled" and closed["entry"]["billing_verified"] is True
    credit = {
        "action": "credit",
        "provider": "telnyx",
        "receipt_id": "credit-note-1",
        "source_reference": "Carrier credit 2026-10",
        "cost_usd": 0.01,
        "attest_original": True,
    }
    credited = post(
        workspace,
        f"/v1/admin/billing/ledger/{record['id']}/reconcile",
        {**credit, "expected_version": record["version"]},
    )
    assert credited["entry"]["net_cost_usd"] == 0.01
    current = next(e for e in ledger_rows(workspace) if e["id"] == record["id"])
    over = {**credit, "receipt_id": "credit-note-2", "cost_usd": 0.05, "expected_version": current["version"]}
    post(workspace, f"/v1/admin/billing/ledger/{record['id']}/reconcile", over, status=409)


def test_exhausted_budget_blocks_metered_work_until_the_budget_is_raised(workspace):
    budget = call(workspace, "PUT", "/v1/admin/budget", json={"daily_limit_usd": 0, "monthly_limit_usd": 0})
    assert budget["daily_limit_usd"] == 0
    assert "BUDGET_EXHAUSTED" in refused(workspace)
    assert "BUDGET_EXHAUSTED" in get(workspace, "/v1/admin/preflight")["blockers"]
    call(
        workspace,
        "PUT",
        "/v1/admin/budget",
        json={"daily_limit_usd": -1, "monthly_limit_usd": 10},
        status=422,
    )
    raised = call(
        workspace, "PUT", "/v1/admin/budget", json={"daily_limit_usd": 25, "monthly_limit_usd": 250}
    )
    assert raised["monthly_limit_usd"] == 250
    assert get(workspace, "/v1/admin/settings")["budget"]["daily_limit_usd"] == 25
    assert offered(workspace) in (PT, ET, CT)


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


def test_backup_and_restore_keep_business_data_and_hold_outbound_until_reconciled(workspace):
    leads = sorted(lead["phone"] for lead in get(workspace, "/v1/workspace/leads")["items"])
    backup = post(workspace, "/v1/admin/backup")
    assert len(backup["checksum"]) == 64
    post(workspace, "/v1/admin/numbers", {"phone": "+16505550123", "region": "650"})
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


def test_ingress_check_refuses_unconfigured_and_off_route_targets_before_any_fetch(workspace):
    body = {"request_id": str(uuid4()), "expected_version": 0}
    assert "INGRESS_NOT_CONFIGURED" in str(post(workspace, "/v1/admin/ingress-check", body, status=409))
    put_settings(workspace, webhook_url="https://ingress.example.test/not-the-webhook")
    post(workspace, "/v1/admin/ingress-check", {**body, "expected_version": 1}, status=422)
    assert workspace["client"].post("/v1/admin/ingress-check", json=body).status_code in (401, 403)
    assert workspace["client"].get("/webhooks/ingress-check/" + "0" * 32).status_code == 404


def test_telnyx_webhook_accepts_only_signed_events_and_redelivery_is_idempotent(workspace, clock):
    client = workspace["client"]
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    put_settings(workspace, telnyx_public_key=public)

    def sign(stamp, payload):
        return base64.b64encode(key.sign(stamp.encode() + b"|" + payload)).decode()

    def deliver(payload, signature, stamp):
        headers = {
            "telnyx-signature-ed25519": signature,
            "telnyx-timestamp": stamp,
            "content-type": "application/json",
        }
        return client.post("/webhooks/telnyx", content=payload, headers=headers)

    body = json.dumps(
        {
            "data": {
                "id": "evt-acceptance-1",
                "event_type": "call.answered",
                "payload": {"call_control_id": "unknown-call"},
            }
        }
    ).encode()
    stamp = str(int(clock.now.timestamp()))
    assert client.post("/webhooks/telnyx", json={"data": {"id": "forged"}}).status_code == 401
    accepted = deliver(body, sign(stamp, body), stamp)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["received"] is True
    receipt = accepted.json()["receipt_id"]
    assert deliver(body, sign(stamp, body), stamp).json()["receipt_id"] == receipt
    assert deliver(body + b" ", sign(stamp, body), stamp).status_code == 401
    old = str(int(clock.now.timestamp()) - 600)
    assert deliver(body, sign(old, body), old).status_code == 401
    missing = json.dumps({"data": {"event_type": "call.answered", "payload": {"a": 1}}}).encode()
    assert deliver(missing, sign(stamp, missing), stamp).status_code == 422
    run_jobs(workspace)
    assert deliver(body, sign(stamp, body), stamp).json()["receipt_id"] == receipt


def test_call_recording_is_never_served_even_for_an_owned_call(workspace):
    row = start(workspace)
    response = workspace["client"].get(f"/v1/calls/{row['id']}/recording", headers=workspace["headers"])
    assert response.status_code == 410 and "retention" in response.text
    get(workspace, "/v1/calls/no-such-call/recording", status=404)


HUMAN_LABELS = {"right_party": True, "not_interested": True, "no_answer": False, "busy": False}


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
    for _ in range(4):
        if get(workspace, "/v1/metrics")["projection"]["status"] == "ready":
            break
        run_jobs(workspace)
    post(workspace, "/v1/admin/optimization/refresh")
    run_jobs(workspace, rounds=2)
    health = get(workspace, f"/v1/admin/numbers/{number_id}/review")["health"]
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


def test_large_proposal_takes_the_greedy_path_and_respects_its_lower_bound(workspace, clock):
    import_leads(
        workspace,
        [
            {
                "name": f"Planner Lead {i}",
                "phone": f"+1415555{3100 + i:04d}",
                "company": "Planner Example",
                "domain": f"planner{i}.example.test",
                "timezone": "America/Los_Angeles",
                "timezone_confidence": "confirmed",
                "region": "415",
            }
            for i in range(30)
        ],
    )
    campaign = workspace["campaign_id"]
    body = {"campaign_id": campaign, "daily_capacity_per_number": 1}
    post(workspace, "/v1/admin/numbers/proposal", body)
    run_jobs(workspace)
    plan = post(workspace, "/v1/admin/numbers/proposal", body)
    assert plan["projection"]["status"] == "ready"
    assert PLANNER_CONTRACT <= set(plan), PLANNER_CONTRACT - set(plan)
    assert plan["forecast_attempts"] > 8, "Above eight work items the exact search is not used"
    assert (
        plan["purchased"] is False and plan["purchase_allowed"] is False and plan["approval_required"] is True
    )
    assert plan["expected_conversion_lift"] is None
    lower, best = plan["lower_bound"], plan["best_found_additional_numbers"]
    assert isinstance(lower, int) and lower >= 0
    assert plan["proposed_additional_numbers"] >= lower and best >= lower
    assert plan["optimality_gap"] == best - lower
    assert plan["minimum_additional_numbers"] in (None, best)
    retry = plan["contingent_retry_low_mean_high"]
    assert set(retry) >= {"low", "mean", "high", "support", "omitted_leads"}
    assert retry["low"] <= retry["mean"] <= retry["high"]
    scenarios = plan["contingent_capacity"]
    assert scenarios["status"] == "ready"
    low, mean, high = scenarios["low"], scenarios["mean"], scenarios["high"]
    assert low["planned_attempts"] == plan["forecast_attempts"] and low["retry_attempts"] == 0
    assert low["additional_numbers"] == plan["proposed_additional_numbers"]
    assert 0 < mean["retry_attempts"] <= high["retry_attempts"]
    assert low["planned_attempts"] <= mean["planned_attempts"] <= high["planned_attempts"]
