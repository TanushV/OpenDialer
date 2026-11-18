"""Finished scripts can bypass preparation through the ordinary CSV and Dialer entry points."""

from app import ops
from app.platform.db import update
from conftest import ready
from helpers import principal
from journey import get, import_leads, post, run_jobs

SCRIPT = """# Opening
Hi Morgan, this is Alex from Example Seller.

# The Question
How do you handle "rush orders," especially on Fridays?

# Common objections
## Busy
When would be better?

# Close
Would Tuesday work?
"""


def row(name, phone, **values):
    return {"name": name, "phone": phone, "timezone": "America/Los_Angeles", **values}


def test_finished_csv_scripts_are_callable_without_a_master_or_generation(workspace, app, carrier):
    ws = workspace
    service = app.state.service
    service.require_prepared_scripts = True
    with service.db.transaction() as session:
        campaign = ops.selected_campaign(service, session, principal(ws))
        update(campaign, script=None)
    result = import_leads(
        ws,
        [
            row("Morgan CSV", "+14155550113", **{"Prepared Script": SCRIPT, "signal": "a new role"}),
            row("Needs writing", "+14155550114", **{"Prepared Script": ""}),
            row("Unfinished", "+14155550115", **{"Prepared Script": "{{ write the opening }}"}),
        ],
        list_name="Finished scripts",
    )
    assert (result["imported"], result["scripts_imported"], result["invalid"], result["preparing"]) == (
        2,
        1,
        1,
        0,
    )
    assert result["errors"][0]["reason"] == "INVALID_SCRIPT"
    saved, blank = result["leads"]
    packet = get(ws, f"/v1/leads/{saved['id']}/packet")
    assert packet["preparation"]["status"] == "ready"
    script = packet["script"]
    assert script["source"] == "csv" and script["fallback"] is False
    assert script["opener"] == "Hi Morgan, this is Alex from Example Seller."
    assert [section["key"] for section in script["sections"]] == [
        "opening",
        "custom:the-question",
        "objections",
        "close",
    ]
    assert (
        script["sections"][1]["options"][0]["text"]
        == 'How do you handle "rush orders," especially on Fridays?'
    )
    assert "Prepared Script" not in saved["row"] and saved["row"]["signal"] == "a new role"
    assert get(ws, f"/v1/leads/{blank['id']}/packet")["preparation"]["status"] == "missing"
    progress = get(ws, "/v1/preparation")
    entry = next(entry for entry in progress["lists"] if entry["list_name"] == "Finished scripts")
    assert (entry["ready"], entry["unrequested"], progress["threads"]) == (1, 1, [])

    ready(ws)
    hold = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"], "list_names": ["Finished scripts"]})
    assert hold["lead"]["id"] == saved["id"]
    call = post(
        ws, "/v1/calls/start", {"reservation_id": hold["reservation_id"], "idempotency_key": "imported"}
    )
    assert call["script_snapshot"]["script_version"] == script["script_version"]
    assert call["script_snapshot"]["sections"] == script["sections"]
    assert len(carrier.dials) == 1


def test_reimport_replaces_queued_generation_and_keeps_the_finished_script(workspace):
    ws = workspace
    lead = row("Dana CSV", "+14155550123")
    queued = import_leads(ws, [lead], list_name="Updated list", prepare=True)
    assert queued["preparing"] == 1
    identity = queued["leads"][0]["id"]
    finished = "Hi Dana.\n\nCan we talk next Thursday?"
    result = import_leads(ws, [{**lead, "script": finished}], list_name="Updated list")
    assert result["duplicates"] == result["scripts_imported"] == 1
    run_jobs(ws, rounds=2)
    script = get(ws, f"/v1/leads/{identity}/packet")["script"]
    assert script["source"] == "csv" and script["opener"] == finished

    base = get(ws, "/v1/optimization/script")["base"]
    post(
        ws,
        "/v1/admin/script/import",
        {
            "markdown": "# Opening\n{rep} from {seller}.\n\n# Close\nHow about Friday?",
            "expected_version": base["version"],
            "preview": False,
        },
    )
    import_leads(ws, [{**lead, "script": ""}], list_name="Updated list")
    retry = post(ws, "/v1/preparation/rewrite", {"list_name": "Updated list"})
    assert retry["marked"] == 0
    current = get(ws, f"/v1/leads/{identity}/packet")
    assert current["preparation"]["status"] == "ready"
    assert current["script"]["script_version"] == script["script_version"]
