import pytest
from app.migrate import migrate_database
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.database


def test_additive_optimization_upgrade_preserves_existing_encrypted_records(tmp_path):
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path}/legacy.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0001_initial")
    engine = create_engine(url)
    before = {}
    with engine.begin() as connection:
        for table in ("leads", "notes", "suppressions", "sessions"):
            payload = json.dumps(
                {"ciphertext": "opaque-saved-" + table, "human_text": "Original protected correction"}
            )
            connection.execute(
                text(
                    f"INSERT INTO {table} (id,tenant_id,seller_id,external_key,data,version,created_at) VALUES (:id,:tenant,:seller,:key,:data,2,:at)"  # noqa: S608 - fixed migration table allowlist
                ),
                {
                    "id": table + "-saved",
                    "tenant": "t",
                    "seller": "s",
                    "key": "identity",
                    "data": payload,
                    "at": datetime.now(UTC),
                },
            )
            before[table] = connection.execute(
                text("SELECT id,tenant_id,seller_id,external_key,data,version,created_at FROM " + table)  # noqa: S608 - fixed migration table allowlist
            ).all()
    migrate_database(url)
    with engine.connect() as connection:
        for table, expected in before.items():
            assert (
                connection.execute(
                    text("SELECT id,tenant_id,seller_id,external_key,data,version,created_at FROM " + table)  # noqa: S608 - fixed migration table allowlist
                ).all()
                == expected
            )
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0018_job_due_probe"
        )
    migrate_database(url)
    engine.dispose()


def test_migration_builds_both_core_constraints_and_version_stamp(tmp_path):
    url = f"sqlite:///{tmp_path}/migration.db"
    migrate_database(url)
    inspector = inspect(create_engine(url))
    assert "alembic_version" in inspector.get_table_names()
    assert {
        "active_claims",
        "command_outbox",
        "call_attempts",
        "webhook_inbox",
        "transcript_revisions",
    }.issubset(inspector.get_table_names())
    assert len(inspector.get_unique_constraints("active_claims")) >= 4
    migrate_database(url)


def test_fresh_migration_builds_the_due_job_probe_index(tmp_path):
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path}/fresh.db"
    migrate_database(url)
    engine = create_engine(url)
    try:
        indexes = {i["name"]: i["column_names"] for i in inspect(engine).get_indexes("jobs")}
        assert indexes.get("ix_jobs_due") == ["queue", "status", "available_at", "priority"]
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0018_job_due_probe"
    finally:
        engine.dispose()


def test_retained_0017_store_upgrades_and_0018_store_skips_reinitialization(tmp_path, monkeypatch):
    import app.db as storage
    from app.config import Settings
    from app.db import database_cipher, get, put, startup_contract_current
    from app.main import create_app
    from sqlalchemy import text

    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite:///" + str(tmp_path / "authority.db"),
        secret_key="synthetic-0018-upgrade",
        local_token="synthetic-token",
        simulation=True,
        worker_enabled=False,
    )
    cipher = database_cipher(settings.secret_key)
    first = create_app(settings)
    with first.state.db.transaction() as s:
        put(s, "notes", "tenant", "seller", {"human_text": "Keep this rep note"}, object_id="human")
    engine = first.state.db.engine
    assert startup_contract_current(engine, cipher)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='0017_metric_projections'"))
        connection.execute(text('DROP INDEX IF EXISTS "ix_jobs_due"'))
    engine.dispose()
    retained = create_engine(settings.database_url)
    try:
        assert not startup_contract_current(retained, cipher), (
            "A completed 0017 cutover must still receive the 0018 schema"
        )
    finally:
        retained.dispose()
    upgraded = create_app(settings)
    try:
        engine = upgraded.state.db.engine
        assert "ix_jobs_due" in {i["name"] for i in inspect(engine).get_indexes("jobs")}
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0018_job_due_probe"
        assert startup_contract_current(engine, cipher)
        with upgraded.state.db.transaction(False) as s:
            assert get(s, "notes", "human").data["human_text"] == "Keep this rep note"
    finally:
        upgraded.state.db.engine.dispose()

    def forbidden(*args, **kwargs):
        raise AssertionError("A current 0018 store must not rescan or reinitialize")

    monkeypatch.setattr(storage, "authenticate_store", forbidden)
    monkeypatch.setattr(storage, "ensure_columns", forbidden)
    reopened = create_app(settings)
    reopened.state.db.engine.dispose()
