"""Agent threads: tool rules, close-out, publication, plan limits and the per-list budget."""

import json

import pytest
from app import ops
from app.preparation.agent import SimulatedAgent, ThreadWork
from app.preparation.templates import parse_markdown
from helpers import bound

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


NO_TRIGGER = {
    "trigger": {"type": "none", "date": None, "source_url": None, "quote": None, "evidence": None},
    "bridge": {"text": "I saw you manage several buildings."},
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
    assert call(work, "finish_research", lead="lead-1", summary="Expanding landlord", **NO_TRIGGER)["ok"]
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
        await handler("finish_research", {"lead": first_lead, "summary": "Growing landlord.", **NO_TRIGGER})
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
        await handler("finish_research", {"lead": second, "summary": "Nothing found.", **NO_TRIGGER})
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
    monkeypatch.setattr(ops, "agent_route", bound(lambda p: agent))
    monkeypatch.setattr(ops, "inference", bound(lambda *args, **kwargs: None))
    ids = [
        lead["id"]
        for lead in ops.import_leads(
            service,
            who,
            {"campaign_id": workspace["campaign_id"], "rows": ROWS, "list_name": "Real", "prepare": True},
        )["leads"]
    ]
    return service, who, ids


@pytest.mark.asyncio
async def test_a_metered_thread_verifies_pages_publishes_and_charges_its_list(workspace, app, monkeypatch):
    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    result = await ops.run_prep_thread(service, who, ids)
    assert result["prepared"] == 1 and result["rejections"] == 1
    script, state = service_script(service, who, ids[0])
    assert state["status"] == "ready" and script["written"]["ai-1"]["text"].startswith("You just added")
    assert script["reviewed"] is False and script["fact_ids_used"]  # no reviewer configured here
    _, skipped = service_script(service, who, ids[1])
    assert skipped["status"] == "skipped" and "No public trace" in skipped["reason"]
    status = ops.preparation_status(service, who)
    entry = next(e for e in status["lists"] if e["list_name"] == "Real")
    # The import also queued a thread job for these leads, which this test ran directly.
    assert entry["total"] == 2 and entry["working"] == 2 and entry["spent_usd"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_list_progress_names_each_skipped_lead_and_why(workspace, app, monkeypatch):
    from app.platform.db import Job

    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    await ops.run_prep_thread(service, who, ids)
    with service.db.transaction() as s:
        for job in s.query(Job).filter(Job.queue == "research"):
            job.status = "completed"  # this test ran the queued thread directly
    entry = next(e for e in ops.preparation_status(service, who)["lists"] if e["list_name"] == "Real")
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
            lead = ops.required(service, s, "leads", ids[1], who)
            state = dict(lead.data["preparation"])
            state["next_attempt_at"] = None
            from app.platform.db import update

            update(lead, preparation=state)
        await ops.run_prep_thread(service, who, ids)
    _, state = service_script(service, who, ids[1])
    assert state["status"] == "failed" and state["attempts"] == 3
    with service.db.transaction(False) as s:
        assert ops.required(service, s, "leads", ids[1], who).data["status"] == "eligible"


@pytest.mark.asyncio
async def test_plan_limits_pause_work_and_a_spent_list_budget_stops_it(workspace, app, monkeypatch):
    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent("limit"))
    assert (await ops.run_prep_thread(service, who, ids))["paused"] is True
    status = ops.preparation_status(service, who)
    assert status["paused_until"] and "limit" in status["pause_reason"]
    _, state = service_script(service, who, ids[0])
    assert state["status"] == "queued"
    assert ops.prepare_tick(service, who)["queued"] == 0
    ops.resume_preparation(service, who)
    with service.db.transaction() as s:
        from app.platform.db import update

        update(ops.scoped(service, s, "budgets", who)[0], per_list_usd=0.1)
    monkeypatch.setattr(ops, "agent_route", bound(lambda p: ScriptedAgent()))
    assert (await ops.run_prep_thread(service, who, ids)).get("budget") == "reached"
    assert ops.preparation_status(service, who)["pause_reason"] == "This list reached its AI budget."


@pytest.mark.asyncio
async def test_the_meaning_review_drops_unsupported_written_text(workspace, app, monkeypatch):
    from app.providers.inference import InferenceResult

    class Reviewer:
        async def complete(self, instructions, request, schema):
            verdicts = [
                {"field": f["field"], "verdict": "CONTRADICTED", "evidence": f["evidence"]}
                for f in request["fields"]
            ]
            return InferenceResult({"fingerprint": request["fingerprint"], "verdicts": verdicts}, {}, "r1")

    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    monkeypatch.setattr(ops, "inference", bound(lambda *args, **kwargs: Reviewer()))
    await ops.run_prep_thread(service, who, ids)
    script, state = service_script(service, who, ids[0])
    assert state["status"] == "ready" and "ai-1" not in script["written"]
    # The labeled bridge is judged only for contradiction, and a contradicted one is dropped, not the trigger.
    assert script["bridge"] is None and script["trigger"]["type"] == "none"


def service_script(service, who, lead_id):
    with service.db.transaction(False) as s:
        lead = ops.required(service, s, "leads", lead_id, who)
        row = ops.script_row(service, s, who, lead)
        return (dict(row.data) if row else {}), ops.preparation_state(service, s, who, lead)


def test_finish_research_keeps_only_a_trigger_that_passes_the_code_checks():
    from datetime import date

    rows = leads()
    rows[1]["signals"] = {
        "aligned": True,
        "items": [{"summary": "New site", "date": "2026-10-01", "url": "https://x.example/gone"}],
    }
    work = ThreadWork(rows, parse_markdown(SCRIPT), fetch, today=date(2026, 11, 8))
    claim = {
        "type": "expansion",
        "date": "2026-10-01",
        "source_url": PAGE["url"],
        "quote": "added two buildings on Elm Street",
        "evidence": "page",
    }
    bridge = {"text": "Congrats on the new Elm Street buildings."}
    stale = call(
        work,
        "finish_research",
        lead="lead-1",
        summary="s",
        trigger={**claim, "date": "2025-01-01"},
        bridge=bridge,
    )
    assert not stale["ok"] and "outside the 180-day window" in stale["errors"][0]
    unlisted = call(
        work,
        "finish_research",
        lead="lead-1",
        summary="s",
        trigger={**claim, "type": "rebrand"},
        bridge=bridge,
    )
    assert not unlisted["ok"] and "not on the list" in unlisted["errors"][0]
    misquoted = call(
        work,
        "finish_research",
        lead="lead-1",
        summary="s",
        trigger={**claim, "quote": "added ten buildings downtown"},
        bridge=bridge,
    )
    assert not misquoted["ok"] and "not on" in misquoted["errors"][0]
    assert call(work, "finish_research", lead="lead-1", summary="s", trigger=claim, bridge=bridge)["ok"]
    assert work.triggers["lead-1"]["trigger"]["strength"] == "B"
    assert work.triggers["lead-1"]["bridge"] == {"text": bridge["text"], "is_inference": True}
    row_only = {
        "type": "expansion",
        "date": "2026-10-01",
        "source_url": "https://x.example/gone",
        "quote": None,
        "evidence": "row_only",
    }
    assert call(work, "finish_research", lead="lead-2", summary="s", trigger=row_only, bridge=bridge)["ok"]
    assert work.triggers["lead-2"]["trigger"]["strength"] == "C"
    assert work.triggers["lead-2"]["trigger"]["from_row"] is True


def test_the_agent_sees_the_trigger_list_and_row_signals_first():
    work = ThreadWork(
        leads(), parse_markdown(SCRIPT + "\n# Triggers\n- hiring: Hiring reps (60 days, A)\n"), fetch
    )
    context = work.context()
    assert context["triggers"] == [
        {"type": "hiring", "label": "Hiring reps", "window_days": 60, "strength": "A"}
    ]
    assert context["leads"][0]["signals"] == {"aligned": True, "items": []}


async def test_a_chatgpt_failure_hands_the_thread_to_the_fallback_with_the_same_output_shape(
    workspace, app, monkeypatch
):
    class Fallback(ScriptedAgent):
        billing, route, model = "openrouter", "fallback", "deepseek/deepseek-v4.1-flash"

    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent("limit"))
    monkeypatch.setattr(ops, "fallback_agent", bound(lambda p, fetch=None: Fallback()))
    monkeypatch.setattr(ops, "reserve_optional_budget", bound(lambda *a, **k: True))
    result = await ops.run_prep_thread(service, who, ids)
    fallback, state = service_script(service, who, ids[0])
    assert result.get("paused") is None and state["status"] == "ready"
    assert fallback["route"] == "fallback" and set(fallback["written"]) == {"ai-1"}
    assert SCRIPT_SHAPE <= set(fallback)
    with service.db.transaction(False) as s:
        lead = ops.required(service, s, "leads", ids[0], who).data
    assert lead["research_route"] == "fallback" and lead["trigger"]["type"] == "none"


