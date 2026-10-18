"""A01: adopt the exact legacy bootstrap without dropping pending work."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.migrate import migrate_database
from sqlalchemy import create_engine, inspect, text


def bootstrap(tmp_path):
    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path}/bootstrap.db"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0001_initial")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(
            text("""
            INSERT INTO jobs
            (id,tenant_id,queue,key,payload,status,priority,attempts,max_attempts,available_at)
            VALUES ('saved-job','tenant','research','saved-key','{}','queued',4,1,3,'2026-10-14')
        """)
        )
    return url, engine


def test_legacy_bootstrap_job_is_preserved_through_scoped_metadata_upgrade(tmp_path):
    url, engine = bootstrap(tmp_path)
    try:
        with engine.connect() as connection:
            before = connection.execute(text("SELECT * FROM jobs")).mappings().one()
        migrate_database(url)
        migrate_database(url)
        with engine.connect() as connection:
            after = connection.execute(text("SELECT * FROM jobs")).mappings().one()
        assert {k: after[k] for k in before} == dict(before)
        assert {"seller_id", "owner_id"} <= after.keys()
        assert "ctx_key" in {c["name"] for c in inspect(engine).get_columns("model_versions")}
    finally:
        engine.dispose()


def test_unknown_bootstrap_shape_stays_rejected_without_stamping_or_mutation(tmp_path):
    url, engine = bootstrap(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE jobs ADD COLUMN unexpected_scope TEXT"))
        with pytest.raises(RuntimeError, match="Unversioned schema differs"):
            migrate_database(url)
        assert "alembic_version" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT attempts FROM jobs WHERE id='saved-job'")).scalar() == 1
    finally:
        engine.dispose()
