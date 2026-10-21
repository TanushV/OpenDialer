"""Typed hot-path and migration contracts written before the storage repair."""

import pytest
from app.db import MODELS, Database, doc, get, put, update
from app.migrate import migrate_database
from conftest import reserve
from sqlalchemy import inspect, select

pytestmark = pytest.mark.database


def test_hot_operational_columns_follow_mutations_without_exposing_phone(tmp_path, clock):
    db = Database(f"sqlite:///{tmp_path}/indexed.db", encryption_key="test-index-key")
    with db.transaction() as s:
        row = put(
            s,
            "leads",
            "tenant",
            "seller",
            {
                "status": "eligible",
                "campaign_id": "campaign",
                "endpoint_id": "endpoint",
                "phone": "+14155550101",
                "next_attempt_at": clock.now.isoformat(),
            },
        )
        identifier = row.id
    assert {"op_status", "op_campaign_id", "op_due_at"} <= {
        c["name"] for c in inspect(db.engine).get_columns("leads")
    }
    with db.transaction() as s:
        lead = get(s, "leads", identifier)
        update(lead, status="converted")
    with db.transaction(False) as s:
        model = MODELS["leads"]
        result = s.scalar(
            select(model).where(model.op_status == "converted", model.op_campaign_id == "campaign")
        )
        assert result.id == identifier
        assert result.op_endpoint_id == "endpoint"
    db.engine.dispose()


def test_real_old_schema_backfills_in_place_and_is_repeatable(tmp_path, clock):
    import json
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path}/old.db"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0002_optimization")
    engine = create_engine(url)
    with engine.begin() as s:
        s.execute(
            text(
                "INSERT INTO leads (id,tenant_id,seller_id,data,version,created_at) VALUES ('saved','t','s',:data,7,:at)"
            ),
            {
                "data": json.dumps(
                    {"status": "eligible", "campaign_id": "retained", "phone": "+14155550101"}
                ),
                "at": clock.now,
            },
        )
    engine.dispose()
    for _ in range(2):
        migrate_database(url)
        db = Database(url, encryption_key="test-index-key")
        with db.transaction(False) as s:
            row = get(s, "leads", "saved")
            assert row.op_status == "eligible" and row.op_campaign_id == "retained"
            assert row.version == 7 and doc(row)["phone"] == "+14155550101"
        db.engine.dispose()


def test_queue_never_reconstructs_the_complete_training_dataset(workspace, app, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Queue selection loaded the complete training history")

    monkeypatch.setattr(app.state.service, "operational_dataset", forbidden)
    assert reserve(workspace).status_code == 200


def test_competing_updates_cannot_silently_overwrite_a_newer_record(workspace, app):
    import pytest
    from sqlalchemy.orm.exc import StaleDataError

    db = app.state.db
    with db.transaction(False) as first, db.transaction(False) as second:
        a = get(
            first,
            "leads",
            workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"],
        )
        b = get(second, "leads", a.id)
        update(a, status="needs_review")
        first.commit()
        update(b, status="eligible")
        with pytest.raises(StaleDataError):
            second.flush()
        second.rollback()


def next_after_scan(workspace, limit=5):
    """Follow retryable scan continuations, as a client must, within a fixed bound."""
    for _ in range(limit):
        result = workspace["client"].post(
            "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
        )
        detail = result.json().get("detail") if result.status_code == 409 else None
        if not (isinstance(detail, dict) and detail.get("code") == "QUEUE_SCAN_CONTINUES"):
            return result
    raise AssertionError("Queue scan did not finish within the bounded continuations")


def test_denied_first_page_does_not_hide_eligible_work(workspace, app, clock):
    from datetime import timedelta

    from app.db import rows
    from conftest import ready

    ready(workspace)
    service = app.state.service
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")}
    with service.db.transaction() as s:
        leads = rows(s, "leads", p["tenant_id"], p["seller_id"])
        allowed = next(lead for lead in leads if lead.data["region"] == "415")
        update(allowed, coverage_due_at=(clock.now + timedelta(days=1)).isoformat())
        for lead in leads:
            if lead.id != allowed.id:
                update(lead, outreach_hold=True)
        for i in range(280):
            put(
                s,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                {
                    **allowed.data,
                    "name": f"Held candidate {i}",
                    "outreach_hold": True,
                    "coverage_due_at": (clock.now - timedelta(days=1)).isoformat(),
                },
            )
        allowed_id = allowed.id
    # Each request reads at most three 32-lead pages; a retryable continuation resumes from the
    # saved cursor, so 280 denied leads ahead of it cannot hide the eligible one.
    result = next_after_scan(workspace)
    assert result.status_code == 200, result.text
    assert result.json()["lead"]["id"] == allowed_id


def test_candidate_changed_before_claim_retries_bounded_selection(workspace, app, monkeypatch):
    from conftest import ready

    ready(workspace)
    service = app.state.service
    selection = service._selection
    changed = []

    def race(p, campaign_id, *args, **kwargs):
        plan = selection(p, campaign_id, *args, **kwargs)
        if not changed:
            identifier = plan["selection"]["lead_id"]
            with service.db.transaction() as s:
                update(get(s, "leads", identifier), status="needs_review")
            changed.append(identifier)
        return plan

    monkeypatch.setattr(service, "_selection", race)
    result = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["lead"]["id"] not in changed


def test_bounded_scan_reports_exhaustion_after_all_pages_are_denied(workspace, app, clock):
    from app.db import rows
    from conftest import ready

    ready(workspace)
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")}
    with app.state.db.transaction() as s:
        leads = rows(s, "leads", p["tenant_id"], p["seller_id"])
        for lead in leads:
            update(lead, outreach_hold=True)
        for i in range(280):
            put(s, "leads", p["tenant_id"], p["seller_id"], {**leads[0].data, "name": str(i)})
    result = next_after_scan(workspace)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "NO_ELIGIBLE_LEADS"


def test_concurrent_reps_do_not_race_for_the_lead_another_rep_is_claiming(workspace, app, monkeypatch):
    """Eight reps ranked the same top candidate: one claim won and the rest re-selected repeatedly."""
    import threading

    from app.db import put
    from test_control_completion import principal

    from scripts import measure_completion as measure

    service, p = app.state.service, principal(workspace)
    measure.workload_capacity(service, p, 2)
    with service.db.transaction() as s:
        user = put(
            s,
            "users",
            p["tenant_id"],
            p["seller_id"],
            {"role": "rep", "email": "second-rep@example.test", "disabled": False},
            now=service.clock(),
        )
    second = {**p, "user_id": user.id, "role": "rep"}
    for who in (p, second):
        service.presence(
            who,
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "synthetic-" + who["user_id"],
            },
        )
    original, planned, release, picks = service._claim_selection, threading.Event(), threading.Event(), {}

    def claim(who, campaign_id, plan):
        if who["user_id"] == p["user_id"] and "first_plan" not in picks:
            picks["first_plan"] = plan["selection"]["lead_id"]
            planned.set()
            release.wait(10)
        return original(who, campaign_id, plan)

    monkeypatch.setattr(service, "_claim_selection", claim)
    first = threading.Thread(
        target=lambda: picks.setdefault("first", service.reserve(p, workspace["campaign_id"]))
    )
    first.start()
    assert planned.wait(10)
    try:
        taken = service.reserve(second, workspace["campaign_id"])
    finally:
        release.set()
        first.join(10)
    assert taken["lead"]["id"] != picks["first_plan"], "The second rep skips a lead already being claimed"
    assert picks["first"]["lead"]["id"] == picks["first_plan"], "The first rep keeps its pick"
