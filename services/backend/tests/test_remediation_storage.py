"""Typed hot-path and migration contracts written before the storage repair."""

from app.db import MODELS, Database, doc, get, put, update
from app.migrate import migrate_database
from conftest import reserve
from sqlalchemy import inspect, select


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
