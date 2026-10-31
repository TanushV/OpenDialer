"""Agent threads: tool rules, close-out, publication, plan limits and the per-list budget."""

import json

import pytest
from app.preparation.agent import SimulatedAgent, ThreadWork
from app.preparation.templates import parse_markdown

SCRIPT = """# Opening
Hi {first_name}, this is {rep} with {seller}. {{ why now, from research }}

# Screening
{rep} from {seller}, about roof care.

# Discovery
- When was the roof last checked?
- Who handles repairs?

# Offers
## Inspection
A free inspection.

## Plan
A maintenance plan.

# Close
Would Tuesday work?
"""

PAGE = {
    "url": "https://hilltop.example.test/news",
    "title": "Hilltop Apartments news",
    "text": "Hilltop Apartments added two buildings on Elm Street this spring. Call us today.",
}


def leads():
    return [
        {
            "id": "lead-a",
            "row": {"name": "Dana Reyes", "company": "Hilltop Apartments", "city": "Fresno"},
            "name": "Dana Reyes",
            "company": "Hilltop Apartments",
            "domain": "hilltop.example.test",
        },
        {
            "id": "lead-b",
            "row": {"name": "Lee Park", "company": "Oak Storage"},
            "name": "Lee Park",
            "company": "Oak Storage",
            "domain": "",
        },
    ]


async def fetch(url):
    if url == PAGE["url"]:
        return PAGE
    if "other" in url:
        return {"url": url, "title": "Other", "text": "A different company entirely sells roofs here."}
    raise OSError("not found")


def call(work, name, **args):
    import asyncio

    return json.loads(asyncio.run(work.handle(name, args)))


def test_facts_need_a_readable_page_an_exact_quote_and_the_right_company():
    work = ThreadWork(leads(), parse_markdown(SCRIPT), fetch)
    good = {
        "statement": "Hilltop Apartments added two buildings on Elm Street.",
        "source_url": PAGE["url"],
        "quote": "added two buildings on Elm Street",
        "stability": "stable",
    }
    result = call(
        work,
        "save_facts",
        lead="lead-1",
        facts=[
            good,
            {**good, "quote": "added three buildings"},
            {**good, "source_url": "https://missing.example.test/"},
            {**good, "source_url": "https://other.example.test/", "quote": "sells roofs here"},
            {**good, "statement": "short"},
        ],
    )
    assert result["saved"] == ["lead-1/f1"] and not result["ok"]
    assert any("quote is not on" in e for e in result["errors"])
    assert any("could not be read" in e for e in result["errors"])
    assert any("does not name this lead" in e for e in result["errors"])
    assert any("statement 10-300" in e for e in result["errors"])
    assert call(work, "save_facts", lead="lead-9", facts=[good])["errors"][0].startswith("Unknown lead")


def test_scripts_close_out_only_after_research_and_with_valid_submissions():
    work = ThreadWork(leads(), parse_markdown(SCRIPT), fetch)
    picks = [{"section": "offers", "ids": ["offers-2"]}, {"section": "discovery", "ids": ["discovery-2"]}]
    written = [
        {"block": "ai-1", "text": "You just added two buildings on Elm Street.", "fact_ids": ["lead-1/f1"]}
    ]
    early = call(work, "submit_script", lead="lead-1", picks=picks, written=written)
    assert early["errors"] == ["Call finish_research for lead-1 first"]
    call(
        work,
        "save_facts",
        lead="lead-1",
        facts=[
            {
                "statement": "Hilltop Apartments added two buildings on Elm Street.",
                "source_url": PAGE["url"],
                "quote": "added two buildings on Elm Street",
                "stability": "stable",
            }
        ],
    )
    assert call(work, "finish_research", lead="lead-1", summary="Expanding landlord")["ok"]
    wrong = call(
        work,
        "submit_script",
        lead="lead-1",
        picks=[{"section": "offers", "ids": ["offers-7"]}, {"section": "discovery", "ids": ["discovery-1"]}],
        written=[{"block": "ai-1", "text": "Huge growth of 300%.", "fact_ids": ["lead-1/f1"]}],
    )
    assert not wrong["ok"] and any("offers-7" in e for e in wrong["errors"])
    assert any("'300%'" in e for e in wrong["errors"])
    assert work.follow_up().startswith("These leads are not closed yet: lead-1, lead-2")
    accepted = call(work, "submit_script", lead="lead-1", picks=picks, written=written)
    assert accepted == {"ok": True, "accepted": "lead-1", "still_open": ["lead-2"]}
    assert call(work, "submit_script", lead="lead-1", picks=picks, written=written)["errors"] == [
        "lead-1 is already closed"
    ]
    assert not call(work, "skip_lead", lead="lead-2", reason="no")["ok"]
    assert call(work, "skip_lead", lead="lead-2", reason="The business closed in 2024.")["ok"]
    assert work.remaining() == [] and work.follow_up() is None
    assert work.rejections == 4 and work.tool_calls == 8


