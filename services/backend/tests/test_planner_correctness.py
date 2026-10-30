"""W5b planner acceptance: forecasts are bounded, count-first and never gate a real call."""

import time
from datetime import timedelta

import pytest
from app.db import update
from app.optimization.numbering import capacity_forecast
from helpers import principal
from test_time_capacity import NOW, lead, number, window


def plan(leads, inventory, **overrides):
    options = dict(
        now=NOW,
        horizon_hours=24,
        rep_minutes=120,
        handling_seconds=120,
        daily_capacity_per_number=5,
        policy={"max_attempts": 1, "min_retry_seconds": 3600, "local_start_hour": 9, "local_end_hour": 20},
    )
    options.update(overrides)
    return capacity_forecast(leads, inventory, **options)


# Group 1: the planner never writes the field that authorizes a real call.


def test_forecast_never_rewrites_the_next_attempt_gate(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    due = (clock.now - timedelta(hours=1)).isoformat()
    with service.db.transaction() as s:
        rows = service.scoped(s, "leads", p)
        identity = rows[0].id
        update(
            rows[0],
            timezone="UTC",
            attempt_count=1,
            requested_contact=True,
            last_attempt_at=(clock.now - timedelta(hours=2)).isoformat(),
            next_attempt_at=due,
        )
    with service.db.transaction(False) as s:
        versions = {r.id: r.version for r in service.scoped(s, "leads", p)}
    assert service.build_forecast(p, workspace["campaign_id"])["status"] == "ready"
    with service.db.transaction(False) as s:
        row = service.required(s, "leads", identity, p)
        assert row.data["next_attempt_at"] == due
        assert {r.id: r.version for r in service.scoped(s, "leads", p)} == versions


def test_wrapup_cadence_makes_a_lead_due_without_any_forecast(workspace, app, clock, monkeypatch):
    import app.optimization.workflows as workflows
    from app.compliance.policy import authorize
    from conftest import start
    from helpers import finish

    def forbidden(*args, **kwargs):
        raise AssertionError("Cadence must not depend on a forecast")

    monkeypatch.setattr(workflows, "capacity_forecast", forbidden)
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call, outcome="no_answer")

    def reasons():
        with service.db.transaction(False) as s:
            row = service.required(s, "leads", call["lead_id"], p)
            number = service.required(s, "number_inventory", call["number_id"], p)
            return row.data, authorize(service.context(s, p, row, number)).reasons

    data, before = reasons()
    with service.db.transaction(False) as s:
        policy = service.required(
            s, "policy_versions", service.selected_campaign(s, p).data["policy_id"], p
        ).data
    wait = policy.get("min_retry_seconds", 3600)
    assert data["attempt_count"] == 1 and data["next_attempt_at"]
    assert "NOT_DUE" in before
    clock.now += timedelta(seconds=wait + 1)
    _, after = reasons()
    assert "NOT_DUE" not in after and "CADENCE_LIMIT" not in after


# Group 2: one bounded summary replaces persisted per-lead plans.


def test_forecast_publishes_one_bounded_summary_and_no_per_lead_plans(workspace, app, monkeypatch):
    from sqlalchemy import event

    service, p = app.state.service, principal(workspace)
    response = workspace["client"].post(
        "/v1/leads/import",
        headers=workspace["headers"],
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [
                {"name": f"Summary {i}", "phone": f"+141557{i:05d}", "company": "S", "timezone": "UTC"}
                for i in range(130)
            ],
        },
    )
    assert response.status_code == 200, response.text
    writes = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "scheduling_plans" in statement and not statement.lstrip().upper().startswith("SELECT"):
            writes.append(statement)

    event.listen(service.db.engine, "before_cursor_execute", capture)
    try:
        assert service.build_forecast(p, workspace["campaign_id"])["status"] == "ready"
    finally:
        event.remove(service.db.engine, "before_cursor_execute", capture)
    assert writes == [], "Forecasts no longer persist per-lead plans"
    with service.db.transaction(False) as s:
        saved = service._by_key(s, p, "report_projections", f"planning:{workspace['campaign_id']}:configured")
        summary = saved.data["report"]["next_attempts"]
    assert 0 < len(summary) <= 100
    assert len({row["lead_id"] for row in summary}) == len(summary)
    for row in summary:
        assert {"lead_id", "at", "rep_id", "number_id", "priority_class", "predicted_conversions"} <= set(row)
        assert "alternatives" not in row and "predictions" not in row
    report = service.optimization_report(p)
    assert report["future_slots"] and len(report["future_slots"]) <= 100
    assert all(slot["name"] and slot["reason"] for slot in report["future_slots"])
    assert len(report["plans"]) <= 100


