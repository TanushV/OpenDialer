"""HTTP acceptance for fresh generations and subscription token receipts."""

from app.preparation.agent import SimulatedAgent
from journey import call, get, import_leads, post, ready, reserve, run_jobs
from test_campaign_prep import SCRIPT, save_script


def add(ws):
    return import_leads(
        ws,
        [
            {
                "name": "Fresh Writer",
                "company": "Writer Co",
                "phone": "+14155557941",
                "timezone": "America/Los_Angeles",
            }
        ],
        list_name="Playbook rewrites",
        prepare=True,
    )["leads"][0]["id"]


def test_new_master_never_reuses_old_personalized_output(workspace):
    ws = workspace
    save_script(ws)
    identity = add(ws)
    run_jobs(ws, 3)
    before = get(ws, f"/v1/leads/{identity}/packet")["script"]
    assert before["written"]
    save_script(ws, SCRIPT.replace("Would Tuesday work?", "Could we review the new scope?"))
    stale = get(ws, f"/v1/leads/{identity}/packet")["script"]
    assert stale["fallback"] and not stale.get("written")
    run_jobs(ws, 3)
    after = get(ws, f"/v1/leads/{identity}/packet")["script"]
    assert after["base_version"] > before["base_version"]
    assert after["script_version"] != before["script_version"]
    assert after["preparation_status"] == "ready"


def test_subscription_usage_is_persisted_once_and_scoped_to_generation(workspace, app):
    class Agent(SimulatedAgent):
        billing = "chatgpt_plan"
        model = "test-model"

        async def run(self, instructions, tools, handler, first, follow_up, *, on_usage=None, **kwargs):
            await on_usage(
                {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 40, "reasoning_tokens": 8}
            )
            await on_usage(
                {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 40, "reasoning_tokens": 8}
            )
            await super().run(instructions, tools, handler, first, follow_up, **kwargs)
            return {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 40, "reasoning_tokens": 8}

    app.state.service.agent_route = lambda p: Agent()
    save_script(workspace)
    add(workspace)
    run_jobs(workspace, 3)
    report = get(
        workspace,
        "/v1/usage/tokens",
        params={"campaign_id": workspace["campaign_id"], "list_name": "Playbook rewrites"},
    )
    assert report["totals"]["input_tokens"] == 100
    assert report["totals"]["output_tokens"] == 20
    assert report["totals"]["total_tokens"] == 120
    assert report["totals"]["cached_tokens"] == 40
    row = report["records"][0]
    assert row["context"]["base_version"] == get(workspace, "/v1/optimization/script")["base"]["version"]
    assert row["billing"] == "subscription_included" and row["token_status"] == "reported"
    assert row["context"]["lead_ids"]


def test_rewrite_and_research_refresh_are_separate_explicit_actions(workspace):
    save_script(workspace)
    identity = add(workspace)
    run_jobs(workspace, 3)
    result = post(
        workspace,
        "/v1/preparation/rewrite",
        {"list_name": "Playbook rewrites", "refresh_research": False, "sample_size": 1},
    )
    assert result["marked"] == 1 and result["sample"]
    run_jobs(workspace, 3)
    report = get(workspace, "/v1/preparation/estimate", params={"list_name": "Playbook rewrites"})
    assert report["basis"] in {"measured", "unavailable"}
    assert report["leads"] == 1
    assert get(workspace, f"/v1/leads/{identity}/packet")["script"]["preparation_status"] == "ready"


def test_missing_provider_counters_are_unknown_not_zero(workspace, app):
    class Agent(SimulatedAgent):
        billing = "chatgpt_plan"

        async def run(self, *args, **kwargs):
            await super().run(*args, **kwargs)
            return {}

    app.state.service.agent_route = lambda p: Agent()
    save_script(workspace)
    add(workspace)
    run_jobs(workspace, 3)
    report = get(workspace, "/v1/usage/tokens", params={"list_name": "Playbook rewrites"})
    assert report["totals"]["unknown_requests"] == 1
    assert report["totals"]["input_tokens"] is None
    assert report["totals"]["output_tokens"] is None
    assert report["records"][0]["token_status"] == "unknown"