@pytest.mark.asyncio
async def test_simulated_agent_uses_the_same_rules():
    from app.preparation.agent import simulated_page

    rows = [lead["row"] for lead in leads()]
    work = ThreadWork(leads(), parse_markdown(SCRIPT), simulated_page(rows))
    await SimulatedAgent().run("", [], work.handle, json.dumps(work.context()), work.follow_up)
    assert sorted(work.done) == ["lead-1", "lead-2"] and not work.skipped
    assert work.done["lead-1"]["written"]["ai-1"]["fact_ids"] == ["lead-1/f1"]


ROWS = [
    {
        "name": "Dana Reyes",
        "phone": "+14155559101",
        "company": "Hilltop Apartments",
        "domain": "hilltop.example.test",
    },
    {"name": "Lee Park", "phone": "+14155559102", "company": "Oak Storage", "units": "40"},
]


class ScriptedAgent:
    """A metered fake model that researches through the real fetcher and writes one script."""

    billing = "openai"

    def __init__(self, behaviour="normal"):
        self.behaviour = behaviour

    async def run(self, instructions, tools, handler, first, follow_up, **_):
        from app.providers.codex import PlanLimit

        if self.behaviour == "limit":
            raise PlanLimit("limit")
        context = json.loads(first)
        first_lead, second = (lead["lead"] for lead in context["leads"])
        saved = json.loads(
            await handler(
                "save_facts",
                {
                    "lead": first_lead,
                    "facts": [
                        {
                            "statement": "Hilltop Apartments added two buildings on Elm Street.",
                            "source_url": "https://hilltop.example.test/news",
                            "quote": "added two buildings on Elm Street",
                            "stability": "stable",
                        },
                        {
                            "statement": "Hilltop has 900 units.",
                            "source_url": "https://hilltop.example.test/news",
                            "quote": "900 units",
                            "stability": "stable",
                        },
                    ],
                },
            )
        )
        assert saved["saved"] == [f"{first_lead}/f1"] and "quote is not on" in saved["errors"][0]
        await handler("finish_research", {"lead": first_lead, "summary": "Growing landlord."})
        picks = [
            {"section": s["section"], "ids": [o["id"] for o in s["options"]][:1]}
            for s in context["script"]
            if s["pick"] and s["pick"] != "order all"
        ]
        written = [
            {
                "block": "ai-1",
                "text": "You just added two buildings on Elm Street.",
                "fact_ids": [f"{first_lead}/f1"],
            }
        ]
        assert json.loads(
            await handler("submit_script", {"lead": first_lead, "picks": picks, "written": written})
        )["ok"]
        if self.behaviour == "partial":
            return {"input_tokens": 100, "output_tokens": 10}
        await handler("finish_research", {"lead": second, "summary": "Nothing found."})
        await handler("skip_lead", {"lead": second, "reason": "No public trace of this business."})
        return {"input_tokens": 100, "output_tokens": 10}


def prepared_workspace(workspace, app, monkeypatch, agent):
    from unittest.mock import AsyncMock

    import app.research.fetch as fetch
    import httpx
    from helpers import principal

    monkeypatch.setattr(fetch, "public_address", AsyncMock(return_value="93.184.215.14"))
    real = httpx.AsyncClient

    def handler(request):
        if request.headers["host"] == "hilltop.example.test" and request.url.path == "/news":
            return httpx.Response(
                200,
                text="<title>Hilltop Apartments</title><p>Hilltop Apartments added two buildings on Elm Street.</p>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        fetch.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)
    )
    service, who = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "agent_route", lambda p: agent)
    monkeypatch.setattr(service, "inference", lambda *args, **kwargs: None)
    ids = [
        lead["id"]
        for lead in service.import_leads(
            who, {"campaign_id": workspace["campaign_id"], "rows": ROWS, "list_name": "Real", "prepare": True}
        )["leads"]
    ]
    return service, who, ids


