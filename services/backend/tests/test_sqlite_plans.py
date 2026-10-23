"""Fixed SQLite statistics steer subset reads to their partial indexes, whatever the index creation order."""

import pytest
from app.db import MODELS, plan_statistics
from sqlalchemy import create_engine, literal_column, select


@pytest.mark.parametrize("reverse", [False, True])
def test_open_calls_read_uses_the_partial_index(tmp_path, reverse):
    # Index creation order follows set iteration, which changes with each process's hash seed.
    engine = create_engine(f"sqlite:///{tmp_path}/plans.db")
    table = MODELS["call_attempts"].__table__
    indexes = sorted(table.indexes, key=lambda index: index.name, reverse=reverse)
    table.create(engine)
    for index in indexes:
        index.drop(engine)
    for index in indexes:
        index.create(engine)
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
        plan_statistics(connection)
    with engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + str(compiled), tuple(compiled.params.values())
        )
        assert "ix_call_attempts_open_owner_page" in str(plan.fetchall())
