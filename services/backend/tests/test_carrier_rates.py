"""Work package 6.17: today's short-call and abandoned-call shares from the workspace's own calls."""

from datetime import UTC, datetime

from app.calling.rates import carrier_rates

NOW = datetime(2026, 11, 22, 18, tzinfo=UTC)
TODAY = "2026-11-22T15:00:00+00:00"


def call(**fields):
    return {"provider_id": "leg", "started_at": TODAY, "ended_at": TODAY, **fields}


def test_short_and_abandoned_shares_follow_the_telnyx_definitions():
    calls = [
        call(answered_at=TODAY, connected_seconds=4),  # short: 6 s or less after answer
        call(answered_at=TODAY, connected_seconds=6),  # short at exactly 6 s
        call(answered_at=TODAY, connected_seconds=7),  # the 7-second hold keeps an early mailbox end out
        call(provider_hangup_source="caller"),  # we ended it while ringing: abandoned
        call(provider_hangup_source="callee"),  # they declined: not ours
        call(provider_hangup_source="caller", started_at="2026-11-21T23:00:00+00:00"),  # yesterday (UTC)
        {"started_at": TODAY},  # never reached the carrier
    ]
    rates = carrier_rates(calls, NOW)
    assert (rates["connected"], rates["short"], rates["short_share"]) == (3, 2, 0.667)
    assert (rates["outbound"], rates["abandoned"], rates["abandoned_share"]) == (5, 1, 0.2)
    assert rates["short_limit"] == 0.15 and rates["abandoned_limit"] == 0.20


def test_no_calls_gives_zero_shares_not_an_error():
    assert carrier_rates([], NOW)["short_share"] == 0.0
