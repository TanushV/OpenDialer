import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db import Base


def migrate_database(url):
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    engine = create_engine(url)
    try:
        existing = set(inspect(engine).get_table_names())
        if "tenants" in existing and "alembic_version" not in existing:
            # Adopt only the pre-migration development schema after checking every existing column.
            for table in Base.metadata.sorted_tables:
                if table.name not in existing:
                    continue
                actual = {c["name"] for c in inspect(engine).get_columns(table.name)}
                bootstrap_columns = {
                    name for name in table.columns.keys() if not name.startswith(("op_", "ctx_"))
                }
                if table.name == "jobs":
                    bootstrap_columns -= {"seller_id", "owner_id"}
                if actual not in (
                    set(table.columns.keys()),
                    bootstrap_columns,
                ):
                    raise RuntimeError(
                        "Unversioned schema differs from the known bootstrap schema; restore/review before migration"
                    )
            Base.metadata.create_all(engine)
            command.stamp(config, "0001_initial")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
