"""A16/A17: named staffing must preserve callback ownership and real concurrency."""

import itertools
from datetime import timedelta

import pytest
from app.db import ActiveClaim, get, put, update
from app.optimization.numbering import capacity_forecast
from test_remediation_privacy import principal
from test_time_capacity import NOW, forecast, lead, number, window


def staffed(*owners, start=NOW, minutes=2):
    return {**window(start, minutes, len(owners)), "rep_ids": list(owners)}


def callback(identity="callback", owner="alice", **values):
    return lead(identity, callback_owner_id=owner, callback_due_at=NOW.isoformat(), **values)


def test_callback_cannot_borrow_another_reps_availability_or_be_fixed_with_numbers():
    result = forecast([callback()], [], staffed_windows=[staffed("bob")])
    assert result["existing_plus_addition_capacity"] == 0
    assert result["minimum_additional_numbers"] == 0
    assert result["owner_unavailable_attempts"] == 1
    assert result["staffing_shortfall"] == 1


def test_anonymous_headcount_does_not_establish_promised_owner_availability():
    result = forecast([callback(), lead("flexible")], [number()], staffed_windows=[window(minutes=4)])
    assert [r["lead_id"] for r in result["existing_schedule"]] == ["flexible"]
    assert result["owner_unavailable_attempts"] == 1


def test_callback_waits_for_owner_and_schedule_records_the_actual_rep():
    result = forecast(
        [callback(), lead("flexible")],
        [number(capacity=2)],
        staffed_windows=[staffed("bob"), staffed("alice", start=NOW + timedelta(minutes=2))],
    )
    by_lead = {r["lead_id"]: r for r in result["existing_schedule"]}
    assert by_lead["callback"]["rep_id"] == "alice"
    assert by_lead["callback"]["at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert by_lead["flexible"]["rep_id"] == "bob"


def test_overlapping_declarations_count_each_rep_only_once():
    result = forecast(
        [lead(str(i)) for i in range(4)],
        [number(capacity=4)],
        staffed_windows=[staffed("alice", "bob"), staffed("bob", "carol")],
    )
    assert result["rep_capacity_attempts"] == 3
    assert result["existing_only_capacity"] == 3
    assert {r["rep_id"] for r in result["existing_schedule"]} == {"alice", "bob", "carol"}


def test_two_callbacks_for_same_owner_cannot_use_parallel_rep_slots():
    result = forecast(
        [callback("a"), callback("b")],
        [number(capacity=2)],
        staffed_windows=[staffed("alice", "bob")],
    )
    assert result["existing_only_capacity"] == 1
    assert result["staffing_shortfall"] == 1
    assert result["minimum_additional_numbers"] == 0


def test_offset_windows_cannot_double_book_the_same_did_between_grid_points():
    result = forecast(
        [lead("one"), lead("two")],
        [number()],
        staffed_windows=[
            staffed("alice", minutes=4),
            staffed("bob", start=NOW + timedelta(minutes=1), minutes=4),
        ],
    )
    times = sorted(r["at"] for r in result["existing_schedule"])
    assert times == [NOW.isoformat(), (NOW + timedelta(minutes=2)).isoformat()]
    assert result["assumptions"]["staffing_grid_seconds"] == 120


def test_adjacent_periods_for_one_owner_are_continuous_availability():
    result = forecast(
        [callback()],
        [number()],
        staffed_windows=[
            staffed("alice", minutes=1),
            staffed("alice", start=NOW + timedelta(minutes=1), minutes=1),
        ],
    )
    assert result["existing_only_capacity"] == 1


def test_small_named_schedules_match_independent_owner_slot_oracle():
    # Three starts with varying named availability; one DID permits two simultaneous calls.
    roster = [{"a", "b"}, {"b"}, {"a", "b"}]
    for owners in itertools.product([None, "a", "b"], repeat=3):
        leads = [
            lead(str(i), callback_owner_id=owner, callback_due_at=NOW.isoformat() if owner else None)
            for i, owner in enumerate(owners)
        ]
        options = [None, *itertools.product(range(3), ("a", "b"))]
        optimum = (0, 0)
        for trial in itertools.product(options, repeat=3):
            chosen = [(i, value) for i, value in enumerate(trial) if value is not None]
            if any(rep not in roster[slot] or owners[i] not in {None, rep} for i, (slot, rep) in chosen):
                continue
            if len({value for _, value in chosen}) != len(chosen):
                continue
            optimum = max(optimum, (sum(owners[i] is not None for i, _ in chosen), len(chosen)))
        result = forecast(
            leads,
            [number(capacity=2)],
            staffed_windows=[
                staffed(*sorted(reps), start=NOW + timedelta(minutes=2 * i)) for i, reps in enumerate(roster)
            ],
        )
        schedule = result["existing_schedule"]
        assert (sum(row["callback"] for row in schedule), len(schedule)) == optimum
        assert len({(row["at"], row["rep_id"]) for row in schedule}) == len(schedule)


@pytest.mark.parametrize(
    "bad", ["duplicate", "count", "empty", "unknown", "foreign", "disabled", "anonymous_overlap"]
)
def test_staffing_rejects_invalid_or_unavailable_roster_without_replacing_settings(
    workspace, app, clock, bad
):
    service, p = app.state.service, principal(workspace)
    body = [staffed(p["user_id"], start=clock.now)]
    if bad == "duplicate":
        body[0].update(rep_ids=[p["user_id"], p["user_id"]], reps=2)
    elif bad == "count":
        body[0]["reps"] = 2
    elif bad == "empty":
        body[0]["rep_ids"] = []
    elif bad == "unknown":
        body[0]["rep_ids"] = ["unknown"]
    elif bad in {"foreign", "disabled"}:
        with service.db.transaction() as s:
            person = put(
                s,
                "users",
                p["tenant_id"],
                "foreign" if bad == "foreign" else p["seller_id"],
                {"name": "Unavailable", "role": "rep", "disabled": bad == "disabled"},
            )
            body[0]["rep_ids"] = [person.id]
    else:
        body.append(window(clock.now, 2))
    response = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"staffed_windows": body}
    )
    assert response.status_code == 422, response.text
    with service.db.transaction(False) as s:
        assert not service.optimization_config(s, p)["staffed_windows"]


