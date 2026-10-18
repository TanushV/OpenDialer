"""Small exhaustive capacity cases provide an independent allocation oracle."""

import itertools
import math

import pytest
from app.optimization.numbering import capacity_forecast, select_number


def report(clock, demand, inventory, rep=100):
    leads = [
        {"id": f"{r}-{i}", "region": r, "timezone": "UTC", "status": "eligible"}
        for r, count in demand.items()
        for i in range(count)
    ]
    return capacity_forecast(
        leads,
        inventory,
        now=clock.now.replace(hour=10),
        rep_minutes=rep * 2,
        handling_seconds=120,
        daily_capacity_per_number=5,
        policy={"max_attempts": 1},
    )


def test_shared_pool_matches_small_exhaustive_oracle(clock):
    for a, b, general, regional in itertools.product(range(4), repeat=4):
        inventory = [
            {"id": "g", "region": "general", "daily_limit": general, "lifecycle": "healthy"},
            {"id": "a", "region": "415", "daily_limit": regional, "lifecycle": "healthy"},
        ]
        result = report(clock, {"415": a, "general": b}, inventory)
        maximum = max(min(a, regional + x) + min(b, general - x) for x in range(general + 1))
        assert result["existing_only_capacity"] == maximum
        assert result["minimum_additional_numbers"] == math.ceil((a + b - maximum) / 5)
        assert (
            sum(r["usable_existing_capacity"] + r["general_fallback_capacity"] for r in result["regions"])
            == maximum
        )


def test_use_available_inventory_before_proposing_numbers_for_a_rep_limited_list(clock):
    result = report(
        clock,
        {"415": 30, "212": 20},
        [{"id": "ny", "region": "212", "daily_limit": 20, "lifecycle": "healthy"}],
        rep=20,
    )
    assert result["existing_only_capacity"] == 20
    assert result["minimum_additional_numbers"] == 0
    assert result["staffing_shortfall"] == 30


def test_booked_hold_and_unknown_timezone_do_not_create_number_demand(clock):
    result = capacity_forecast(
        [
            {"id": "booked", "status": "eligible", "timezone": "UTC", "outreach_hold": "meeting_booked"},
            {"id": "unknown", "status": "eligible", "timezone": None},
        ],
        [],
        now=clock.now.replace(hour=10),
        policy={"max_attempts": 1},
    )
    assert result["forecast_attempts"] == 0
    assert result["minimum_additional_numbers"] == 0
    assert result["window_unknown_leads"] == 1


@pytest.mark.parametrize(
    "unavailable",
    [
        {"provider_present": False},
        {"identity_configured": False},
        {"callback_tested": False},
        {"provider_recording_enabled": True},
    ],
)
def test_unverified_inventory_never_counts_as_callable_capacity(clock, unavailable):
    number = {"id": "n", "region": "415", "daily_limit": 50, "lifecycle": "healthy", **unavailable}
    assert select_number({"region": "415"}, [number])["number_id"] is None
    result = report(clock, {"415": 10}, [number])
    assert result["existing_only_capacity"] == 0


def test_binding_waits_for_its_daily_capacity_instead_of_rotating_on_each_retry():
    bound = {"id": "bound", "region": "415", "daily_limit": 5, "used_today": 5, "lifecycle": "healthy"}
    other = {**bound, "id": "spare", "used_today": 0}
    selection = select_number({"region": "415"}, [bound, other], bound_id="bound")
    assert selection["number_id"] is None
    assert "bound" in selection["reason"].lower()
    assert selection["reassignment_reason"] is None
