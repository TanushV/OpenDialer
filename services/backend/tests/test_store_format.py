"""A 0.2/0.3 store is refused with a plain message instead of opening half-readable."""

import pytest
from app.db import Database, OldStore
from sqlalchemy import create_engine, text


def test_a_0_3_store_is_refused_and_left_unchanged(tmp_path):
    url = f"sqlite:///{tmp_path}/opendialer.db"
    with create_engine(url).begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0019_lead_queue_order')"))
    with pytest.raises(OldStore, match="0.2 or 0.3"):
        Database(url)
    with create_engine(url).connect() as connection:
        tables = connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).scalars()
        assert set(tables) == {"alembic_version"}
