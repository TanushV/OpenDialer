"""A14-A18: capacity must fit declared work times, identities and daily limits."""

import itertools
import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest
from app.db import get, put, update
from app.optimization.numbering import capacity_forecast
from test_remediation_privacy import principal

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)


def lead(identity, **values):
    return {"id": identity, "status": "eligible", "timezone": "UTC", "region": "415", **values}


def number(identity="owned", **values):
    return {
        "id": identity,
        "region": "general",
        "lifecycle": "healthy",
        "daily_limit": 40,
        "capacity": 1,
        **values,
    }


def window(start=NOW, minutes=60, reps=1):
    return {
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=minutes)).isoformat(),
        "reps": reps,
    }


def forecast(leads, numbers, **kwargs):
    return capacity_forecast(
        leads,
        numbers,
        now=NOW,
        horizon_hours=48,
        rep_minutes=240,
        handling_seconds=120,
        daily_capacity_per_number=10,
        policy={"max_attempts": 1, "local_start_hour": 9, "local_end_hour": 20},
        staffed_windows=kwargs.pop("staffed_windows", [window()]),
        **kwargs,
    )


def test_calls_outside_staffed_hours_cannot_be_fixed_by_buying_numbers():
    result = forecast([lead("west", timezone="America/Los_Angeles")], [])
    assert result["existing_only_capacity"] == 0
    assert result["minimum_additional_numbers"] == 0
    assert result["staffing_shortfall"] == 1
    assert result["schedule_scope"] == "declared_staffed_windows"


def test_one_number_daily_capacity_cannot_be_borrowed_from_an_unstaffed_tomorrow():
    result = forecast([lead(str(i)) for i in range(15)], [number(daily_limit=10)])
    assert result["existing_only_capacity"] == 10
    assert result["minimum_additional_numbers"] == 1
    assert result["existing_plus_addition_capacity"] == 15


def test_exhausted_healthy_binding_cannot_be_rotated_to_make_a_proposal_feasible():
    result = forecast(
        [lead("bound", bound_number_id="owned")], [number(daily_limit=1, used_today=1), number("spare")]
    )
    assert result["existing_only_capacity"] == 0
    assert result["minimum_additional_numbers"] == 0
    assert result["binding_shortfall"] == 1


def test_healthy_binding_can_wait_for_its_next_actual_workday():
    result = forecast(
        [lead("bound", bound_number_id="owned")],
        [number(daily_limit=1, used_today=1)],
        staffed_windows=[window(), window(NOW + timedelta(days=1))],
    )
    assert result["existing_only_capacity"] == 1
    assert result["existing_schedule"][0]["at"].startswith("2026-10-20")
    assert result["existing_schedule"][0]["number_id"] == "owned"


def test_daily_debits_replace_reserved_calls_and_do_not_reset_between_staffed_windows():
    result = forecast(
        [lead(str(i)) for i in range(6)],
        [number(daily_limit=5, used_today=2, reserved_attempts=1)],
        staffed_windows=[window(minutes=10), window(NOW + timedelta(hours=1), minutes=10)],
    )
    assert result["existing_only_capacity"] == 2


def test_number_concurrency_and_rep_slots_constrain_the_same_attempt():
    result = forecast(
        [lead(str(i)) for i in range(4)], [number(capacity=1)], staffed_windows=[window(minutes=2, reps=4)]
    )
    assert result["existing_only_capacity"] == 1
    assert result["minimum_additional_numbers"] == 3
    assert result["existing_plus_addition_capacity"] == 4


def test_callback_slot_is_reserved_before_flexible_work():
    result = forecast(
        [
            lead("flexible"),
            lead(
                "callback",
                callback_due_at=NOW.isoformat(),
                callback_window_end=(NOW + timedelta(minutes=2)).isoformat(),
            ),
        ],
        [number()],
        staffed_windows=[window(minutes=4)],
    )
    schedule = result["existing_schedule"]
    assert schedule[0]["lead_id"] == "callback"
    assert schedule[1]["lead_id"] == "flexible"


def test_regional_number_is_used_before_shared_capacity_needed_elsewhere():
    result = forecast(
        [lead("a"), lead("b", region="212")],
        [number(daily_limit=1), number("regional", region="415", daily_limit=1)],
    )
    assert result["existing_only_capacity"] == 2
    assert result["minimum_additional_numbers"] == 0
    assert {i["lead_id"]: i["number_id"] for i in result["existing_schedule"]} == {
        "a": "regional",
        "b": "owned",
    }


def test_new_capacity_waits_for_declared_setup_delay():
    result = forecast([lead("one")], [], additional_ready_after_hours=24)
    assert result["existing_plus_addition_capacity"] == 0
    assert result["minimum_additional_numbers"] == 0
    assert result["setup_shortfall"] == 1


