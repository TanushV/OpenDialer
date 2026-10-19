"""A14-A18/A31/A33/A50-A51: deterministic complexity and capacity contracts."""

import math
import time
from datetime import timedelta

import pytest
from app.optimization.numbering import capacity_forecast
from conftest import completed_report, ready
from sqlalchemy import event


@pytest.mark.parametrize("regions,demand,capacity", [(2, 3, 10), (3, 4, 10), (4, 1, 8), (3, 10, 10)])
def test_minimum_additions_can_share_general_capacity_across_sparse_regions(clock, regions, demand, capacity):
    leads = [
        {"id": f"{r}:{i}", "region": str(400 + r), "timezone": "UTC", "status": "eligible"}
        for r in range(regions)
        for i in range(demand)
    ]
    at = clock.now.replace(hour=10)
    report = capacity_forecast(
        leads,
        [],
        now=at,
        rep_minutes=1000,
        staffed_windows=[
            {"start_at": at.isoformat(), "end_at": (at + timedelta(hours=8)).isoformat(), "reps": 1}
        ],
        policy={"max_attempts": 1},
        daily_capacity_per_number=capacity,
    )
    assert report["minimum_additional_numbers"] == math.ceil(regions * demand / capacity)
    assert report["optimality"] == "proven_small_case"
    assert report["existing_only_capacity"] == 0


def test_rep_bottleneck_does_not_propose_numbers_for_unstaffed_calls(clock):
    leads = [{"id": str(i), "region": "415", "timezone": "UTC", "status": "eligible"} for i in range(240)]
    inventory = [
        {"id": "regional", "region": "415", "lifecycle": "healthy", "daily_limit": 60},
        {"id": "general", "region": "general", "lifecycle": "healthy", "daily_limit": 80},
    ]
    result = capacity_forecast(
        leads,
        inventory,
        now=clock.now.replace(hour=10),
        rep_minutes=360,
        handling_seconds=120,
        policy={"max_attempts": 1},
        daily_capacity_per_number=50,
    )
    assert result["existing_only_capacity"] == 140
    assert result["minimum_additional_numbers"] == 1
    assert result["staffing_shortfall"] == 60
    assert result["number_shortfall"] == 40


def test_next_lead_query_count_does_not_grow_per_candidate(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    records = [
        {
            "name": f"Load {i}",
            "phone": f"+1415{5600000 + i:07d}",
            "company": "Load fixture",
            "timezone": "America/Los_Angeles",
        }
        for i in range(500)
    ]
    r = c.post("/v1/leads/import", headers=h, json={"campaign_id": workspace["campaign_id"], "rows": records})
    assert r.status_code == 200, r.text
    ready(workspace)
    queries = []

    def count(*args):
        queries.append(args[2])

    event.listen(app.state.db.engine, "before_cursor_execute", count)
    begun = time.perf_counter()
    try:
        response = c.post("/v1/queue/next", headers=h, json={"campaign_id": workspace["campaign_id"]})
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    assert len(queries) <= 30, {"queries": len(queries), "elapsed": time.perf_counter() - begun}


def test_cached_report_does_not_run_future_scheduling_on_view(workspace, app, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/admin/optimization/refresh", headers=h)
    completed_report(workspace)

    def forbidden(*args, **kwargs):
        raise AssertionError("Report GET recomputed an operational plan")

    monkeypatch.setattr("app.optimization.workflows.future_slots", forbidden)
    result = c.get("/v1/optimization", headers=h)
    assert result.status_code == 200, result.text
    assert result.json()["as_of"]
