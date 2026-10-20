"""A14-A18/A31/A33/A50-A51: deterministic complexity and capacity contracts."""

import math
import time
from datetime import timedelta

import pytest
from app.optimization.numbering import capacity_forecast
from conftest import completed_report, ready
from sqlalchemy import event


@pytest.mark.parametrize("completion", ["read", "commit", "rollback", "callback_failure"])
def test_closed_transaction_releases_decrypted_cache_without_waiting_for_gc(workspace, app, completion):
    import weakref
    from contextlib import nullcontext

    from app.db import get, prefetch, update

    service = app.state.service
    principal = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")}
    expected = (
        pytest.raises(ValueError, match="synthetic boundary failure")
        if completion in {"rollback", "callback_failure"}
        else nullcontext()
    )
    callbacks = []

    def after_transaction():
        # Recovery is allowed to start a new transaction after writer admission releases.
        with service.db.transaction(False) as recovery:
            callbacks.append(get(recovery, "campaigns", workspace["campaign_id"]).id)
        if completion == "callback_failure":
            raise ValueError("synthetic boundary failure")

    with expected, service.db.transaction(completion != "read") as session:
        prefetch(session, principal, {"campaigns": []})
        campaign = get(session, "campaigns", workspace["campaign_id"])
        if completion != "read":
            update(campaign, optimization={"handling_seconds": 125})
            session.flush()  # Includes the report invalidation transaction reference.
            assert session.info["report_revision_transaction"] is session.get_transaction()
        payload = weakref.ref(campaign.data)
        del campaign
        session.info["after_transaction"] = [after_transaction]
        if completion == "rollback":
            raise ValueError("synthetic boundary failure")
    assert callbacks == [workspace["campaign_id"]]
    # Keep the closed session itself reachable, as exception traces/SQLAlchemy cycles can.
    # No raw/decrypted request cache may survive the application's ownership boundary.
    assert payload() is None, "Closed transaction retained decrypted context until cyclic GC"
    assert session.info == {}


def test_caller_owned_transaction_keeps_cache_until_its_outer_boundary(workspace, app):
    from app.db import prefetch

    service = app.state.service
    principal = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")}
    with service.db.transaction() as outer:
        prefetch(outer, principal, {"campaigns": []})
        with service.db.transaction(session=outer) as nested:
            assert nested is outer
        assert outer.info["row_cache"]
        assert outer.in_transaction()
    assert outer.info == {}


