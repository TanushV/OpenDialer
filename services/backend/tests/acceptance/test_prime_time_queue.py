"""Call insights plan P5: calling prefers each lead's local prime time.

One curve in local time for every prospect: prime 9:30 to 11:30 and 16:00 to 17:00, good 9:00 to 9:30, 11:30 to 12:00
and 13:30 to 16:00, low 12:00 to 13:30. The queue calls the best band available now; it only ranks, so a due callback
still goes first and a lead is never held back. Retries land in the lead's next prime window.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.platform.db import MODELS
from app.scheduling import prime_time
from conftest import ready
from journey import call, event, get, import_leads, post, run_jobs
from sqlalchemy import text
from test_final_stage import sign_in

TUESDAY = datetime(2026, 12, 1, tzinfo=UTC)
PACIFIC, EASTERN, CENTRAL = (
    ZoneInfo("America/Los_Angeles"),
    ZoneInfo("America/New_York"),
    ZoneInfo("America/Chicago"),
)


@pytest.mark.parametrize(
    ("hour", "minute", "band"),
    [
        (9, 0, 1.0),
        (9, 30, 2.0),
        (11, 29, 2.0),
        (11, 30, 1.0),
        (12, 0, 0.0),
        (13, 29, 0.0),
        (13, 30, 1.0),
        (16, 0, 2.0),
    ],
)
def test_the_default_curve(hour, minute, band):
    assert prime_time.default_band(datetime(2026, 12, 1, hour, minute)) == band


def test_an_unsure_lead_is_scored_by_its_worst_zone():
    at = datetime(2026, 12, 1, 10, 0, tzinfo=PACIFIC)  # 13:00 Eastern (low), 10:00 Pacific (prime)
    assert prime_time.score("America/Los_Angeles", at) == prime_time.PRIME
    assert prime_time.score("America/Los_Angeles|America/New_York", at) == prime_time.LOW


def test_own_answer_rates_take_over_after_enough_dials():
    assert prime_time.learned_scores({1: (100, 10)}) == {}, "Too few dials: the default curve stands"
    samples = {index: (100, 10) for index in range(16)}
    samples[6] = (400, 120)  # 12:00 to 12:30 answers three times as often here
    learned = prime_time.learned_scores(samples)
    assert learned[6] > prime_time.default_band(datetime(2026, 12, 1, 12, 0))


def at(clock, ws, local):
    clock.now = local.astimezone(UTC)
    sign_in(ws)


def only(ws, rows, name="Prime list"):
    # The seeded leads stay out of the way: these lists are the whole queue.
    for lead in call(ws, "GET", "/v1/workspace/leads")["items"]:
        post(ws, "/v1/suppressions", {"phone": lead["phone"], "reason": "opt_out"})
    import_leads(ws, rows, list_name=name)
    return name


def chosen(ws, name):
    ready(ws)
    packet = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"], "list_names": [name]})
    return packet


ROWS = [
    {"name": "Pat Pacific", "phone": "+14155550301"},
    {"name": "Erin Eastern", "phone": "+12125550302"},
    {"name": "Cam Central", "phone": "+13125550303"},
]


def test_at_lunch_on_the_west_coast_the_central_and_eastern_leads_go_first(workspace, clock):
    at(clock, workspace, TUESDAY.replace(hour=12, minute=30, tzinfo=PACIFIC))
    name = only(workspace, ROWS)
    packet = chosen(workspace, name)
    assert packet["lead"]["name"] in {"Erin Eastern", "Cam Central"}


def test_early_morning_pacific_is_prime_time_in_the_east(workspace, clock):
    at(clock, workspace, TUESDAY.replace(hour=7, minute=0, tzinfo=PACIFIC))  # 10:00 Eastern, 9:00 Central
    name = only(workspace, ROWS)
    packet = chosen(workspace, name)
    assert packet["lead"]["name"] == "Erin Eastern"
    assert packet["local"] == {"zone": "Eastern", "local_time": "10:00", "band": "prime"}


def test_a_lead_in_a_low_band_is_still_called_when_nobody_else_is_due(workspace, clock):
    at(clock, workspace, TUESDAY.replace(hour=12, minute=30, tzinfo=PACIFIC))
    name = only(workspace, [ROWS[0]], name="Pacific only")
    assert chosen(workspace, name)["lead"]["name"] == "Pat Pacific"


def test_a_retry_lands_in_the_next_prime_window_in_the_other_block(workspace, app, clock):
    at(clock, workspace, TUESDAY.replace(hour=10, minute=0, tzinfo=EASTERN))
    name = only(workspace, [ROWS[1]], name="Retry")
    packet = chosen(workspace, name)
    placed = post(
        workspace, "/v1/calls/start", {"reservation_id": packet["reservation_id"], "idempotency_key": "r1"}
    )
    event(workspace, placed["id"], "call.hangup", hangup_cause="no_answer")
    run_jobs(workspace, rounds=2)
    with app.state.db.transaction(False) as s:
        lead = s.get(MODELS["leads"], packet["lead"]["id"])
        due = datetime.fromisoformat(lead.data["next_attempt_at"]).astimezone(EASTERN)
    assert due - clock.now >= timedelta(hours=24)
    assert prime_time.default_band(due) == prime_time.PRIME and due.hour >= 16, due
    assert due.weekday() < 5


def test_old_leads_get_their_zone_once_at_startup(app, workspace):
    from app.scheduling.prime_time import backfill_zones

    with app.state.db.transaction() as s:
        s.execute(text("UPDATE leads SET op_zone = NULL"))
    assert backfill_zones(app.state.db) >= 1
    with app.state.db.transaction(False) as s:
        assert all(lead.op_zone for lead in s.query(MODELS["leads"]))


def test_the_dialer_shows_why_this_lead_now(workspace, clock):
    at(clock, workspace, TUESDAY.replace(hour=13, minute=45, tzinfo=CENTRAL))
    name = only(workspace, [ROWS[2]], name="Central only")
    local = chosen(workspace, name)["local"]
    assert local == {"zone": "Central", "local_time": "1:45", "band": "good"}
    assert get(workspace, "/v1/workspace")["campaign"]


def test_a_prospect_another_rep_called_today_is_skipped_for_a_day(workspace, app, clock, monkeypatch):
    """Plan P5 step 8: on shared calling, a lead another rep's computer called in the last 24 hours waits."""
    import asyncio

    from app import ops
    from app.calling import pool_queue
    from app.numbers import pool

    at(clock, workspace, TUESDAY.replace(hour=10, minute=0, tzinfo=EASTERN))
    name = only(workspace, ROWS)
    ready(workspace)
    called = (clock.now - timedelta(hours=3)).isoformat()
    real = ops.reserve

    def shared(service, p, *args):
        return {**real(service, p, *args), "preferred_is_bound": False}

    async def recent(managed, prospect):
        return called if prospect == "+12125550302" else None

    async def assigned(service, p, provisional):
        return provisional

    monkeypatch.setattr(ops, "reserve", shared)
    monkeypatch.setattr(pool, "recent_elsewhere", recent)
    monkeypatch.setattr(pool_queue, "assign", assigned)
    service = app.state.service
    p = {
        "tenant_id": workspace["tenant_id"],
        "seller_id": workspace["seller_id"],
        "user_id": workspace["user_id"],
        "role": "admin",
    }
    packet = asyncio.run(pool_queue.reserve(service, p, workspace["campaign_id"], [name], None))
    assert packet["lead"]["name"] != "Erin Eastern"
    with service.db.transaction(False) as s:
        held = next(lead for lead in s.query(MODELS["leads"]) if lead.data.get("phone") == "+12125550302")
        assert (
            held.data["next_attempt_at"] == (datetime.fromisoformat(called) + timedelta(hours=24)).isoformat()
        )
        assert held.data["other_rep_called_at"] == called
