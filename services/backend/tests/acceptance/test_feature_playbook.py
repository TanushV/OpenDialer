"""HTTP acceptance for fresh generations and subscription token receipts."""

from app.preparation.agent import SimulatedAgent
from journey import get, import_leads, post, run_jobs
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
