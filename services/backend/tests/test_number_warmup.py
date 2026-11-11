"""Warmup starts at fifteen real attempts per rolling day and grows only with resolved evidence."""

from datetime import UTC, datetime, timedelta

from app.optimization.numbering import adaptive_daily_limit, select_number, warmup_allowance

START = datetime(2026, 11, 2, 16, tzinfo=UTC)


def number(**values):
    return {
        "lifecycle": "limited_real_use",
        "daily_limit": 60,
        "warmup_started_at": START.isoformat(),
        **values,
    }


def calls(days, per_day, leads=12):
    """Resolved attempts spread across the first `days` days, cycling through `leads` leads."""
    return [
        (START + timedelta(days=d, minutes=10 * i), f"lead-{(d * per_day + i) % leads}", True)
        for d in range(days)
        for i in range(per_day)
    ]


def test_a_new_number_starts_at_fifteen_and_waiting_alone_earns_nothing():
    assert warmup_allowance(number(), [], START + timedelta(days=30)) == 15


def test_business_day_ramp_excludes_weekends_unresolved_calls_and_idle_days():
    history = calls(30, 1)
    assert warmup_allowance(number(), history, START + timedelta(days=1)) == 15
    assert warmup_allowance(number(), history, START + timedelta(days=2)) == 25
    assert warmup_allowance(number(), history, START + timedelta(days=5)) == 40
    assert warmup_allowance(number(), history, START + timedelta(days=11)) == 40
    assert warmup_allowance(number(), history, START + timedelta(days=12)) == 60
    unresolved = [(at, lead, False) for at, lead, _ in history]
    assert warmup_allowance(number(), unresolved, START + timedelta(days=30)) == 15
    assert warmup_allowance(number(daily_limit=5), history, START + timedelta(days=30)) == 5


def test_the_effective_limit_uses_the_allowance_and_still_drops_on_trouble():
    value = {**number(), "warmup_allowance": 6}
    assert adaptive_daily_limit(value, {"recommended_action": "continue"}) == 6
    assert adaptive_daily_limit(value, {"recommended_action": "hold_outbound"}) == 0
    assert (
        adaptive_daily_limit(
            {**value, "warmup_allowance": 40},
            {"recommended_action": "review_workload", "delivery_success_rate": 0.9, "delivery_samples": 100},
        )
        <= 15
    )

    healthy = {"lifecycle": "healthy", "daily_limit": 40}
    assert warmup_allowance(healthy, [], START) == 40


def test_mature_numbers_can_reach_ninety_and_saved_limits_cannot_exceed_it():
    assert warmup_allowance(number(daily_limit=90), calls(30, 1), START + timedelta(days=30)) == 90
    assert adaptive_daily_limit({"daily_limit": 120}, {}) == 90


def test_the_same_metro_comes_before_the_least_used_number():
    base = {"lifecycle": "limited_real_use", "capacity": 1, "daily_limit": 60, "reserved_attempts": 0}
    sf = {**base, "id": "sf", "phone": "+16282564157", "region": "628", "used_today": 9}
    la = {**base, "id": "la", "phone": "+12135550100", "region": "213", "used_today": 0}
    lead = {"phone": "+14155550101", "region": "415"}
    assert select_number(lead, [sf, la])["number_id"] == "sf"
    assert select_number(lead, [{**sf, "used_today": 60}, la])["number_id"] == "la"
    nyc = {**base, "id": "nyc", "phone": "+19293799649", "region": "929", "used_today": 9}
    albany = {**base, "id": "albany", "phone": "+15185550100", "region": "518", "used_today": 0}
    assert select_number({"phone": "+12125550101", "region": "212"}, [nyc, albany])["number_id"] == "nyc"
