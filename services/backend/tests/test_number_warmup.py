"""Warmup starts at fifteen real attempts per rolling day and grows only with resolved evidence."""

from datetime import UTC, datetime, timedelta

from app.optimization.numbering import WARMUP, adaptive_daily_limit, select_number, warmup_allowance

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
    assert WARMUP["start"] == 15
    assert warmup_allowance(number(), [], START + timedelta(days=30)) == 15


def test_growth_needs_enough_resolved_calls_leads_and_calling_days():
    # After three calling days: 27 resolved attempts across 12 leads, and the day used its allowance.
    history = calls(4, 9)
    assert warmup_allowance(number(), history, START + timedelta(days=2, hours=23)) == 15
    assert warmup_allowance(number(), history, START + timedelta(days=3)) == 18
    assert warmup_allowance(number(), history, START + timedelta(days=4)) == 21
    unresolved = [(at, lead, False) for at, lead, _ in history]
    assert warmup_allowance(number(), unresolved, START + timedelta(days=4)) == 15
    few_leads = calls(4, 9, leads=3)
    assert warmup_allowance(number(), few_leads, START + timedelta(days=4)) == 15


def test_growth_is_at_most_twenty_percent_a_day_and_never_above_the_owners_maximum():
    history = calls(30, 60)
    grown = [warmup_allowance(number(), history, START + timedelta(days=d)) for d in range(1, 30)]
    assert all(b - a <= max(1, int(a * 0.2)) for a, b in zip(grown, grown[1:], strict=False))
    assert max(grown) == 60
    assert warmup_allowance(number(daily_limit=5), history, START + timedelta(days=30)) == 5


def test_an_idle_day_holds_the_allowance():
    history = calls(4, 9) + [(START + timedelta(days=6, minutes=i), f"lead-{i}", True) for i in range(5)]
    assert warmup_allowance(number(), history, START + timedelta(days=4)) == 21
    # The fifth day had no calls, so it adds nothing even though the totals already qualified.
    assert warmup_allowance(number(), history, START + timedelta(days=5, hours=1)) == 21


def test_the_effective_limit_uses_the_allowance_and_still_drops_on_trouble():
    value = {**number(), "warmup_allowance": 6}
    assert adaptive_daily_limit(value, {"recommended_action": "continue"}) == 6
    assert adaptive_daily_limit(value, {"recommended_action": "hold_outbound"}) == 0
    healthy = {"lifecycle": "healthy", "daily_limit": 40}
    assert warmup_allowance(healthy, [], START) == 40


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
