"""E06/C022-C025: one demand meaning and an independent tiny capacity oracle."""

import itertools
import math
from datetime import timedelta

import pytest
from app.optimization.numbering import capacity_forecast
from test_time_capacity import NOW, lead, number, window


def plan(leads, inventory, **overrides):
    options = dict(
        now=NOW,
        horizon_hours=24,
        rep_minutes=120,
        handling_seconds=120,
        daily_capacity_per_number=5,
        policy={"max_attempts": 3, "min_retry_seconds": 3600, "local_start_hour": 9, "local_end_hour": 11},
    )
    options.update(overrides)
    return capacity_forecast(leads, inventory, **options)


def test_same_availability_same_demand():
    leads = [lead(str(i)) for i in range(12)]
    inventory = [number(daily_limit=100)]
    simple = plan(leads, inventory)
    anonymous = plan(leads, inventory, staffed_windows=[window(minutes=120)])
    named = plan(leads, inventory, staffed_windows=[{**window(minutes=120), "rep_ids": ["alice"]}])
    assert simple["forecast_attempts"] == anonymous["forecast_attempts"] == named["forecast_attempts"] == 12
    assert (
        simple["existing_only_capacity"]
        == anonymous["existing_only_capacity"]
        == named["existing_only_capacity"]
    )
    for result in (simple, anonymous, named):
        assert result.get("demand_definition") == "known_next_attempts"
        assert result.get("known_next_attempts") == 12
        retries = result.get("contingent_retry_low_mean_high")
        assert retries and 0 <= retries["low"] <= retries["mean"] <= retries["high"] <= 24


@pytest.mark.parametrize("reverse", [False, True])
def test_conservation_known_and_contingent(reverse):
    leads = [lead(str(i), region="415" if i % 2 else "212") for i in range(160)]
    if reverse:
        leads.reverse()
    result = plan(
        leads,
        [number(daily_limit=80)],
        rep_minutes=360,
        staffed_windows=[window(minutes=360)],
        policy={"max_attempts": 4, "local_start_hour": 9, "local_end_hour": 20},
    )
    assert result["existing_only_capacity"] == 80
    assert len({item["lead_id"] for item in result["existing_schedule"]}) == 80
    assert result.get("known_next_attempts") == 160
    assert result["forecast_attempts"] == 160, (
        "Uncertain retries are a separate forecast, not current obligations"
    )
    assert result.get("contingent_retry_low_mean_high", {}).get("mean", 0) > 0
    assert result["purchase_allowed"] is False


def exhaustive_maximum(regions, general, regional, rep_slots):
    """Enumerate actual assignments; no production feasibility function is reused."""
    best = 0
    for choices in itertools.product((None, "general", "regional"), repeat=len(regions)):
        if any(
            choice == "regional" and region != "415" for choice, region in zip(choices, regions, strict=True)
        ):
            continue
        count = sum(c is not None for c in choices)
        if (
            choices.count("general") <= general
            and choices.count("regional") <= regional
            and count <= rep_slots
        ):
            best = max(best, count)
    return best


@pytest.mark.parametrize("general,regional,rep_slots", list(itertools.product(range(3), range(3), (2, 4))))
def test_minimum_additions_independent_oracle(general, regional, rep_slots):
    regions = ["415", "415", "212", "212"]
    result = plan(
        [lead(str(i), region=r) for i, r in enumerate(regions)],
        [number("g", daily_limit=general), number("r", region="415", daily_limit=regional)],
        staffed_windows=[window(minutes=rep_slots * 2)],
        daily_capacity_per_number=2,
        policy={"max_attempts": 1, "local_start_hour": 9, "local_end_hour": 20},
    )
    expected = exhaustive_maximum(regions, general, regional, rep_slots)
    assert result["existing_only_capacity"] == expected
    target = min(len(regions), rep_slots)
    addition = next(
        n for n in range(3) if exhaustive_maximum(regions, general + n * 2, regional, rep_slots) == target
    )
    assert result["minimum_additional_numbers"] == addition
    assert result["lower_bound"] <= addition
    assert result.get("search_operations", math.inf) <= 200000