async def test_a_primary_script_has_the_same_shape(workspace, app, monkeypatch):
    service, who, ids = prepared_workspace(workspace, app, monkeypatch, ScriptedAgent())
    await ops.run_prep_thread(service, who, ids)
    primary, _ = service_script(service, who, ids[0])
    assert primary["route"] == "chatgpt" and SCRIPT_SHAPE <= set(primary)


SCRIPT_SHAPE = {"picks", "written", "trigger", "bridge", "summary", "route", "template_version", "reviewed"}


async def test_the_fallback_agent_searches_reads_and_keeps_the_no_retention_route():
    import httpx
    from app.providers.chat_agent import ChatAgent

    bodies, replies = (
        [],
        iter(
            [
                {
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": '{"query": "Hilltop"}'},
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "id": "2",
                            "type": "function",
                            "function": {"name": "read_page", "arguments": '{"url": "https://h.example/n"}'},
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "id": "3",
                            "type": "function",
                            "function": {
                                "name": "skip_lead",
                                "arguments": '{"lead": "lead-1", "reason": "Closed for good."}',
                            },
                        }
                    ]
                },
                {"content": "done"},
            ]
        ),
    )

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", **next(replies)}}], "usage": {}}
        )

    async def search(query):
        return [{"title": "Hilltop", "url": "https://h.example/n", "snippet": "news"}]

    async def page(url):
        return {"title": "Hilltop news", "text": "Hilltop Apartments added two buildings."}

    handled = []

    async def handler(name, args):
        handled.append(name)
        return json.dumps({"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        agent = ChatAgent("key", search=search, fetch_page=page, http=http)
        await agent.run(
            "go",
            [{"name": "skip_lead", "description": "d", "schema": {"type": "object"}}],
            handler,
            "{}",
            lambda: None,
        )
    assert handled == ["skip_lead"]
    assert all(body["provider"] == {"zdr": True, "data_collection": "deny"} for body in bodies)
    assert bodies[0]["model"] == "deepseek/deepseek-v4.1-flash"
    assert {t["function"]["name"] for t in bodies[0]["tools"]} == {"skip_lead", "web_search", "read_page"}
    tool_answers = [m for m in bodies[-1]["messages"] if m.get("role") == "tool"]
    assert "Hilltop Apartments added two buildings" in tool_answers[1]["content"]


def test_research_set_to_the_fallback_uses_openrouter_even_when_chatgpt_is_signed_in(
    workspace, app, monkeypatch
):
    from app.providers.chat_agent import ChatAgent
    from helpers import principal

    service, who = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(ops, "inference", bound(lambda *a, **k: ScriptedAgent()))
    service.configs.write(who["tenant_id"], {"openrouter_api_key": "or-test"})
    assert isinstance(ops.agent_route(service, who), ScriptedAgent)
    service.configs.write(who["tenant_id"], {"research_route": "fallback"})
    assert isinstance(ops.agent_route(service, who), ChatAgent)
    monkeypatch.setattr(ops, "inference", bound(lambda *a, **k: None))
    service.configs.write(who["tenant_id"], {"research_route": "chatgpt"})
    assert isinstance(ops.agent_route(service, who), ChatAgent), "no ChatGPT login: the fallback takes over"
