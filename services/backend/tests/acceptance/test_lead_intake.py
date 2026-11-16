"""R9.2, R9.3, R9.16 and R9.21 through the API: one column map reads the owner's rows, a dead number is never dialed,
and a CSV import is taken as it is."""

import json

from journey import get, import_leads, post, reserve

ROW = {
    "name": "Liam Example",
    "company": "Brighterway",
    "phone": "+16175550100",
    "title": "VP Sales",
    "industry": "Logistics",
    "employees (estimate)": "120",
    "finder": "blitzapi",
    "network status": "LIVE",
    "signal summaries": "Hiring 3 SDRs | Opened Austin office",
    "signal dates": "2026-10-20; 2026-09-02",
    "signal urls": "https://b.example/jobs; https://b.example/news",
    "email": "liam@b.example",
    "apollo person id": "abc123",
}


def lead_named(ws, name):
    page = get(ws, "/v1/workspace/leads", params={"q": name})
    return next(lead for lead in page["items"] if lead["name"] == name)


def test_csv_mapping_keeps_all_imported_columns_on_the_lead(workspace):
    result = import_leads(workspace, [ROW], list_name="Intake")
    assert result["column_map"]["signal_summaries"] == {"column": "signal summaries", "delimiter": "|"}
    lead = lead_named(workspace, "Liam Example")
    assert lead["signals"]["aligned"] is True
    assert [item["url"] for item in lead["signals"]["items"]] == [
        "https://b.example/jobs",
        "https://b.example/news",
    ]
    assert lead["profile"]["title"] == "VP Sales"
    assert lead["number_source"] == "blitzapi"
    assert lead["row"] == ROW
    assert lead["row"]["signal summaries"].startswith("Hiring 3 SDRs")
    # R9.21: a CSV lead is taken as it is; no number check runs and the lead says so.
    assert lead["source"] == "csv"
    assert lead["number_label"] == "Not checked (CSV)"
    assert lead["number_hold"] is None


def test_reimporting_csv_recovers_unmapped_context_and_keeps_the_same_lead(workspace):
    first = import_leads(workspace, [ROW], list_name="Intake")
    imported = import_leads(
        workspace,
        [{"name": ROW["name"], "phone": ROW["phone"], "account rank": "A", "grade": "B"}],
        list_name="Ranked",
    )
    assert imported["duplicates"] == 1
    lead = lead_named(workspace, ROW["name"])
    assert lead["id"] == first["leads"][0]["id"]
    assert lead["row"] == {**ROW, "account rank": "A", "grade": "B"}
    assert {"Intake", "Ranked"} <= set(lead["list_names"])
    assert len(lead["signals"]["items"]) == 2


def test_research_agent_gets_named_and_unmapped_columns_from_the_csv(workspace, monkeypatch):
    from app.preparation.agent import SimulatedAgent
    from journey import run_jobs

    received = []
    original = SimulatedAgent.run

    async def capture(self, instructions, tools, handler, first, follow_up, **kwargs):
        received.extend(lead["row"] for lead in json.loads(first)["leads"])
        return await original(self, instructions, tools, handler, first, follow_up, **kwargs)

    monkeypatch.setattr(SimulatedAgent, "run", capture)
    row = {**ROW, "account rank": "Enterprise", "custom note": "Requested security details"}
    import_leads(workspace, [row], list_name="Full row")
    assert post(workspace, "/v1/preparation/lists", {"list_name": "Full row"})["marked"] == 1
    run_jobs(workspace, rounds=3)
    assert row in received


def test_a_dead_number_is_never_dialed_and_the_lead_says_why(workspace):
    import_leads(
        workspace,
        [{**ROW, "name": "Dana Dead", "phone": "+16175550199", "network status": "DEAD"}],
        list_name="Dead only",
    )
    lead = lead_named(workspace, "Dana Dead")
    assert lead["live_status"] == "dead"
    assert lead["number_hold"] == "NUMBER_DEAD"
    assert "dead" in lead["number_hold_reason"]
    response = reserve(workspace, list_names=["Dead only"])
    assert response.status_code == 409, response.text
    assert "NUMBER_DEAD" in response.text


def test_a_corrected_map_is_checked_and_the_guess_is_offered(workspace):
    guess = post(workspace, "/v1/leads/column-map", {"headers": list(ROW)})
    assert guess["map"]["live_status"]["column"] == "network status"
    bad = {"signal_urls": {"column": "not a column", "delimiter": ";"}}
    result = post(
        workspace,
        "/v1/leads/import",
        {"campaign_id": workspace["campaign_id"], "rows": [ROW], "column_map": bad},
        status=422,
    )
    assert result["detail"]["code"] == "COLUMN_MAP_INVALID"


def test_the_export_headings_for_account_signals_and_phone_status_are_recognized(workspace):
    row = {
        "name": "Liam Row",
        "phone": "+16175550100",
        "account signals": "Hiring SDRs",
        "Phone_Status": "DEAD",
    }
    result = import_leads(workspace, [row], list_name="Export headers")
    assert result["column_map"]["signal_summaries"]["column"] == "account signals"
    assert result["column_map"]["live_status"]["column"] == "Phone_Status"
    lead = lead_named(workspace, row["name"])
    assert lead["signals"]["items"][0]["summary"] == "Hiring SDRs"
    assert lead["number_hold"] == "NUMBER_DEAD"


def test_a_signal_column_split_on_its_own_delimiter_never_misaligns(workspace):
    row = {
        **ROW,
        "name": "Mia Mixed",
        "phone": "+16175550111",
        "signal summaries": "Hiring; outbound push | Austin office",
    }
    import_leads(workspace, [row], list_name="Mixed")
    items = lead_named(workspace, "Mia Mixed")["signals"]["items"]
    assert [item["summary"] for item in items] == ["Hiring; outbound push", "Austin office"]