@pytest.mark.asyncio
async def test_a_metered_thread_verifies_pages_publishes_and_charges_its_list(workspace, app, monkeypatch):
    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    result = await service.run_prep_thread(who, ids)
    assert result["prepared"] == 1 and result["rejections"] == 1
    script, state = service_script(service, who, ids[0])
    assert state["status"] == "ready" and script["written"]["ai-1"]["text"].startswith("You just added")
    assert script["reviewed"] is False and script["fact_ids_used"]  # no reviewer configured here
    _, skipped = service_script(service, who, ids[1])
    assert skipped["status"] == "skipped" and "No public trace" in skipped["reason"]
    status = service.preparation_status(who)
    entry = next(e for e in status["lists"] if e["list_name"] == "Real")
    # The import also queued a thread job for these leads, which this test ran directly.
    assert entry["total"] == 2 and entry["working"] == 2 and entry["spent_usd"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_list_progress_names_each_skipped_lead_and_why(workspace, app, monkeypatch):
    from app.db import Job

    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    await service.run_prep_thread(who, ids)
    with service.db.transaction() as s:
        for job in s.query(Job).filter(Job.queue == "research"):
            job.status = "completed"  # this test ran the queued thread directly
    entry = next(e for e in service.preparation_status(who)["lists"] if e["list_name"] == "Real")
    assert entry["ready"] == 1 and entry["skipped"] == 1
    assert entry["skipped_leads"] == [
        {"name": ROWS[1]["name"], "reason": "No public trace of this business."}
    ]


def test_agents_leave_phone_numbers_to_the_dialer():
    from app.preparation.agent import INSTRUCTIONS, TOOLS

    skip = next(t for t in TOOLS if t["name"] == "skip_lead")["description"]
    assert "wrong number" not in skip
    assert "phone number" in INSTRUCTIONS and "the app checks numbers" in INSTRUCTIONS


@pytest.mark.asyncio
async def test_a_lead_left_open_is_retried_then_released_to_the_base_script(workspace, app, monkeypatch):
    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent("partial"))
    for _ in range(3):
        with service.db.transaction() as s:
            lead = service.required(s, "leads", ids[1], who)
            state = dict(lead.data["preparation"])
            state["next_attempt_at"] = None
            from app.db import update

            update(lead, preparation=state)
        await service.run_prep_thread(who, ids)
    _, state = service_script(service, who, ids[1])
    assert state["status"] == "failed" and state["attempts"] == 3
    with service.db.transaction(False) as s:
        assert service.required(s, "leads", ids[1], who).data["status"] == "eligible"


@pytest.mark.asyncio
async def test_plan_limits_pause_work_and_a_spent_list_budget_stops_it(workspace, app, monkeypatch):
    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent("limit"))
    assert (await service.run_prep_thread(who, ids))["paused"] is True
    status = service.preparation_status(who)
    assert status["paused_until"] and "limit" in status["pause_reason"]
    _, state = service_script(service, who, ids[0])
    assert state["status"] == "queued"
    assert service.prepare_tick(who)["queued"] == 0
    service.resume_preparation(who)
    with service.db.transaction() as s:
        from app.db import update

        update(service.scoped(s, "budgets", who)[0], per_list_usd=0.1)
    monkeypatch.setattr(service, "agent_route", lambda p: ScriptedAgent())
    assert (await service.run_prep_thread(who, ids)).get("budget") == "reached"
    assert service.preparation_status(who)["pause_reason"] == "This list reached its AI budget."


@pytest.mark.asyncio
async def test_the_meaning_review_drops_unsupported_written_text(workspace, app, monkeypatch):
    from app.providers.inference import InferenceResult

    class Reviewer:
        async def complete(self, instructions, request, schema):
            verdicts = [
                {"field": f["field"], "verdict": "CONTRADICTED", "evidence": f["evidence"] or ["x"]}
                for f in request["fields"]
            ]
            return InferenceResult({"fingerprint": request["fingerprint"], "verdicts": verdicts}, {}, "r1")

    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    monkeypatch.setattr(service, "inference", lambda *args, **kwargs: Reviewer())
    await service.run_prep_thread(who, ids)
    script, state = service_script(service, who, ids[0])
    assert state["status"] == "ready" and "ai-1" not in script["written"]


def service_script(service, who, lead_id):
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", lead_id, who)
        row = service.script_row(s, who, lead)
        return (dict(row.data) if row else {}), service.preparation_state(s, who, lead)
