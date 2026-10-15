from app.migrate import migrate_database
from sqlalchemy import create_engine, inspect


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