def test_named_periods_save_merge_and_recheck_disabled_owner_in_forecast(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    periods = [
        staffed(p["user_id"], start=clock.now, minutes=10),
        staffed(p["user_id"], start=clock.now + timedelta(minutes=2), minutes=10),
    ]
    response = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"staffed_windows": periods}
    )
    assert response.status_code == 200, response.text
    with service.db.transaction() as s:
        assert service.optimization_config(s, p)["staffed_windows"] == periods
        update(get(s, "users", p["user_id"]), disabled=True)
        result = service.forecast(s, p)
    assert result["rep_capacity_attempts"] == 0
    assert result["unavailable_staff_count"] == 1


def test_busy_owner_cannot_be_reused_before_reservation_expiry():
    result = capacity_forecast(
        [callback(), lead("flexible")],
        [number(capacity=2)],
        now=NOW,
        staffed_windows=[staffed("alice", "bob", minutes=4)],
        rep_busy_until={"alice": (NOW + timedelta(minutes=2)).isoformat()},
    )
    by_lead = {r["lead_id"]: r for r in result["existing_schedule"]}
    assert by_lead["callback"]["at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert by_lead["flexible"]["rep_id"] == "bob"


def test_authority_excludes_live_claimed_work_and_occupied_rep(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        campaign = get(s, "campaigns", workspace["campaign_id"])
        update(
            campaign,
            optimization={
                **campaign.data.get("optimization", {}),
                "staffed_windows": [staffed(p["user_id"], start=clock.now, minutes=2)],
            },
        )
        leads = service.scoped(s, "leads", p)
        for row in leads:
            update(row, timezone="UTC", status="eligible", next_attempt_at=None, callback_due_at=None)
        s.add(
            ActiveClaim(
                tenant_id=p["tenant_id"],
                seller_id=p["seller_id"],
                endpoint_id=leads[0].data["endpoint_id"],
                lead_id=leads[0].id,
                rep_id=p["user_id"],
                number_id=service.scoped(s, "number_inventory", p)[0].id,
                reservation_id="forecast-reservation",
                call_id="uncertain-call",
                expires_at=clock.now - timedelta(seconds=1),
            )
        )
    with service.db.transaction(False) as s:
        result = service.forecast(s, p)
    assert result["forecast_attempts"] == len(leads) - 1
    assert result["existing_plus_addition_capacity"] == 0
    assert result["rep_capacity_attempts"] == 0
    assert result["occupied_rep_count"] == 1
