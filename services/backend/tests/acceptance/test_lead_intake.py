"""R9.2, R9.3, R9.16 and R9.21 through the API: one column map reads the owner's rows, a dead number is never dialed,
and a CSV import is taken as it is."""

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


def test_the_owner_columns_are_mapped_and_bookkeeping_never_reaches_the_row(workspace):
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
    assert "email" not in lead["row"] and "apollo person id" not in lead["row"]
    assert lead["row"]["signal summaries"].startswith("Hiring 3 SDRs")
    # R9.21: a CSV lead is taken as it is; no number check runs and the lead says so.
    assert lead["source"] == "csv"
    assert lead["number_label"] == "Not checked (CSV)"
    assert lead["number_hold"] is None


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
