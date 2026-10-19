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
