"""Warmup starts at five real attempts per rolling day and grows only with resolved evidence."""

from datetime import UTC, datetime, timedelta

from app.optimization.numbering import WARMUP, adaptive_daily_limit, warmup_allowance

START = datetime(2026, 11, 2, 16, tzinfo=UTC)


def number(**values):
    return {
        "lifecycle": "limited_real_use",
        "daily_limit": 40,
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


def test_a_new_number_starts_at_five_and_waiting_alone_earns_nothing():
    assert WARMUP["start"] == 5
    assert warmup_allowance(number(), [], START + timedelta(days=30)) == 5


def test_growth_needs_enough_resolved_calls_leads_and_calling_days():
    # After three calling days: 21 resolved attempts across 12 leads, and the day used its allowance.
    history = calls(4, 7)
    assert warmup_allowance(number(), history, START + timedelta(days=2, hours=23)) == 5
    assert warmup_allowance(number(), history, START + timedelta(days=3)) == 6
    assert warmup_allowance(number(), history, START + timedelta(days=4)) == 7
    unresolved = [(at, lead, False) for at, lead, _ in history]
    assert warmup_allowance(number(), unresolved, START + timedelta(days=4)) == 5
    few_leads = calls(4, 7, leads=3)
    assert warmup_allowance(number(), few_leads, START + timedelta(days=4)) == 5


def test_growth_is_at_most_twenty_percent_a_day_and_never_above_the_owners_maximum():
    history = calls(30, 40)
    grown = [warmup_allowance(number(), history, START + timedelta(days=d)) for d in range(1, 30)]
    assert all(b - a <= max(1, int(a * 0.2)) for a, b in zip(grown, grown[1:], strict=False))
    assert max(grown) == 40
    assert warmup_allowance(number(daily_limit=5), history, START + timedelta(days=30)) == 5


def test_an_idle_day_holds_the_allowance():
    history = calls(4, 7) + [(START + timedelta(days=6, minutes=i), f"lead-{i}", True) for i in range(5)]
    assert warmup_allowance(number(), history, START + timedelta(days=4)) == 7
    # The fifth day had no calls, so it adds nothing even though the totals already qualified.
    assert warmup_allowance(number(), history, START + timedelta(days=5, hours=1)) == 7


def test_the_effective_limit_uses_the_allowance_and_still_drops_on_trouble():
    value = {**number(), "warmup_allowance": 6}
    assert adaptive_daily_limit(value, {"recommended_action": "continue"}) == 6
    assert adaptive_daily_limit(value, {"recommended_action": "hold_outbound"}) == 0
    healthy = {"lifecycle": "healthy", "daily_limit": 40}
    assert warmup_allowance(healthy, [], START) == 40