def test_forecast_changed_during_calculation_publishes_as_of_without_plan_jobs(workspace, app, monkeypatch):
    import app.optimization.capacity as capacity
    import app.optimization.workflows as workflows
    from app.db import Job

    service, p = app.state.service, principal(workspace)
    calculate = workflows.capacity_forecast

    def changed(*args, **kwargs):
        result = calculate(*args, **kwargs)
        with service.db.transaction() as s:
            update(service.scoped(s, "leads", p)[0], outreach_hold={"kind": "human_booked"})
        return result

    monkeypatch.setattr(workflows, "capacity_forecast", changed)
    # Published with the watermark read before the calculation: shown stale, not discarded.
    result = service.build_forecast(p, workspace["campaign_id"])
    assert result["status"] == "ready"
    with service.db.transaction(False) as s:
        assert service.saved_forecast(s, p)["projection"]["status"] == "stale"
        keys = [job.key for job in s.query(Job).all()]
    assert not [k for k in keys if k.startswith(("retry-plans:", "planning-rebalance:"))]
    assert not hasattr(capacity, "plan_changed")
    assert not hasattr(service, "_publish_schedule_plans")
    assert not hasattr(service, "slot_predictor")


# Group 3: planner correctness.


def test_ten_thousand_work_items_complete_within_default_caps():
    days = [NOW + timedelta(days=d) for d in range(4)]
    leads = [lead(f"{i:05d}") for i in range(10000)]
    inventory = [number(f"n{i}", daily_limit=1000, capacity=10) for i in range(4)]
    started = time.monotonic()
    result = plan(
        leads,
        inventory,
        horizon_hours=96,
        staffed_windows=[window(day, minutes=600, reps=10) for day in days],
    )
    elapsed = time.monotonic() - started
    assert result["planning_limited"] is False, result["search_operations"]
    assert result["search_complete"] is True
    assert result["existing_only_capacity"] == 10000
    assert result["minimum_additional_numbers"] == 0
    assert result["search_operations"] <= 200000
    assert elapsed < 120


def test_live_call_occupies_rep_and_number_for_predicted_remaining_work(workspace, app, clock):
    from datetime import datetime

    from conftest import start

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction(False) as s:
        saved = service.required(s, "call_attempts", call["id"], p).data
    started = datetime.fromisoformat(saved.get("submitted_at") or saved["started_at"])
    clock.now += timedelta(seconds=30)
    with service.db.transaction(False) as s:
        inputs, _ = service.forecast_input(s, p, campaign_id=workspace["campaign_id"])
        work = service.optimization_config(s, p)["handling_seconds"]
    expected = started + timedelta(seconds=work)
    busy = datetime.fromisoformat(inputs["rep_busy_until"][p["user_id"]])
    assert abs((busy - expected).total_seconds()) < 1, "Live work ends at its predicted remaining time"
    occupied = next(n for n in inputs["inventory"] if n["id"] == call["number_id"])
    assert occupied["active_obligations"] == 1
    assert abs((datetime.fromisoformat(occupied["busy_until"]) - expected).total_seconds()) < 1


def test_number_obligation_frees_its_number_after_the_known_end():
    occupied = number(
        "busy", active_obligations=1, busy_until=(NOW + timedelta(minutes=2)).isoformat(), capacity=1
    )
    result = plan([lead(str(i)) for i in range(2)], [occupied], staffed_windows=[window(minutes=6)])
    assert result["existing_only_capacity"] == 2
    assert min(r["at"] for r in result["existing_schedule"]) == (NOW + timedelta(minutes=2)).isoformat()


def test_declared_windows_supply_staffed_time_instead_of_rep_minutes():
    result = plan(
        [lead(str(i)) for i in range(40)],
        [number(daily_limit=100)],
        rep_minutes=10,
        staffed_windows=[window(minutes=60)],
    )
    assert result["rep_capacity_attempts"] == 30
    assert result["existing_only_capacity"] == 30
    assert result["assumptions"]["staffed_rep_minutes"] == 60


def test_served_counts_come_before_predicted_conversions():
    # Both formats so the pre-change region-keyed estimates also prefer hour ten.
    values = {"0:9": 0.1, "0:10": 0.9, "0:9:general": 0.1, "0:10:general": 0.9}
    start = NOW + timedelta(minutes=58)
    # Nine buckets: larger than the old exact-search gate, so the greedy pass decides.
    result = plan(
        [lead("a", slot_estimates=values), lead("b", next_attempt_at=(NOW + timedelta(hours=1)).isoformat())],
        [number(daily_limit=10)],
        staffed_windows=[window(start - timedelta(seconds=30), minutes=4.5)],
    )
    assert result["existing_only_capacity"] == 2, "A better hour cannot cost another lead its attempt"
    pair = plan(
        [lead("a", slot_estimates=values), lead("b", slot_estimates=values)],
        [number(daily_limit=10)],
        staffed_windows=[window(start, minutes=4, reps=2)],
    )
    assert pair["existing_only_capacity"] == 2
    assert pair["minimum_additional_numbers"] == 0, "Equal served counts never justify another number"