def test_approved_future_workload_limit_does_not_inflate_today():
    result = forecast(
        [lead(str(i)) for i in range(5)], [number(daily_limit=1, workload_schedule={"2026-10-20": 10})]
    )
    assert result["existing_only_capacity"] == 1


def test_a_due_retry_is_counted_once_until_its_outcome_creates_more_work():
    result = forecast([lead("retry", attempt_count=2)], [number()])
    # The configured allowance is exhausted; no extra forecast attempt is created.
    assert result["forecast_attempts"] == 0


def test_window_boundary_requires_time_for_the_whole_attempt():
    result = forecast([lead("one")], [number()], staffed_windows=[window(minutes=1)])
    assert result["existing_only_capacity"] == 0
    assert result["minimum_additional_numbers"] == 0


@pytest.mark.parametrize("bad", ["reversed", "naive", "overlap", "zero_reps"])
def test_invalid_staffed_windows_cannot_replace_existing_settings(workspace, bad):
    body = [window()]
    if bad == "reversed":
        body[0]["end_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif bad == "naive":
        body[0]["start_at"] = NOW.replace(tzinfo=None).isoformat()
    elif bad == "overlap":
        body.append(window(NOW + timedelta(minutes=1)))
    else:
        body[0]["reps"] = 0
    c, h = workspace["client"], workspace["headers"]
    assert c.put("/v1/admin/optimization", headers=h, json={"staffed_windows": body}).status_code == 422


def test_staffed_windows_persist_and_forecast_reads_real_binding(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    body = {"staffed_windows": [window(clock.now, minutes=60)], "additional_ready_after_hours": 24}
    response = c.put("/v1/admin/optimization", headers=h, json=body)
    assert response.status_code == 200, response.text
    assert (
        c.get("/v1/optimization", headers=h).json()["settings"]["staffed_windows"] == body["staffed_windows"]
    )
    with service.db.transaction() as s:
        leads = service.scoped(s, "leads", p)
        for item in leads:
            update(item, status="suppressed")
        target = leads[0]
        update(target, status="eligible", timezone="UTC", region="415", attempt_count=0)
        for inventory in service.scoped(s, "number_inventory", p):
            update(inventory, lifecycle="paused_for_outbound")
        owned_data = number("n", daily_limit=0)
        owned_data.pop("id")
        owned = put(
            s,
            "number_inventory",
            p["tenant_id"],
            p["seller_id"],
            {**owned_data, "phone": "+14155550199"},
        )
        put(
            s,
            "lead_number_bindings",
            p["tenant_id"],
            p["seller_id"],
            {"lead_id": target.id, "number_id": owned.id},
            key=target.id,
        )
        policy = get(s, "policy_versions", get(s, "campaigns", workspace["campaign_id"]).data["policy_id"])
        update(policy, max_attempts=1, local_start_hour=0, local_end_hour=24)
        result = service.forecast(s, p)
    assert result["existing_only_capacity"] == 0
    assert result["binding_shortfall"] == 1
    assert result["minimum_additional_numbers"] == 0


def test_small_time_assignments_match_an_independent_exhaustive_oracle():
    # Three actual start slots, two numbers, no assumptions shared with the planner.
    for due_offsets, limits in itertools.product([(0, 0, 0), (0, 1, 2), (0, 2, 2)], [(1, 1), (1, 2), (2, 1)]):
        leads = [
            lead(
                str(i),
                region="415" if i < 2 else "212",
                next_attempt_at=(NOW + timedelta(minutes=2 * due)).isoformat(),
            )
            for i, due in enumerate(due_offsets)
        ]
        numbers = [
            number("regional", region="415", daily_limit=limits[0]),
            number("shared", daily_limit=limits[1]),
        ]
        choices = [None, *itertools.product(range(3), range(2))]
        maximum = 0
        for trial in itertools.product(choices, repeat=3):
            assigned = [(i, value) for i, value in enumerate(trial) if value is not None]
            if any(
                slot < due_offsets[i] or (i == 2 and number_index == 0)
                for i, (slot, number_index) in assigned
            ):
                continue
            if len({slot for _, (slot, _) in assigned}) != len(assigned):
                continue
            if any(sum(number_index == n for _, (_, number_index) in assigned) > limits[n] for n in range(2)):
                continue
            maximum = max(maximum, len(assigned))
        result = forecast(leads, numbers, staffed_windows=[window(minutes=6)])
        assert result["existing_only_capacity"] == maximum


def test_existing_schedule_does_not_spend_more_than_declared_work_budget():
    # Simple staffing turns rep minutes into windows; declared windows supply their own time.
    result = capacity_forecast(
        [lead(str(i)) for i in range(5)],
        [number()],
        now=NOW,
        rep_minutes=4,
        handling_seconds=120,
        policy={"max_attempts": 1},
    )
    assert result["existing_only_capacity"] == 2
    declared = capacity_forecast(
        [lead(str(i)) for i in range(5)],
        [number()],
        now=NOW,
        rep_minutes=4,
        handling_seconds=120,
        policy={"max_attempts": 1},
        staffed_windows=[window()],
    )
    assert declared["existing_only_capacity"] == 5


def test_low_sample_health_is_not_used_as_spam_proof_or_a_capacity_ban():
    result = forecast([lead("cold")], [number(health={"samples": 0, "score": None})])
    assert result["existing_only_capacity"] == 1 and result["cold_start"]
    assert result["expected_conversion_lift"] is None


def test_declared_weekday_closures_are_respected():
    result = capacity_forecast(
        [lead("closed")],
        [number()],
        now=NOW,
        staffed_windows=[window()],
        policy={"max_attempts": 1, "closed_dates": ["2026-10-19"]},
    )
    assert result["existing_only_capacity"] == 0


def test_a_late_available_owned_number_does_not_disappear_when_reps_can_wait():
    result = forecast([lead("later")], [number(ready_at=(NOW + timedelta(minutes=30)).isoformat())])
    assert result["existing_only_capacity"] == 1
    assert datetime.fromisoformat(result["existing_schedule"][0]["at"]) >= NOW + timedelta(minutes=30)


def test_new_number_search_never_drops_an_achievable_callback_for_more_flexible_calls():
    # The callback needs the only shared number; both earlier flexible calls can use the regional DID.
    leads = [
        lead(
            "callback",
            region="212",
            callback_due_at=NOW.isoformat(),
            callback_window_end=(NOW + timedelta(minutes=2)).isoformat(),
        ),
        lead("one"),
        lead("two"),
    ]
    result = forecast(
        leads,
        [number("shared", daily_limit=1), number("regional", region="415", daily_limit=2)],
        staffed_windows=[window(minutes=6)],
    )
    assert result["existing_schedule"][0]["lead_id"] == "callback"
    assert result["existing_only_capacity"] == 3


def test_promised_callback_has_priority_even_when_skipping_it_would_fit_more_calls():
    leads = [
        lead(
            "callback",
            region="212",
            callback_due_at=NOW.isoformat(),
            callback_window_end=(NOW + timedelta(minutes=2)).isoformat(),
        ),
        lead("later", region="212", next_attempt_at=(NOW + timedelta(minutes=2)).isoformat()),
        lead("early", callback_window_end=(NOW + timedelta(minutes=2)).isoformat()),
    ]
    result = forecast(
        leads,
        [number("shared", daily_limit=1), number("regional", region="415", daily_limit=1)],
        staffed_windows=[window(minutes=4)],
    )
    assert {item["lead_id"] for item in result["existing_schedule"]} == {"callback"}
    assert result["minimum_additional_numbers"] == 1
    assert any(item["lead_id"] == "callback" for item in result["expanded_schedule"])


def test_repeated_time_windows_do_not_allocate_a_matrix_per_lead():
    from app.optimization.capacity import Assignment, work_items

    end = NOW + timedelta(hours=2)
    work, _ = work_items([lead(str(i)) for i in range(5000)], NOW, end, {"max_attempts": 1})
    slots = [(NOW + timedelta(minutes=2 * i), 20) for i in range(60)]
    tracemalloc.start()
    try:
        assignment = Assignment(work, slots, NOW, {}, 120, 1200)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert assignment.allowed and peak < 1024 * 1024, f"Time-window workspace used {peak} bytes"


def test_planning_budget_stops_search_without_claiming_a_minimum(monkeypatch):
    import app.optimization.capacity as capacity

    monkeypatch.setattr(capacity, "MAX_PLANNING_STEPS", 5, raising=False)
    result = forecast([lead(str(i)) for i in range(30)], [number(daily_limit=2)])
    assert result["planning_limited"]
    assert result["minimum_additional_numbers"] is None
    assert result["staffing_shortfall"] is None and result["number_shortfall"] is None
    assert result["monthly_rental_estimate_usd"] is None
    assert all(region["proposed_additional"] is None for region in result["regions"])
    assert result["planning_steps"] <= 6


def test_pending_capacity_is_shared_once_and_cannot_become_ready_capacity():
    result = forecast(
        [lead(f"{region}-{i}", region=region) for region in ("415", "212") for i in range(5)],
        [number("waiting", lifecycle="purchased", daily_limit=5)],
    )
    assert result["existing_only_capacity"] == 0
    assert sum(r["capacity_waiting_for_review"] for r in result["regions"]) == 5
    assert result["proposed_additional_numbers"] == 1


def test_pending_low_workload_cannot_displace_a_whole_high_capacity_addition():
    result = forecast(
        [lead(str(i)) for i in range(30)], [number("waiting", lifecycle="purchased", daily_limit=1)]
    )
    assert result["minimum_additional_numbers"] == 3
    assert result["proposed_additional_numbers"] == 3
