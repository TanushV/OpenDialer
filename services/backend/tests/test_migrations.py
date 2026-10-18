from app.migrate import migrate_database
from sqlalchemy import create_engine, inspect


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
            == "0014_current_knowledge"
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
