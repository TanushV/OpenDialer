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


@pytest.mark.asyncio
async def test_generated_template_settles_usage_without_sql_on_live_loop(workspace, app, monkeypatch):
    from app.providers.inference import InferenceResult
    from test_preparation_completion import configure

    configure(workspace)
    service, p = app.state.service, principal(workspace)
    lead_id = workspace["lead_ids"][0]

    class Provider:
        async def complete(self, prompt, context, schema):
            fact = context["facts"][0]
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": fact["statement"], "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                "off-loop-script",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with async_sql(app) as queries:
        await service.prepare(p, [lead_id])
    assert service.packet(p, lead_id)["script"]["fallback"] is False
    with service.db.transaction(False) as s:
        assert any(r.data.get("cost_usd") == 0.001 for r in service.scoped(s, "usage_ledger", p))
    assert not queries, f"Generated preparation ran {len(queries)} SQL statements on the live loop"


@pytest.mark.asyncio
async def test_research_provider_publishes_and_settles_off_the_live_loop(workspace, app, monkeypatch):
    from app.providers.inference import InferenceResult
    from app.research.fetch import SafeFetcher

    service, p = app.state.service, principal(workspace)
    service.settings.simulation = False

    async def fetch(self, url):
        return {"url": url, "title": "Fixture company", "text": "We repair commercial roofs."}

    class Provider:
        async def complete(self, prompt, context, schema):
            source = context["sources"][0]
            return InferenceResult(
                {
                    "facts": [
                        {"statement": source["text"], "quote": source["text"], "source_id": source["id"]}
                    ],
                    "unknowns": [],
                },
                {"cost": 0.001},
                "off-loop-research",
            )

    monkeypatch.setattr(SafeFetcher, "fetch", fetch)
    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with async_sql(app) as queries:
        await service.research_company(p, "roofing.example", {"company": "Fixture company"})
    with service.db.transaction(False) as s:
        assert any(r.data["domain"] == "roofing.example" for r in service.scoped(s, "research_facts", p))
    assert not queries, f"Research ran {len(queries)} SQL statements on the live loop"
