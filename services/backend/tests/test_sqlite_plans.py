"""Fixed SQLite statistics steer subset reads to their partial indexes, whatever the index creation order."""

import pytest
from app.platform.db import MODELS, plan_statistics
from sqlalchemy import create_engine, literal_column, select


@pytest.mark.parametrize("reverse", [False, True])
def test_open_calls_read_uses_the_partial_index(reverse):
    # Index creation order follows set iteration, which changes with each process's hash seed.
    # This tests the planner, so index DDL need not sync a journal to disk on the CI runner.
    engine = create_engine("sqlite://")
    table = MODELS["call_attempts"].__table__
    indexes = sorted(table.indexes, key=lambda index: index.name, reverse=reverse)
    model = MODELS["call_attempts"]
    query = (
        select(model.id)
        .where(
            model.tenant_id == "t",
            model.seller_id == "s",
            model.op_owner_id == "u",
            model.op_status != literal_column("'FINALIZED'"),
        )
        .order_by(model.created_at.desc(), model.id.desc())
        .limit(50)
    )
    compiled = query.compile(engine)
    with engine.begin() as connection:
        table.create(connection)
        for index in indexes:
            index.drop(connection)
        for index in indexes:
            index.create(connection)
        plan_statistics(connection)
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + str(compiled), tuple(compiled.params.values())
        )
        assert "ix_call_attempts_open_owner_page" in str(plan.fetchall())