@pytest.mark.sqlite_only
def test_sqlite_writers_wait_before_begin_without_blocking_reads_or_another_store(app, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.db import Database
    from sqlalchemy import text

    db = app.state.db
    other = Database(f"sqlite:///{tmp_path}/independent.db", "synthetic-independent-" + "a" * 40)
    held, release, attempted, started = (threading.Event() for _ in range(4))

    def begin(conn, cursor, statement, parameters, context, many):
        if statement == "BEGIN IMMEDIATE" and held.is_set():
            attempted.set()

    def holder():
        with db.transaction():
            held.set()
            assert release.wait(5)

    def contender():
        started.set()
        with db.transaction() as session:
            return session.scalar(text("SELECT 1"))

    def independent(database, write):
        with database.transaction(write) as session:
            return session.scalar(text("SELECT 1"))

    event.listen(db.engine, "before_cursor_execute", begin)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(holder)
            assert held.wait(2)
            second = pool.submit(contender)
            try:
                assert started.wait(2)
                assert pool.submit(independent, db, False).result(timeout=2) == 1
                assert pool.submit(independent, other, True).result(timeout=2) == 1
                assert not attempted.wait(0.1), "Competing local writers entered SQLite's coarse busy backoff"
            finally:
                release.set()
                first.result(timeout=2)
                assert second.result(timeout=2) == 1
    finally:
        event.remove(db.engine, "before_cursor_execute", begin)
        other.engine.dispose()


@pytest.mark.parametrize("failure", ["body", "commit"])
@pytest.mark.sqlite_only
def test_sqlite_writer_admission_releases_after_failure_and_before_callbacks(app, failure):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    db = app.state.db
    callbacks = []

    def callback():
        with db.transaction() as session:
            callbacks.append(session.scalar(text("SELECT 1")))

    def fail_commit(session):
        if session.info.pop("fail_commit", False):
            raise ValueError("commit interrupted")

    def recover():
        with pytest.raises(ValueError, match="interrupted"):
            with db.transaction() as session:
                session.info["after_transaction"] = [callback]
                if failure == "body":
                    raise ValueError("body interrupted")
                session.info["fail_commit"] = True
        with db.transaction() as session:
            return session.scalar(text("SELECT 1"))

    event.listen(Session, "before_commit", fail_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(recover).result(timeout=3) == 1
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert callbacks == [1]


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


def load_leads(workspace, count, **fields):
    c, h = workspace["client"], workspace["headers"]
    records = [
        {
            "name": f"Load {i}",
            "phone": f"+1415{5600000 + i:07d}",
            "company": "Load fixture",
            "timezone": "America/Los_Angeles",
            **fields,
        }
        for i in range(count)
    ]
    r = c.post("/v1/leads/import", headers=h, json={"campaign_id": workspace["campaign_id"], "rows": records})
    assert r.status_code == 200, r.text
    return r.json()["leads"]


def measured_selection(workspace, app, monkeypatch):
    from test_context_reads import count_decrypted

    c, h = workspace["client"], workspace["headers"]
    ready(workspace)
    queries = []

    def count(*args):
        queries.append(args[2])

    decrypted = count_decrypted(app, monkeypatch)
    event.listen(app.state.db.engine, "before_cursor_execute", count)
    try:
        response = c.post("/v1/queue/next", headers=h, json={"campaign_id": workspace["campaign_id"]})
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    return response.json(), len(queries), len(decrypted)


def test_next_lead_decrypts_a_bounded_top_k_not_the_whole_page(workspace, app, monkeypatch):
    load_leads(workspace, 500)
    _, statements, decrypted = measured_selection(workspace, app, monkeypatch)
    assert statements <= 30, statements
    assert decrypted <= 130, decrypted  # 794 before: 256 leads, 256 endpoints, then context


def test_next_lead_page_query_is_served_in_index_order(workspace, app):
    from sqlalchemy import text

    load_leads(workspace, 50)
    with app.state.db.engine.connect() as connection:
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM leads WHERE tenant_id=:t AND seller_id=:s AND "
                "op_campaign_id=:c AND op_status='eligible' AND (op_due_at <= :n OR op_due_at IS NULL) "
                "ORDER BY op_priority DESC, op_coverage_at, id LIMIT 32"
            ),
            {
                "t": workspace["tenant_id"],
                "s": workspace["seller_id"],
                "c": workspace["campaign_id"],
                "n": "9999",
            },
        ).all()
    details = " ".join(str(row[-1]) for row in plan)
    assert "ix_leads_queue" in details and "TEMP B-TREE" not in details, details


def test_due_callback_outranks_every_coverage_lead_beyond_the_top_k(workspace, app, monkeypatch, clock):
    from app.db import get, update

    leads = load_leads(workspace, 120)
    last = leads[-1]["id"]
    with app.state.db.transaction() as s:
        update(
            get(s, "leads", last),
            callback_due_at=clock.now.isoformat(),
            callback_owner_id=workspace["user_id"],
            coverage_due_at=(clock.now + timedelta(days=30)).isoformat(),
        )
    packet, _, _ = measured_selection(workspace, app, monkeypatch)
    assert packet["lead"]["id"] == last


def test_denied_head_of_queue_does_not_starve_later_eligible_leads(workspace, app, monkeypatch, clock):
    from app.db import get, update

    leads = load_leads(workspace, 80)
    with app.state.db.transaction() as s:
        for lead in leads[:70]:
            update(get(s, "leads", lead["id"]), timezone=None, timezone_confidence="unknown")
        for lead in leads[70:]:
            update(get(s, "leads", lead["id"]), coverage_due_at=(clock.now + timedelta(days=9)).isoformat())
    packet, _, _ = measured_selection(workspace, app, monkeypatch)
    assert packet["lead"]["id"] not in {lead["id"] for lead in leads[:70]}


def test_cached_report_does_not_run_future_scheduling_on_view(workspace, app, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/admin/optimization/refresh", headers=h)
    completed_report(workspace)

    def forbidden(*args, **kwargs):
        raise AssertionError("Report GET recomputed an operational plan")

    monkeypatch.setattr("app.optimization.workflows.capacity_forecast", forbidden)
    result = c.get("/v1/optimization", headers=h)
    assert result.status_code == 200, result.text
    assert result.json()["as_of"]
