"""A09/A30/A50: optional work must not block the authority's event loop on SQL."""

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from test_control_completion import principal


@contextmanager
def async_sql(app):
    found = []

    def inspected(connection, cursor, statement, *rest):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        found.append(statement.split()[0])

    event.listen(app.state.db.engine, "before_cursor_execute", inspected)
    try:
        yield found
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", inspected)


@pytest.mark.asyncio
async def test_advance_preparation_keeps_database_work_off_the_live_loop(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        lead = next(r for r in service.scoped(s, "leads", p) if r.data.get("domain"))
        lead_id = lead.id
    with async_sql(app) as queries:
        await service.prepare(p, [lead_id])
    assert service.packet(p, lead_id)["script"]["script_version"]
    assert not queries, f"Preparation ran {len(queries)} SQL statements on the live loop"


@pytest.mark.asyncio
async def test_restore_reconciliation_keeps_database_work_off_the_live_loop(workspace, app):
    service, p = app.state.service, principal(workspace)
    with async_sql(app) as queries:
        result = await service.reconcile_restore(p, "Reviewed synthetic empty authorization journal")
    assert result["reconciled"]
    assert not queries, f"Restore ran {len(queries)} SQL statements on the live loop"
