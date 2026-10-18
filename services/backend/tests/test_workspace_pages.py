"""A45/A50/A51: refresh never reloads history; every omitted row stays reachable."""

from datetime import timedelta

import pytest
from app.db import ActiveClaim, Job, get, put, update
from conftest import start
from sqlalchemy import inspect, select
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


def snapshot(w):
    response = w["client"].get("/v1/workspace", headers=w["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def page(w, section, **params):
    response = w["client"].get(f"/v1/workspace/{section}", headers=w["headers"], params=params)
    assert response.status_code == 200, response.text
    return response.json()


def fill(w, app, clock, count=130):
    service, p = app.state.service, principal(w)
    with service.db.transaction() as s:
        original = service.scoped(s, "leads", p)[0]
        values = dict(original.data)
        ids = []
        for index in range(count):
            lead = put(
                s,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                {
                    **values,
                    "phone": f"+1212555{index:04d}",
                    "name": f"History {index}",
                    "company": "Archive Co",
                    "attempt_count": 1,
                },
                now=clock.now + timedelta(seconds=1),
            )
            ids.append(lead.id)
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "rep_id": p["user_id"],
                    "state": "FINALIZED",
                    "outcome": "right_party",
                    "started_at": clock.now.isoformat(),
                },
                now=clock.now + timedelta(seconds=1),
            )
            put(
                s,
                "followup_tasks",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "owner_id": p["user_id"],
                    "status": "completed",
                    "action": "Earlier task",
                    "due_at": clock.now.isoformat(),
                },
                now=clock.now + timedelta(seconds=1),
            )
        return ids


def forbidden(*args, **kwargs):
    raise AssertionError("Ordinary workspace view performed full history/integrity work")


def test_workspace_refresh_never_computes_history_or_full_integrity(workspace, app, monkeypatch):
    monkeypatch.setattr(app.state.service, "metrics", forbidden)
    monkeypatch.setattr(app.state.db, "integrity", forbidden)
    result = snapshot(workspace)
    assert result["metrics"]["projection"]["status"] == "pending"
    assert result["metrics"].get("original_cohort") is None
    assert result["readiness"]["integrity_ok"] is None
    assert result["readiness"]["checks_scope"] == "operational"


def test_workspace_decrypts_a_bounded_preview_and_discloses_more(workspace, app, clock, monkeypatch):
    fill(workspace, app, clock, 400)
    reads = count_decrypted(app, monkeypatch)
    result = snapshot(workspace)
    assert len(reads) <= 260, len(reads)
    for section in ("leads", "calls", "tasks"):
        assert len(result[section]) <= 50
        assert result["pages"][section]["next_cursor"]
        assert result["pages"][section]["total"] >= 400


def test_keyset_pages_reach_all_rows_with_timestamp_ties(workspace, app, clock):
    fill(workspace, app, clock)
    # Compare against the actual scoped inventory, independent of preview order.
    with app.state.db.transaction(False) as s:
        expected = {r.id for r in app.state.service.scoped(s, "leads", principal(workspace))}
    seen, cursor = [], None
    for _ in range(10):
        result = page(workspace, "leads", **({"cursor": cursor} if cursor else {}))
        seen += [r["id"] for r in result["items"]]
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert not cursor
    assert len(seen) == len(set(seen))
    assert set(seen) == expected


def test_search_scans_bounded_chunks_and_does_not_claim_missing_until_exhausted(
    workspace, app, clock, monkeypatch
):
    fill(workspace, app, clock, 450)
    with app.state.db.transaction() as s:
        values = dict(app.state.service.scoped(s, "leads", principal(workspace))[0].data)
        target = put(
            s,
            "leads",
            workspace["tenant_id"],
            workspace["seller_id"],
            {**values, "name": "Rare Needle"},
            now=clock.now + timedelta(days=1),
        ).id
    reads = count_decrypted(app, monkeypatch)
    first = page(workspace, "leads", q="needle")
    assert first["items"] == []
    assert first["next_cursor"]
    assert first["search_complete"] is False
    assert len(reads) <= 220
    cursor, found = first["next_cursor"], []
    for _ in range(10):
        result = page(workspace, "leads", q="needle", cursor=cursor)
        found.extend(result["items"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert not cursor
    assert [r["id"] for r in found] == [target]


def test_suppression_checks_only_the_preview_endpoints(workspace, app, clock, monkeypatch):
    fill(workspace, app, clock, 70)
    p = principal(workspace)
    with app.state.db.transaction() as s:
        lead = app.state.service.scoped(s, "leads", p)[0]
        target = lead.id
        put(
            s,
            "suppressions",
            p["tenant_id"],
            p["seller_id"],
            {"phone": lead.data["phone"], "active": True},
            key=lead.data["phone"],
            now=clock.now,
        )
        for i in range(600):
            phone = f"+1310555{i:04d}"
            put(
                s,
                "suppressions",
                p["tenant_id"],
                p["seller_id"],
                {"phone": phone, "active": True},
                key=phone,
                now=clock.now,
            )
    reads = count_decrypted(app, monkeypatch)
    result = page(workspace, "leads")
    assert next(lead for lead in result["items"] if lead["id"] == target)["suppressed"]
    assert len(reads) <= 65, len(reads)


def test_old_active_call_and_unfinished_wrapup_survive_history_preview(workspace, app, clock):
    call = start(workspace)
    fill(workspace, app, clock)
    assert call["id"] in {c["id"] for c in snapshot(workspace)["calls"]}
    with app.state.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), state="ENDED")
    assert call["id"] in {c["id"] for c in snapshot(workspace)["calls"]}