def test_exact_search_runs_beyond_eight_grid_buckets_and_reports_its_scope():
    leads = [lead("short", predicted_work_seconds=30)] + [lead(f"long{i}") for i in range(5)]
    result = plan(leads, [number(daily_limit=10)], staffed_windows=[window(minutes=10)])
    assert result["existing_only_capacity"] == 5
    assert result["proven_on_declared_grid"] is True
    assert result["search_nodes"] > 0
    limited = plan(
        [lead(str(i)) for i in range(40)], [number(daily_limit=2)], staffed_windows=[window(minutes=600)]
    )
    assert limited["search_complete"] is True
    assert limited["minimum_additional_numbers"] is not None


def test_lower_bound_and_gap_are_never_measured_against_an_unproven_zero(monkeypatch):
    import app.optimization.capacity as capacity

    solve = capacity.Assignment.solve

    def inexact(self, inventory, **kwargs):
        rows, _ = solve(self, inventory, **kwargs)
        return rows, False

    monkeypatch.setattr(capacity.Assignment, "solve", inexact)
    result = plan([lead(str(i)) for i in range(12)], [number(daily_limit=2)], staffed_windows=[window()])
    assert result["minimum_additional_numbers"] is None
    assert result["lower_bound"] >= 1
    assert result["optimality_gap"] == result["best_found_additional_numbers"] - result["lower_bound"]


def test_regional_proposals_are_attributed_to_their_own_states():
    leads = [lead("a1", region="212"), lead("a2", region="212"), lead("b1", region="415")]
    result = plan(
        leads,
        [],
        staffed_windows=[window(minutes=10)],
        daily_capacity_per_number=2,
        policy={
            "max_attempts": 1,
            "proposed_number_classes": [
                {"id": "212", "region": "212", "daily_limit": 2, "capacity": 1, "quote_usd": 1},
                {"id": "415", "region": "415", "daily_limit": 2, "capacity": 1, "quote_usd": 1},
            ],
        },
    )
    by_region = {r["region"]: r["proposed_additional"] for r in result["regions"]}
    assert result["proposed_additional_numbers"] == 2
    assert by_region == {"NY": 1, "CA": 1}


@pytest.mark.parametrize("lifecycle", ["paused_for_outbound", "investigating"])
def test_held_numbers_are_not_assumed_healthy_in_the_conditional_schedule(lifecycle):
    result = plan([lead("one")], [number("held", lifecycle=lifecycle)], staffed_windows=[window(minutes=4)])
    assert result["conditional_pending_schedule"] == []
    assert result["proposed_additional_numbers"] == 1


def test_delivery_deterioration_is_a_drop_from_a_supported_reference():
    from app.optimization.numbering import assess_number_health

    def stats(failures, samples):
        return [{"delivery_failed": i < failures, "started_at": NOW.isoformat()} for i in range(samples)]

    baseline = {"as_of": (NOW + timedelta(minutes=1)).isoformat()}
    # Without a supported reference the drop is measured from complete delivery, and says so.
    alone = assess_number_health(stats(30, 100), baseline, None, {})
    assert alone["delivery_reference"]["source"] == "assumed_complete_delivery"
    # The same 30% failure rate is not a 20-point drop when comparable numbers fail at 25%.
    similar = assess_number_health(
        stats(30, 100), {**baseline, "delivery_reference": {"samples": 400, "failures": 100}}, None, {}
    )
    assert "delivery_deterioration" not in similar["reason_codes"]
    assert similar["delivery_reference"]["source"] == "other_numbers"
    worse = assess_number_health(
        stats(60, 100), {**baseline, "delivery_reference": {"samples": 400, "failures": 20}}, None, {}
    )
    assert "delivery_deterioration" in worse["reason_codes"] and worse["status"] == "investigating"


def test_slot_estimates_are_keyed_by_local_time_only(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        inputs, _ = service.forecast_input(s, p, campaign_id=workspace["campaign_id"])
    for row in inputs["leads"]:
        assert len(row["slot_estimates"]) == 168
        assert all(key.count(":") == 1 for key in row["slot_estimates"])
