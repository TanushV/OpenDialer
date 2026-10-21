"""Keyset pages seek to their cursor instead of rescanning the history before it.

The 100k-lead/1M-attempt profile's paginated rebuild slowed page by page: SQLite ran
`created_at > ? OR (created_at = ? AND id > ?)` as a scan from the start of the seller's
index range. A row-value comparison seeks the same index directly to the cursor."""

from types import SimpleNamespace

import pytest
from app.db import MODELS, uid
from app.workspace import after
from sqlalchemy import insert, select

ROWS = 3000


def history(service, p, clock):
    model = MODELS["leads"]
    with service.db.transaction() as s:
        s.execute(
            insert(model),
            [
                {
                    "id": uid(),
                    "tenant_id": p["tenant_id"],
                    "seller_id": p["seller_id"],
                    "version": 1,
                    "data": {},
                    "created_at": clock.now,
                }
                for _ in range(ROWS)
            ],
        )
    with service.db.transaction(False) as s:
        rows = list(s.execute(select(model.created_at, model.id).order_by(model.created_at, model.id)))
    return model, rows[-10]


def steps(service, query):
    count = [0]
    with service.db.transaction(False) as s:
        connection = s.connection().connection.driver_connection

        def tick():
            count[0] += 1
            return 0

        connection.set_progress_handler(tick, 1)
        try:
            found = list(s.execute(query))
        finally:
            connection.set_progress_handler(None, 0)
    return count[0], found


@pytest.mark.sqlite_only
@pytest.mark.parametrize("descending", [False, True])
def test_a_late_cursor_page_does_constant_work(workspace, app, clock, descending):
    from test_control_completion import principal

    service, p = app.state.service, principal(workspace)
    model, (at, anchor) = history(service, p, clock)
    if descending:
        _, rows = steps(
            service, select(model.created_at, model.id).order_by(model.created_at, model.id).limit(10)
        )
        at, anchor = rows[-1]
    order = (model.created_at.desc(), model.id.desc()) if descending else (model.created_at, model.id)
    query = (
        select(model.id)
        .where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        .where(after(model, SimpleNamespace(created_at=at, id=anchor), descending))
        .order_by(*order)
        .limit(5)
    )
    work, found = steps(service, query)
    assert len(found) == 5
    assert work < 600, f"{work} VM steps: the page rescanned history before its cursor"