@pytest.mark.parametrize("ramp", [20, 50])
def test_staffing_binding_and_time_constraints(ramp):
    leads = [lead(str(i), region="415" if i < 120 else "212") for i in range(240)]
    result = plan(
        leads,
        [number("regional", region="415", daily_limit=60), number("general", daily_limit=80)],
        rep_minutes=360,
        daily_capacity_per_number=ramp,
        staffed_windows=[window(minutes=360)],
        policy={"max_attempts": 1, "local_start_hour": 9, "local_end_hour": 20},
    )
    assert result["existing_only_capacity"] == 140
    assert result["rep_capacity_attempts"] == 180
    assert result["staffing_shortfall"] == 60
    assert result["number_shortfall"] == 40
    assert result["minimum_additional_numbers"] == math.ceil(40 / ramp)
    assert result["existing_plus_addition_capacity"] == 180
    # A known callback cannot consume somebody else's anonymous headcount.
    held = plan(
        [lead("held", callback_owner_id="alice", callback_due_at=NOW.isoformat())],
        [number()],
        staffed_windows=[window(minutes=10)],
    )
    assert held["existing_only_capacity"] == 0
    bound = plan(
        [lead("bound", bound_number_id="owned")],
        [number(daily_limit=1, used_today=1), number("spare")],
        staffed_windows=[window(minutes=10)],
    )
    assert bound["minimum_additional_numbers"] == 0 and bound["existing_only_capacity"] == 0
    pending = plan(
        [lead("future")],
        [number(lifecycle="purchased", available_at=(NOW + timedelta(days=2)).isoformat())],
        staffed_windows=[window(minutes=10)],
    )
    assert pending["existing_only_capacity"] == 0