def test_own_inbound_obligation_survives_closed_history(workspace, app, clock):
    p = principal(workspace)
    with app.state.db.transaction() as s:
        task = put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "inbound": True,
                "inbound_state": "connected",
                "status": "completed",
                "routed_rep_id": p["user_id"],
            },
            now=clock.now - timedelta(days=1),
        )
        identity = task.id
        s.add(
            ActiveClaim(
                id="inbound-test",
                tenant_id=p["tenant_id"],
                seller_id=p["seller_id"],
                endpoint_id=task.id,
                lead_id=task.id,
                rep_id=p["user_id"],
                number_id="inbound-number",
                reservation_id=task.id,
                call_id=task.id,
                expires_at=clock.now + timedelta(minutes=10),
            )
        )
    fill(workspace, app, clock)
    assert identity in {t["id"] for t in snapshot(workspace)["tasks"]}


def test_call_pages_join_lead_labels_without_loading_the_list(workspace, app, clock, monkeypatch):
    fill(workspace, app, clock, 400)
    reads = count_decrypted(app, monkeypatch)
    result = page(workspace, "calls")
    assert all(c["lead_name"].startswith("History") for c in result["items"])
    assert all(c["lead_company"] == "Archive Co" for c in result["items"])
    assert len(reads) <= 110, len(reads)


def test_rep_call_pages_and_cursors_cannot_expose_another_rep(workspace, app, clock):
    p = principal(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "users", p["user_id"]), role="rep")
        other = put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {"rep_id": "different-rep", "state": "FINALIZED", "outcome": "right_party"},
            now=clock.now,
        )
        identity = other.id
    assert page(workspace, "calls")["items"] == []
    assert (
        workspace["client"]
        .get("/v1/workspace/calls", headers=workspace["headers"], params={"cursor": identity})
        .status_code
        == 404
    )


def test_workspace_hides_foreign_jobs_raw_errors_and_registry_members(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    service.jobs.enqueue(
        "research",
        {"seller_id": "another-seller", "secret": "foreign-payload"},
        "foreign",
        tenant_id=p["tenant_id"],
    )
    job = service.jobs.enqueue(
        "research", {"seller_id": p["seller_id"], "user_id": p["user_id"]}, "own", tenant_id=p["tenant_id"]
    )
    with service.db.transaction() as s:
        s.get(Job, job["id"]).error = "private transcript in old error"
        put(
            s,
            "registry_snapshots",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "prepaid",
                "phone": "+12125550199",
                "listed_numbers": ["+12125550198"],
                "payload": "RAW-VENDOR-BODY",
                "status": "clear",
            },
            now=clock.now,
        )
    result = snapshot(workspace)
    assert "private transcript" not in str(result)
    assert "RAW-VENDOR-BODY" not in str(result)
    assert all(
        j["id"] != job["id"] or j["error"] == "Job failed; inspect recovery status" for j in result["jobs"]
    )
    assert all(j["id"] != "foreign" for j in result["jobs"])
    assert len(result["jobs"]) == 1


@pytest.mark.asyncio
async def test_home_metrics_are_saved_invalidated_and_rebuilt_without_planning(workspace, app, monkeypatch):
    service = app.state.service
    monkeypatch.setattr(service, "forecast", forbidden)
    first = snapshot(workspace)
    assert first["metrics"]["projection"]["status"] == "pending"
    await service.run_jobs(queues=["ml"])
    before = snapshot(workspace)["metrics"]
    assert before["projection"]["status"] == "ready"
    assert before["original_cohort"] == 3
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", principal(workspace))[0]
        update(lead, status="needs_review")
    stale = snapshot(workspace)["metrics"]
    assert stale["projection"]["status"] == "stale"
    assert stale["eligible_now"] == before["eligible_now"]
    await service.run_jobs(queues=["ml"])
    after = snapshot(workspace)["metrics"]
    assert after["eligible_now"] == before["eligible_now"] - 1
    with monkeypatch.context() as m:
        m.setattr(service, "metrics", forbidden)
        assert snapshot(workspace)["metrics"] == after


