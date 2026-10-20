"""W5a: derived reports and learning never fence, lock or spin on the seller revision.

Source writes take the seller report-revision row last, at commit. Derived work
reads an as-of watermark and publishes with it; the next dirty refresh repairs
staleness. GETs read saved evidence only.
"""

import collections
import re
from contextlib import contextmanager
from datetime import timedelta

import pytest
from app.db import MODELS, Job, get, put, update
from conftest import completed_metrics, start
from sqlalchemy import event, select
from test_idle_authority import write_transactions
from test_optimization_workflows import finish
from test_projection_replay import dirty_jobs, project_pending
from test_remediation_privacy import principal


@contextmanager
def statements(service):
    """Every SQL statement plus COMMIT markers, in execution order."""
    seen = []

    def execute(connection, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    def commit(connection):
        seen.append("COMMIT")

    event.listen(service.db.engine, "before_cursor_execute", execute)
    event.listen(service.db.engine, "commit", commit)
    try:
        yield seen
    finally:
        event.remove(service.db.engine, "before_cursor_execute", execute)
        event.remove(service.db.engine, "commit", commit)


def write_transactions_in(seen, *, sqlite=True):
    """Write transactions in execution order.

    SQLite marks them with BEGIN IMMEDIATE. PostgreSQL begins implicitly, so every
    statement run between two COMMITs that contains a write is one write transaction.
    """
    result, current = [], None if sqlite else []
    for statement in seen:
        if sqlite and statement.startswith("BEGIN IMMEDIATE"):
            current = []
        elif statement == "COMMIT":
            if current is not None and (sqlite or any(WRITE.match(s) for s in current)):
                result.append(current)
            current = None if sqlite else []
        elif current is not None:
            current.append(statement)
    return result


WRITE = re.compile(r"^(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def revision(service, p):
    with service.db.transaction(False) as s:
        return service.report_revision(s, p)


# Lock order: a source write bumps the revision as its last statement.


def test_call_write_bumps_the_report_revision_as_its_last_write(workspace, app):
    service = app.state.service
    call = start(workspace)
    with statements(service) as seen:
        finish(workspace, call)
    bumped = [
        t
        for t in write_transactions_in(seen, sqlite=service.db.sqlite)
        if any("report_revisions" in s for s in t)
    ]
    assert bumped, "A disposition changes report inputs and must bump the seller revision"
    for transaction in bumped:
        writes = [s for s in transaction if WRITE.match(s)]
        assert "report_revisions" in writes[-1], writes[-3:]
        assert sum("report_revisions" in s for s in transaction) == 1
        # Dirty projection jobs are locked and written before the revision, in key order.
        jobs = [s for s in transaction if "FROM jobs" in s and "FOR UPDATE" in s.upper()]
        assert all(transaction.index(s) < transaction.index(writes[-1]) for s in jobs)


def test_projection_refresh_and_rebuild_never_touch_the_revision_row(workspace, app):
    from app.optimization.projections import rebuild_page

    service, p = app.state.service, principal(workspace)
    with statements(service) as seen:
        project_pending(service, p)
        for page in range(20):
            if rebuild_page(service, p, start=page == 0)["published"]:
                break
    transactions = write_transactions_in(seen, sqlite=service.db.sqlite)
    assert transactions, "The fixture must exercise derived writes"
    touched = [s for t in transactions for s in t if "report_revisions" in s]
    assert touched == [], touched[:3]


def test_rolled_back_savepoint_does_not_bump_the_revision(workspace, app):
    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    before = revision(service, p)
    with service.db.transaction() as s:
        savepoint = s.begin_nested()
        update(service.scoped(s, "leads", p)[0], segment="rolled-back")
        s.flush()
        savepoint.rollback()
    jobs = dirty_jobs(service, p)
    with service.db.transaction() as s:
        savepoint = s.begin_nested()
        update(service.scoped(s, "leads", p)[0], segment="rolled-back-again")
        s.flush()
        savepoint.rollback()
    assert revision(service, p) == before
    assert dirty_jobs(service, p) == jobs


# Narrow invalidation.


def test_heartbeat_only_work_interval_update_neither_bumps_nor_enqueues(workspace, app, clock):
    from conftest import ready

    service, p = app.state.service, principal(workspace)
    ready(workspace)
    heartbeat = lambda: workspace["client"].post(  # noqa: E731
        "/v1/rep/heartbeat", headers=workspace["headers"], json={"activity": "ready"}
    )
    assert heartbeat().status_code == 200
    project_pending(service, p)
    before, jobs = revision(service, p), dirty_jobs(service, p)
    clock.now += timedelta(seconds=30)
    assert heartbeat().status_code == 200
    with service.db.transaction(False) as s:
        interval = service.scoped(s, "work_intervals", p)[0]
        assert interval.data["heartbeat_at"] == clock.now.isoformat(), "The lease was extended"
    assert revision(service, p) == before
    assert dirty_jobs(service, p) == jobs


def test_derived_tables_do_not_bump_the_revision(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    before = revision(service, p)
    with service.db.transaction() as s:
        number = service.scoped(s, "number_inventory", p)[0]
        put(s, "number_health", p["tenant_id"], p["seller_id"], {"number_id": number.id}, now=clock.now)
        put(s, "learning_datasets", p["tenant_id"], p["seller_id"], {"rows": []}, now=clock.now)
    assert revision(service, p) == before


# GETs are reads.


def test_metric_and_workspace_reads_do_not_write_when_reports_are_stale(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    completed_metrics(workspace)
    with service.db.transaction() as s:
        update(service.scoped(s, "leads", p)[0], status="needs_review")
    client, headers = workspace["client"], workspace["headers"]
    with write_transactions(service, monkeypatch) as (writes, begins):
        for _ in range(3):
            metrics = client.get("/v1/metrics", headers=headers)
            home = client.get("/v1/workspace", headers=headers)
            assert metrics.status_code == home.status_code == 200
            assert metrics.json()["projection"]["status"] == "stale"
            assert home.json()["metrics"]["projection"]["status"] == "stale"
    assert writes == [] and begins == [], collections.Counter(writes)


@pytest.mark.asyncio
async def test_stale_home_metrics_are_repaired_by_the_dirty_source_chain(workspace, app):
    service, p = app.state.service, principal(workspace)
    first = completed_metrics(workspace)
    with service.db.transaction() as s:
        update(service.scoped(s, "leads", p)[0], status="needs_review")
    await service.run_jobs(p["tenant_id"], ["ml"])
    current = workspace["client"].get("/v1/metrics", headers=workspace["headers"]).json()
    assert current["projection"]["status"] == "ready"
    assert current["eligible_now"] == first["eligible_now"] - 1


# Derived refresh publishes; it does not spin on unrelated changes.


@pytest.mark.asyncio
async def test_persistently_changing_seller_does_not_respin_refresh_jobs(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    completed_metrics(workspace)
    original, writes = service.metrics, []

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(writes) < 4:
            # Calling continues while every derived view is calculated.
            with service.db.transaction() as s:
                update(service.scoped(s, "leads", p)[0], segment=f"busy-{len(writes)}")
            writes.append(1)
        return result

    monkeypatch.setattr(service, "metrics", changing)
    claim, claims = service.jobs.claim, collections.Counter()

    def counted(*args, **kwargs):
        job = claim(*args, **kwargs)
        if job and job["payload"].get("kind") in {"home_refresh", "report_refresh", "optimization_refresh"}:
            claims[job["key"]] += 1
        return job

    monkeypatch.setattr(service.jobs, "claim", counted)
    with service.db.transaction() as s:
        update(service.scoped(s, "leads", p)[0], segment="start")
    for _ in range(3):
        await service.run_jobs(p["tenant_id"], ["ml"])
    assert claims, "The fixture must run a derived refresh"
    # Every computation publishes; each source change causes at most one follow-up.
    assert max(claims.values()) <= len(writes) + 1, claims
    current = workspace["client"].get("/v1/metrics", headers=workspace["headers"]).json()
    assert current["projection"]["status"] == "ready"
    from app.jobs.queue import aware

    with service.db.transaction(False) as s:
        # A run scheduled at the next contribution boundary is not a retry loop.
        stuck = [
            j.key
            for j in s.scalars(select(Job).where(Job.queue == "ml", Job.status.in_(["queued", "running"])))
            if j.payload.get("kind") in {"home_refresh", "report_refresh"}
            and aware(j.available_at) <= service.clock()
        ]
    assert not stuck


# One poison unit is a per-unit error, not a seller-wide outage.


def test_poison_unit_is_reported_without_blocking_other_reports(workspace, app, clock):
    from app.optimization.projections import refresh_unit

    service, p = app.state.service, principal(workspace)
    before = completed_metrics(workspace)
    with service.db.transaction() as s:
        poison = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "call_authorization",
                "status": "settled",
                "billing_verified": True,
                "cost_usd": 0.01,
                "credited_usd": 0.05,
                "at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    payload = dirty_jobs(service, p)[f"projection:{p['seller_id']}:usage:{poison.id}"]["payload"]
    assert refresh_unit(service, p, payload)["status"] == "error"
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        update(lead, status="needs_review")
    after = completed_metrics(workspace)
    assert after["eligible_now"] == before["eligible_now"] - 1, "Other sources still publish"
    assert after["unit_errors"] == 1
    with service.db.transaction() as s:
        update(get(s, "usage_ledger", poison.id), credited_usd=0)
    project_pending(service, p)
    assert completed_metrics(workspace)["unit_errors"] == 0


def test_dead_source_job_does_not_block_metric_publication(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        job = s.scalar(select(Job).where(Job.key.startswith(f"projection:{p['seller_id']}:")))
        job.status, job.attempts = "dead", 3
    service.prepare_metric_read(p)
    assert completed_metrics(workspace)["projection"]["status"] == "ready"


# Learning reads one consistent snapshot and publishes it.


def test_snapshot_read_is_isolated_from_concurrent_commits(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    model = MODELS["leads"]

    def count(s):
        return len(list(s.scalars(select(model.id).where(model.seller_id == p["seller_id"]))))

    with service.db.snapshot() as s:
        first = count(s)
        with service.db.transaction() as w:
            put(w, "leads", p["tenant_id"], p["seller_id"], {"name": "Concurrent"}, now=clock.now)
        assert count(s) == first
    with service.db.transaction(False) as s:
        assert count(s) == first + 1


def test_learning_refresh_completes_while_calls_write_during_its_read(workspace, app, monkeypatch):
    import app.optimization.workflows as workflows

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    original, interleaved = workflows.training_rows, []

    def calling(*args, **kwargs):
        rows = original(*args, **kwargs)
        with service.db.transaction() as s:
            update(get(s, "call_attempts", call["id"]), outcome="gatekeeper")
            update(service.scoped(s, "leads", p)[0], segment="during-learning")
        interleaved.append(1)
        return rows

    monkeypatch.setattr(workflows, "training_rows", calling)
    before = revision(service, p)
    result = service.refresh_learning(p)
    assert interleaved and revision(service, p) > before
    assert result["status"] == "ready", result
    with service.db.transaction(False) as s:
        dataset = get(s, "learning_datasets", result["dataset_id"])
        assert dataset.data["as_of"] and dataset.data["source_revision"] == before
        saved = next(r for r in dataset.data["rows"] if r["call_id"] == call["id"])
    assert saved["outcome"] == "right_party", "The dataset is the as-of snapshot, not a mixed read"


def test_training_job_carries_the_dataset_identity_not_its_rows(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    rows = [{"human": i % 2, "call_id": str(i)} for i in range(200)]
    dataset = {
        "id": "dataset-id",
        "rows": rows,
        "goal": "meeting_held",
        "checksum": "c" * 64,
        "baseline": {},
        "window_days": 14,
    }
    monkeypatch.setattr("app.ml.training.training_support", lambda data: None)
    queued = service.train_operational(p, dataset)
    assert queued["status"] == "queued"
    with service.db.transaction(False) as s:
        payload = s.get(Job, queued["job_id"]).payload
    assert "rows" not in payload
    assert payload["source_dataset_id"] == "dataset-id" and payload["input_fingerprint"] == "c" * 64


def test_dataset_byte_bound_truncates_honestly_instead_of_failing(workspace, app, monkeypatch):
    import app.optimization.workflows as workflows

    service, p = app.state.service, principal(workspace)
    finish(workspace, start(workspace))

    def rows(calls, *args, **kwargs):
        return [
            {"call_id": f"row-{i}", "at": f"2026-10-0{i + 1}T00:00:00+00:00", "features": {"pad": "x" * 200}}
            for i in range(5)
        ]

    monkeypatch.setattr(workflows, "training_rows", rows)
    with service.db.transaction(False) as s:
        complete = service.operational_dataset(s, p)
    assert len(complete) == 5 and complete.truncation is None
    monkeypatch.setattr(workflows, "TRAINING_SNAPSHOT_MAX_BYTES", 700)
    with service.db.transaction(False) as s:
        bounded = service.operational_dataset(s, p)
    assert 1 <= len(bounded) < 5
    assert bounded.truncation["reason"] == "byte_bound"
    assert bounded.truncation["omitted_rows"] == 5 - len(bounded)
    kept = sorted(r["at"] for r in bounded)
    assert kept == sorted(r["at"] for r in complete)[-len(kept) :], "The newest evidence is kept"


# Lifecycle.


def test_lead_change_no_longer_queues_retired_retry_plans(workspace, app):
    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    with service.db.transaction() as s:
        update(service.scoped(s, "leads", p)[0], status="needs_review")
    project_pending(service, p)
    with service.db.transaction(False) as s:
        kinds = {j.payload.get("kind") for j in s.scalars(select(Job))}
    assert "retry_plan_refresh" not in kinds


def test_health_thresholds_are_read_time_policy_not_a_projection_definition():
    from app.optimization.numbering import HealthPolicy
    from app.optimization.projections import definition_version

    base = {"conversion_goal": "meeting_held", "window_days": 14}
    policy = HealthPolicy().model_dump()
    first = definition_version({**base, "health_policy": policy}, "c:e")
    edited = {**policy, "min_comparable": 50, "material_drop": 0.3, "confidence": 0.9}
    assert definition_version({**base, "health_policy": edited}, "c:e") == first
    assert definition_version({**base, "health_policy": {**policy, "window_days": 30}}, "c:e") != first


def test_superseded_and_obsolete_namespaces_are_scheduled_for_cleanup(workspace, app):
    from app.optimization import projections

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    first = projections.rebuild_page(service, p, start=True)
    with service.db.transaction(False) as s:
        abandoned = service.selected_campaign(s, p).data["metric_projection"]["building_namespace"]
    with service.db.transaction() as s:
        campaign = service.selected_campaign(s, p)
        update(campaign, optimization={**campaign.data.get("optimization", {}), "window_days": 21})
    for page in range(20):
        if projections.rebuild_page(service, p, start=page == 0)["published"]:
            break
    assert not first["published"]
    with service.db.transaction(False) as s:
        pointer = service.selected_campaign(s, p).data["metric_projection"]
        cleanups = [
            j.payload
            for j in s.scalars(select(Job).where(Job.key.startswith("metric-cleanup:")))
            if j.status == "queued"
        ]
    namespaces = {c["namespace"] for c in cleanups}
    assert abandoned in namespaces, "An abandoned building namespace must not leak"
    assert "initial" in namespaces, "A retired older definition is removed as well"
    for payload in cleanups:
        while projections.cleanup_page(service, p, payload)["status"] == "pending":
            pass
    with service.db.transaction(False) as s:
        left = {
            r.external_key.split(":", 2)[1]
            for table in ("metric_contributions", "metric_buckets")
            for r in service.scoped(s, table, p)
        }
    assert left == {pointer["active_namespace"]}


def test_unique_conversion_counts_only_events_inside_the_outcome_window(clock):
    from app.optimization.projections import lead_values

    now = clock.now
    config = {"conversion_goal": "meeting_held", "window_days": 14}
    lead = {"id": "lead", "status": "eligible", "originally_eligible": True}
    attempt = {"lead_id": "lead", "started_at": (now - timedelta(days=30)).isoformat()}

    def event(days_after):
        at = (now - timedelta(days=30) + timedelta(days=days_after)).isoformat()
        return {
            "id": "event",
            "lead_id": "lead",
            "kind": "meeting_held",
            "status": "confirmed",
            "occurred_at": at,
            "label_available_at": at,
        }

    inside = lead_values(lead, [attempt], [event(13)], suppressed=False, config=config, now=now)
    late = lead_values(lead, [attempt], [event(15)], suppressed=False, config=config, now=now)
    unattempted = lead_values(lead, [], [event(1)], suppressed=False, config=config, now=now)
    assert inside["unique_conversions"] == 1
    assert late["unique_conversions"] == 0, "Training ignores this event; so must the cohort metric"
    assert unattempted["unique_conversions"] == 0
    assert late["held_meetings"] == 1, "Held meetings stay a descriptive count"


def test_dead_code_is_removed():
    import app.measurement as measurement
    from app.reporting import ReportProjections

    assert not hasattr(measurement, "connect_health")
    assert not hasattr(measurement.Measurement, "work_in")
    assert "lock" not in ReportProjections.report_revision.__code__.co_varnames