def test_unequal_work_occupies_consecutive_grid_buckets_and_full_windows():
    result = plan(
        [
            lead("short", predicted_work_seconds=30, requested_contact=True),
            lead("long", predicted_work_seconds=90),
        ],
        [number()],
        staffed_windows=[window(minutes=2)],
    )
    assert result["existing_only_capacity"] == 2
    rows = result["existing_schedule"]
    assert rows[0]["at"] == NOW.isoformat()
    assert rows[0]["end_at"] == (NOW + timedelta(seconds=30)).isoformat()
    assert rows[1]["at"] == rows[0]["end_at"]
    assert rows[1]["end_at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert result["assumptions"]["staffing_grid_seconds"] == 30


def test_one_roster_pool_cannot_count_named_and_anonymous_members_twice():
    periods = [
        {**window(minutes=2, reps=2), "pool_id": "team"},
        {**window(minutes=2), "rep_ids": ["alice"], "pool_id": "team"},
    ]
    result = plan([lead(str(i)) for i in range(3)], [number(capacity=3)], staffed_windows=periods)
    assert result["existing_only_capacity"] == 2
    assert result["rep_capacity_attempts"] == 2


def test_declared_account_channel_limit_applies_across_numbers():
    result = plan(
        [lead(str(i)) for i in range(3)],
        [number(str(i)) for i in range(3)],
        staffed_windows=[window(minutes=2, reps=3)],
        policy={"max_attempts": 1, "account_channel_limit": 1},
    )
    assert result["existing_only_capacity"] == 1
    assert result["existing_plus_addition_capacity"] == 1
    assert result["minimum_additional_numbers"] == 0


def test_pending_setup_delay_is_used_in_the_conditional_schedule():
    result = plan(
        [lead("due")],
        [number(lifecycle="purchased", available_at=(NOW + timedelta(days=2)).isoformat())],
        staffed_windows=[window(minutes=10)],
    )
    assert sum(r["capacity_waiting_for_review"] for r in result["regions"]) == 0
    assert result["proposed_additional_numbers"] == 1


def test_search_budget_is_global_and_reports_best_feasible_with_unknown_minimum(monkeypatch):
    import app.optimization.capacity as capacity

    monkeypatch.setattr(capacity, "MAX_PLANNING_STEPS", 12)
    result = plan([lead(str(i)) for i in range(40)], [number(daily_limit=2)], staffed_windows=[window()])
    assert result["search_operations"] <= 12
    assert result["search_complete"] is False
    assert result["minimum_additions"] is None
    assert result["best_found_additions"] >= 0
    assert result["known_next_attempts"] == 40
    assert result["omitted_rows"]["work"] >= 0


def test_priority_classes_and_shuffled_input_are_stable():
    rows = [
        lead("z-request", requested_contact=True),
        lead("a-value", expected_conversion=0.9),
        lead("y-coverage", coverage_due_at=NOW.isoformat()),
        lead("x-callback", callback_due_at=NOW.isoformat()),
    ]
    options = {"staffed_windows": [window(minutes=6)], "policy": {"max_attempts": 1}}
    a, b = plan(rows, [number()], **options), plan(rows[::-1], [number()], **options)
    assert a["existing_schedule"] == b["existing_schedule"]
    assert [r["lead_id"] for r in a["existing_schedule"]] == ["x-callback", "z-request", "y-coverage"]


def test_immutable_planning_contract_and_canonical_fingerprint():
    import dataclasses

    import app.optimization.capacity as capacity

    for name in ("PlanningInput", "Availability", "AttemptWork", "NumberCapacity", "PlanningResult"):
        contract = getattr(capacity, name, None)
        assert contract is not None and dataclasses.is_dataclass(contract), name
        assert contract.__dataclass_params__.frozen
    rows = [lead("b"), lead("a")]
    a, b = plan(rows, [number()]), plan(rows[::-1], [number()])
    assert a["input_fingerprint"] == b["input_fingerprint"]
    assert a["source_versions"] and a["demand_definition"] == "known_next_attempts"


@pytest.mark.parametrize("success,total", [(0, 0), (0, 100), (100, 100)])
def test_retry_forecast_uses_only_matured_conditioned_support(success, total):
    rows = [lead("one", segment="same")]
    history = [
        {
            "segment": "same",
            "attempt_index": 1,
            "needs_retry": i < success,
            "available_at": NOW.isoformat(),
            "matured": True,
        }
        for i in range(total)
    ]
    history += [
        {
            "segment": "same",
            "attempt_index": 1,
            "needs_retry": True,
            "available_at": (NOW + timedelta(days=1)).isoformat(),
            "matured": True,
        }
    ] * 50
    result = plan(
        rows,
        [number()],
        retry_observations=history,
        policy={"max_attempts": 3, "min_retry_seconds": 3600, "local_start_hour": 9, "local_end_hour": 20},
    )
    retry = result["contingent_retry_low_mean_high"]
    assert 0 <= retry["low"] <= retry["mean"] <= retry["high"] <= 2
    assert retry["support"] == total
    assert retry["prior"] == [1, 1]
    if total:
        assert (retry["mean"] > 0.8) is bool(success)
    else:
        assert retry["mean"] == pytest.approx(0.75)


def test_unhealthy_binding_requires_explicit_reassignment_evidence():
    result = plan(
        [lead("one", bound_number_id="owned")],
        [number(lifecycle="paused_for_outbound"), number("spare")],
        staffed_windows=[window(minutes=4)],
    )
    assert result["existing_only_capacity"] == 0
    assert result["binding_shortfall"] == 1
    approved = plan(
        [lead("one", bound_number_id="owned", reassignment_approved=True)],
        [number(lifecycle="paused_for_outbound"), number("spare")],
        staffed_windows=[window(minutes=4)],
    )
    assert approved["existing_schedule"][0]["number_id"] == "spare"
