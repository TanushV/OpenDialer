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
    # The pooled reference describes other numbers; a number is never its own comparison.
    result = number_health(bad, pooled_attempts=[], now=clock.now)
    assert result["delivery_reference"]["source"] == "assumed_complete_delivery"
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
    assert choice["number_id"] is None
    assert choice["reason_code"] == "reassignment_required"
    # The versioned API changes the binding before the pure selector uses it.
    approved = select_number(
        lead, [number("bound", lifecycle="paused_for_outbound"), number("other")], bound_id="other"
    )
    assert approved["number_id"] == "other" and approved["reason"] == "existing_binding"


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
    for state in ["purchased", "identity_configured", "callback_tested"]:
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
    # Held numbers are not assumed to return healthy; the shortfall is disclosed, never bought.
    for state in ["investigating", "paused_for_outbound"]:
        result = capacity_forecast(
            leads,
            [number(lifecycle=state, daily_limit=20)],
            now=clock.now,
            daily_capacity_per_number=20,
            policy={"max_attempts": 1},
        )
        region = result["regions"][0]
        assert region["usable_existing_capacity"] == 0
        assert region["capacity_waiting_for_review"] == 0
        assert result["proposed_additional_numbers"] >= 1 and result["purchase_allowed"] is False


@pytest.mark.parametrize(
    "count,expected,outcome,prediction",
    [
        (2, "unknown", "no_answer", 0.8),
        (29, "unknown", "no_answer", 0.8),
        (40, "investigating", "no_answer", 0.8),
        (40, "observed", "no_answer", 0.05),
    ],
)
def test_health_support_and_case_mix(clock, count, expected, outcome, prediction):
    """C027: compare frozen expected pickup, and keep sparse evidence unknown."""
    from app.optimization.numbering import number_health

    rows = [
        {
            "id": str(i),
            "started_at": clock.now.isoformat(),
            "outcome": outcome,
            "expected_human": prediction,
            "prediction_at": clock.now.isoformat(),
            "feature_schema_version": 2,
            "campaign_id": "campaign",
            "segment": "same",
            "local_hour": 10,
            "attempt_count": 0,
        }
        for i in range(count)
    ]
    health = number_health(rows, pooled_attempts=rows, now=clock.now)
    assert health["status"] == expected
    assert health["spam_label_confirmed"] is False
    if count >= 30:
        assert health.get("case_mix_residual_mean") == pytest.approx(-prediction)
        assert health.get("comparable_samples") == count


def observed_attempt(clock, **changes):
    return {
        "started_at": clock.now.isoformat(),
        "prediction_at": clock.now.isoformat(),
        "outcome": "no_answer",
        "expected_human": 0.8,
        "feature_schema_version": 2,
        "campaign_id": "campaign",
        "segment": "same",
        "local_hour": 10,
        "attempt_count": 0,
        **changes,
    }


def test_health_without_prediction_time_cannot_claim_pre_call_comparison(clock):
    rows = [observed_attempt(clock, prediction_at=None) for _ in range(40)]
    health = number_health(rows, pooled_attempts=rows, now=clock.now)
    assert health["comparable_samples"] == 0
    assert health["status"] == "unknown"


def test_health_zero_prediction_unknown_labels_and_wrong_leg_do_not_invent_support(clock):
    rows = [observed_attempt(clock, expected_human=0) for _ in range(40)]
    rows += [observed_attempt(clock, outcome="unclassified", connected_seconds=2) for _ in range(40)]
    rows += [observed_attempt(clock, outcome="right_party", connected_seconds=2, connected_leg_kind="agent")]
    health = number_health(rows, pooled_attempts=rows, now=clock.now)
    assert health["human_classified_samples"] == 41
    assert health["human_unknown_samples"] == 40
    assert health["comparable_samples"] == 41
    assert health["expected_human_answers"] == pytest.approx(0.8)
    assert health["short_connected_calls"] == 0
    assert health["status"] == "observed"


def test_health_missing_or_future_predictions_are_excluded_not_reconstructed(clock):
    rows = [observed_attempt(clock, expected_human=None) for _ in range(30)]
    rows += [observed_attempt(clock, prediction_at=(clock.now + timedelta(seconds=1)).isoformat())]
    health = number_health(rows, pooled_attempts=rows, now=clock.now)
    assert health["comparable_samples"] == 0
    assert health["excluded_comparison_samples"] == 31
    assert health["case_mix_residual_mean"] is None
    assert health["status"] == "unknown"


def test_health_residual_interval_accounts_for_variance_and_policy_version(clock):
    import statistics

    from app.optimization import numbering

    assess = getattr(numbering, "assess_number_health", None)
    assert callable(assess), "One pure assessment must own health decisions"
    rows = [observed_attempt(clock, outcome="right_party" if i < 15 else "no_answer") for i in range(40)]
    policy = {"min_comparable": 30, "material_drop": 0.2, "confidence": 0.95, "window_days": 90}
    result = assess(rows, {"as_of": clock.now.isoformat(), "attempts": rows}, None, policy)
    residuals = [0.2] * 15 + [-0.8] * 25
    mean = statistics.mean(residuals)
    margin = 1.96 * statistics.stdev(residuals) / (40**0.5)
    assert result["case_mix_interval"] == pytest.approx([mean - margin, mean + margin], abs=1e-5)
    assert result["status"] == "investigating"
    assert result["policy_version"] and result["policy"] == policy
    relaxed = assess(
        rows, {"as_of": clock.now.isoformat(), "attempts": rows}, None, {**policy, "material_drop": 0.6}
    )
    assert relaxed["status"] == "observed" and relaxed["policy_version"] != result["policy_version"]


def test_health_external_unknown_is_not_clean_and_configuration_can_block_without_samples(clock):
    from app.optimization import numbering

    assess = getattr(numbering, "assess_number_health", None)
    assert callable(assess), "Health must combine independent evidence without inventing labels"
    external = {
        "source": "authorized file",
        "observed_at": clock.now.isoformat(),
        "ownership_reference": "owned",
        "spam_label": None,
    }
    result = assess(
        [], {"as_of": clock.now.isoformat(), "configuration_errors": ["recording_not_disabled"]}, external, {}
    )
    assert result["status"] == "blocked" and result["score"] is None
    assert result["external"]["spam_label"] is None
    assert result["spam_label_confirmed"] is False
    assert "recording_not_disabled" in result["reason_codes"]


def test_bound_identity_never_changes_without_explicit_reassignment():
    for inventory in [[number("bound", lifecycle="paused_for_outbound"), number("other")], [number("other")]]:
        selected = select_number({"id": "lead", "region": "415"}, inventory, bound_id="bound")
        assert selected["number_id"] is None
        assert selected["reason_code"] == "reassignment_required"
