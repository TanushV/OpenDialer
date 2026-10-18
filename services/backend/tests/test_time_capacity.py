"""A14-A18: capacity must fit declared work times, identities and daily limits."""

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
        owned = put(
            s,
            "number_inventory",
            p["tenant_id"],
            p["seller_id"],
            {**number("n", daily_limit=0), "phone": "+14155550199"},
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