def test_failed_work_retains_the_last_reported_cumulative_usage(workspace, app):
    class Agent(SimulatedAgent):
        billing = "chatgpt_plan"

        async def run(self, *args, on_usage=None, **kwargs):
            await on_usage({"input_tokens": 7, "output_tokens": 3})
            raise RuntimeError("Provider failed after reporting consumption")

    app.state.service.agent_route = lambda p: Agent()
    save_script(workspace)
    add(workspace)
    run_jobs(workspace, 2)
    report = get(workspace, "/v1/usage/tokens", params={"list_name": "Playbook rewrites"})
    assert report["totals"]["input_tokens"] == 7
    assert report["totals"]["output_tokens"] == 3
    assert report["totals"]["partial_requests"] == 1
    assert report["records"][0]["status"] == "failed"


def test_formatting_only_edit_does_not_regenerate_and_active_call_stays_frozen(workspace):
    ws = workspace
    save_script(ws)
    identity = add(ws)
    run_jobs(ws, 3)
    original = get(ws, f"/v1/leads/{identity}/packet")["script"]
    save_script(ws, SCRIPT.replace("\n\n", "\n\n\n"))
    run_jobs(ws, 3)
    assert get(ws, f"/v1/leads/{identity}/packet")["script"]["script_version"] == original["script_version"]
    ready(ws)
    offered = reserve(ws, list_names=["Playbook rewrites"])
    assert offered.status_code == 200, offered.text
    assert offered.json()["lead"]["id"] == identity
    placed = post(
        ws,
        "/v1/calls/start",
        {"reservation_id": offered.json()["reservation_id"], "idempotency_key": "playbook-frozen"},
    )
    save_script(ws, SCRIPT.replace("Would Tuesday work?", "A new close?"))
    run_jobs(ws, 3)
    assert (
        get(ws, f"/v1/calls/{placed['id']}")["script_snapshot"]["script_version"]
        == original["script_version"]
    )
    assert get(ws, f"/v1/leads/{identity}/packet")["script"]["script_version"] == original["script_version"]


def test_rewrite_remaining_skips_completed_sample_and_paused_lists(workspace):
    ws = workspace
    save_script(ws)
    add(ws)
    run_jobs(ws, 3)
    remaining = post(
        ws, "/v1/preparation/rewrite", {"list_name": "Playbook rewrites", "remaining_only": True}
    )
    assert remaining["marked"] == 0
    estimate = get(ws, "/v1/preparation/estimate", params={"list_name": "Playbook rewrites"})
    assert estimate["remaining_leads"] == 0
    call(
        ws,
        "PATCH",
        "/v1/lists",
        json={"campaign_id": ws["campaign_id"], "list_name": "Playbook rewrites", "action": "pause"},
    )
    assert post(ws, "/v1/preparation/rewrite", {"list_name": "Playbook rewrites"})["marked"] == 0


def test_rewrite_passes_current_facts_without_old_scripts_and_refresh_requests_search(workspace, app):
    import json

    seen = []

    class Agent(SimulatedAgent):
        async def run(self, instructions, tools, handler, first, follow_up, **kwargs):
            seen.append((json.loads(first), kwargs["web_search"]))
            return await super().run(instructions, tools, handler, first, follow_up, **kwargs)

    app.state.service.agent_route = lambda p: Agent()
    save_script(workspace)
    add(workspace)
    run_jobs(workspace, 3)
    post(workspace, "/v1/preparation/rewrite", {"list_name": "Playbook rewrites"})
    run_jobs(workspace, 3)
    context, search = seen[-1]
    assert not search and context["known_facts"]["lead-1"]
    assert list(context["prior_calls"]) == ["lead-1"]
    assert "written" not in json.dumps(context)
    post(workspace, "/v1/preparation/rewrite", {"list_name": "Playbook rewrites", "refresh_research": True})
    run_jobs(workspace, 3)
    assert seen[-1][1] and not seen[-1][0].get("known_facts")


def test_cumulative_metered_cost_settles_once_at_the_final_receipt(workspace, app):
    class Agent(SimulatedAgent):
        billing = "openai"

        async def run(self, *args, on_usage=None, **kwargs):
            await on_usage({"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.01})
            await on_usage({"input_tokens": 20, "output_tokens": 4, "cost_usd": 0.02})
            await super().run(*args, **kwargs)
            return {"input_tokens": 20, "output_tokens": 4, "cost_usd": 0.02}

    app.state.service.agent_route = lambda p: Agent()
    save_script(workspace)
    identity = add(workspace)
    run_jobs(workspace, 3)
    report = get(workspace, "/v1/usage/tokens", params={"list_name": "Playbook rewrites"})
    assert report["totals"]["requests"] == 1
    assert report["records"][0]["cost_usd"] == 0.02
    assert report["records"][0]["status"] == "completed"
    assert get(workspace, f"/v1/leads/{identity}/packet")["script"]["preparation_status"] == "ready"