def test_page_indexes_exist_for_scoped_ordered_reads(app):
    indexes = {i["name"] for i in inspect(app.state.db.engine).get_indexes("call_attempts")}
    assert {"ix_call_attempts_page", "ix_call_attempts_owner_page"} <= indexes


def test_invalid_page_parameters_and_deleted_cursor_require_reload(workspace, app, clock):
    for section, params in (("secrets", {}), ("leads", {"q": "a" * 201}), ("calls", {"filter": "made-up"})):
        assert workspace["client"].get(
            f"/v1/workspace/{section}", headers=workspace["headers"], params=params
        ).status_code in {404, 422}
    assert (
        workspace["client"]
        .get("/v1/workspace/leads", headers=workspace["headers"], params={"cursor": "deleted-id"})
        .status_code
        == 404
    )


def test_foreign_job_history_cannot_starve_scoped_pages(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    own = service.jobs.enqueue(
        "research",
        {"seller_id": p["seller_id"], "user_id": p["user_id"]},
        "own-last",
        tenant_id=p["tenant_id"],
    )
    for i in range(100):
        clock.now += timedelta(seconds=1)
        service.jobs.enqueue(
            "research",
            {"seller_id": "foreign-seller", "user_id": "foreign-rep"},
            f"foreign-{i}",
            tenant_id=p["tenant_id"],
        )
    reads = count_decrypted(app, monkeypatch)
    result = page(workspace, "jobs")
    assert [j["id"] for j in result["items"]] == [own["id"]]
    assert result["next_cursor"] is None
    assert len(reads) < 10


@pytest.mark.asyncio
async def test_time_only_metric_expiry_can_refresh_after_one_minute(workspace, app, clock):
    snapshot(workspace)
    await app.state.service.run_jobs(queues=["ml"])
    previous = snapshot(workspace)["metrics"]
    clock.now += timedelta(seconds=61)
    assert snapshot(workspace)["metrics"]["projection"]["status"] == "stale"
    await app.state.service.run_jobs(queues=["ml"])
    refreshed = snapshot(workspace)["metrics"]
    assert refreshed["projection"]["status"] == "ready"
    assert refreshed["as_of"] != previous["as_of"]


def test_job_scope_backfill_preserves_identity_content_and_status(workspace, app):
    from app.db import Database

    service, p = app.state.service, principal(workspace)
    job = service.jobs.enqueue(
        "research",
        {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": []},
        "legacy-page-job",
        tenant_id=p["tenant_id"],
    )
    columns = {c["name"] for c in inspect(service.db.engine).get_columns("jobs")}
    assert {"seller_id", "owner_id"} <= columns
    with service.db.transaction() as s:
        row = s.get(Job, job["id"])
        row.seller_id, row.owner_id = None, None
    reopened = Database(service.settings.database_url, service.settings.secret_key)
    try:
        with reopened.transaction(False) as s:
            row = s.get(Job, job["id"])
            assert row.payload == job["payload"]
            assert row.status == job["status"]
            assert row.seller_id == p["seller_id"]
            assert row.owner_id == p["user_id"]
    finally:
        reopened.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_source_changes_do_not_exhaust_or_delay_metric_refresh(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    original = service.metrics

    def changing(current):
        result = original(current)
        with service.db.transaction() as s:
            lead = service.scoped(s, "leads", p)[0]
            update(lead, name="Another corrected name")
        return result

    snapshot(workspace)
    with monkeypatch.context() as m:
        m.setattr(service, "metrics", changing)
        for _ in range(4):
            await service.run_jobs(queues=["ml"])
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "ml")))
        assert len(jobs) == 1
        assert jobs[0].status == "queued"
        assert jobs[0].attempts == 0
        assert jobs[0].error is None
    await service.run_jobs(queues=["ml"])
    assert snapshot(workspace)["metrics"]["projection"]["status"] == "ready"


def test_unknown_task_status_is_not_hidden_as_if_closed(workspace, app, clock):
    with app.state.db.transaction() as s:
        task = put(
            s,
            "followup_tasks",
            workspace["tenant_id"],
            workspace["seller_id"],
            {"status": None, "action": "Legacy obligation", "due_at": clock.now.isoformat()},
            now=clock.now,
        )
        identity = task.id
    assert identity in {t["id"] for t in page(workspace, "tasks", filter="open")["items"]}


def test_old_open_followup_is_visible_before_recent_closed_history(workspace, app, clock):
    with app.state.db.transaction() as s:
        task = put(
            s,
            "followup_tasks",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "owner_id": workspace["user_id"],
                "action": "Keep the promised callback",
                "status": "open",
                "due_at": clock.now.isoformat(),
            },
            now=clock.now - timedelta(days=10),
        )
        identity = task.id
    fill(workspace, app, clock, 100)
    result = snapshot(workspace)
    assert identity in {t["id"] for t in result["tasks"]}
