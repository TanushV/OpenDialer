"""October 16 acceptance contract, committed before optimization implementation."""

from datetime import timedelta

import pytest
from app.optimization.numbering import capacity_forecast, number_health, select_number


def number(identifier="n1", **overrides):
    return {
        "id": identifier,
        "region": "415",
        "lifecycle": "healthy",
        "daily_limit": 5,
        "used_today": 0,
        "active_obligations": 0,
        "capacity": 1,
        "identity_configured": True,
        "callback_tested": True,
        **overrides,
    }


def test_number_health_unknown_is_not_a_fabricated_half(clock):
    health = number_health([], pooled_attempts=[], now=clock.now)
    assert health["score"] is None
    assert health["samples"] == 0 and health["status"] == "unknown"


def test_number_health_separates_delivery_from_pickup_and_carries_uncertainty(clock):
    attempts = [{"outcome": "no_answer", "duration_seconds": 0, "expected_human": 0.1} for _ in range(30)]
    result = number_health(attempts, pooled_attempts=attempts, now=clock.now)
    assert result["delivery_success_rate"] > 0.9
    assert result["pickup_rate"] < 0.15
    assert 0 <= result["confidence_low"] <= result["confidence_high"] <= 1
    assert result["spam_label_confirmed"] is False


def test_case_mix_adjustment_does_not_blame_harder_leads(clock):
    easy = [{"outcome": "right_party" if i < 16 else "no_answer", "expected_human": 0.8} for i in range(20)]
    hard = [{"outcome": "right_party" if i < 4 else "no_answer", "expected_human": 0.2} for i in range(20)]
    a = number_health(easy, pooled_attempts=easy + hard, now=clock.now)
    b = number_health(hard, pooled_attempts=easy + hard, now=clock.now)
    assert a["adjusted_pickup_ratio"] == pytest.approx(b["adjusted_pickup_ratio"], abs=0.1)


def test_repeated_delivery_errors_trigger_investigation_not_spam_certification(clock):
    bad = [{"outcome": "temporary_failure", "delivery_failed": True} for _ in range(30)]
    result = number_health(bad, pooled_attempts=bad, now=clock.now)
    assert result["status"] == "investigating"
    assert result["score"] < 0.5 and not result["spam_label_confirmed"]


def test_forecast_subtracts_existing_used_reserved_and_warmup_capacity(clock):
    leads = [
        {
            "id": str(i),
            "region": "415",
            "timezone": "America/Los_Angeles",
            "status": "eligible",
            "next_attempt_at": clock.now.isoformat(),
            "attempt_count": 0,
        }
        for i in range(7)
    ]
    inventory = [
        number(used_today=2, reserved_attempts=1),
        number("new", region="212", lifecycle="purchased", daily_limit=100),
    ]
    result = capacity_forecast(
        leads,
        inventory,
        now=clock.now,
        horizon_hours=24,
        rep_minutes=480,
        handling_seconds=120,
        daily_capacity_per_number=5,
        policy={"max_attempts": 1, "local_start_hour": 9, "local_end_hour": 20},
    )
    region = result["regions"][0]
    assert region["forecast_attempts"] == 7
    assert region["usable_existing_capacity"] == 2
    assert region["proposed_additional"] == 1
    assert result["purchased"] is False and result["approval_required"] is True
    assert result["estimate_requires_provider_quote"] is True


def test_forecast_does_not_buy_capacity_for_unavailable_reps_or_suppressed_leads(clock):
    leads = [
        {"id": "a", "region": "415", "status": "eligible"},
        {"id": "b", "region": "212", "status": "eligible", "suppressed": True},
    ]
    result = capacity_forecast(leads, [], now=clock.now, rep_minutes=0)
    assert result["rep_capacity_attempts"] == 0
    assert sum(r["proposed_additional"] for r in result["regions"]) == 0
    assert result["capacity_shortfall"] > 0


def test_allocation_retains_binding_and_explains_health_reassignment():
    lead = {"id": "lead", "region": "415"}
    choice = select_number(lead, [number("bound"), number("other")], bound_id="bound")
    assert choice["number_id"] == "bound" and choice["reason"] == "existing_binding"
    choice = select_number(
        lead, [number("bound", lifecycle="paused_for_outbound"), number("other")], bound_id="bound"
    )
    assert choice["number_id"] == "other"
    assert choice["reassignment_reason"] and choice["previous_number_id"] == "bound"


def test_allocation_never_falls_back_to_an_unrelated_region_and_honors_detached_lease():
    lead = {"id": "lead", "region": "415"}
    assert select_number(lead, [number("ny", region="212")])["number_id"] is None
    assert select_number(lead, [number(active_obligations=1)])["number_id"] is None
    assert select_number(lead, [number("general", region="general")])["number_id"] == "general"


def test_forecast_accounts_for_due_callbacks_and_future_horizon(clock):
    leads = [
        {
            "id": "due",
            "region": "415",
            "status": "eligible",
            "timezone": "America/Los_Angeles",
            "callback_due_at": (clock.now + timedelta(hours=1)).isoformat(),
            "attempt_count": 1,
        },
        {
            "id": "later",
            "region": "415",
            "status": "eligible",
            "next_attempt_at": (clock.now + timedelta(days=10)).isoformat(),
        },
    ]
    result = capacity_forecast(
        leads,
        [],
        now=clock.now,
        horizon_hours=8,
        rep_minutes=120,
        policy={"max_attempts": 2, "local_start_hour": 9, "local_end_hour": 20},
    )
    assert sum(r["forecast_attempts"] for r in result["regions"]) == 1


def test_invalid_capacity_is_rejected(clock):
    with pytest.raises(ValueError):
        capacity_forecast([], [], now=clock.now, daily_capacity_per_number=0)


def test_existing_inventory_waiting_for_setup_or_review_does_not_trigger_identity_churn(clock):
    leads = [
        {
            "id": str(i),
            "region": "415",
            "timezone": "America/Los_Angeles",
            "status": "eligible",
            "attempt_count": 0,
        }
        for i in range(7)
    ]
    for state in [
        "purchased",
        "identity_configured",
        "callback_tested",
        "investigating",
        "paused_for_outbound",
    ]:
        result = capacity_forecast(
            leads,
            [number(lifecycle=state, daily_limit=20)],
            now=clock.now,
            daily_capacity_per_number=20,
            policy={"max_attempts": 1},
        )
        region = result["regions"][0]
        assert region["usable_existing_capacity"] == 0
        assert region["capacity_waiting_for_review"] >= 7
        assert region["proposed_additional"] == 0
