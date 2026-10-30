"""List-scoped, state-first planning; quoting never authorizes a carrier order."""

from journey import call, get, post, run_jobs


def test_list_import_and_state_plan_reuse_owned_numbers(workspace, carrier):
    imported = post(
        workspace,
        "/v1/leads/import",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "West coast",
            "rows": [
                {
                    "name": "LA",
                    "phone": "+14155550191",
                    "region": "213",
                    "state": "California",
                    "timezone": "America/Los_Angeles",
                },
                {"name": "Moved", "phone": "+12125550192", "state": "CA", "timezone": "America/Los_Angeles"},
            ],
        },
    )
    assert imported["imported"] == 2
    assert {r["list_name"] for r in imported["leads"]} == {"West coast"}
    assert {r["state"] for r in imported["leads"]} == {"CA"}
    assert {r["state_source"] for r in imported["leads"]} == {"provided"}
    post(
        workspace,
        "/v1/admin/numbers/proposal",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "West coast",
        },
    )
    run_jobs(workspace, 3)
    summary = get(workspace, "/v1/admin/numbers/summary", params={"list_name": "West coast"})
    assert "West coast" in summary["lists"]
    forecast = summary["forecast"]
    assert forecast.get("list_name") == "West coast", str(forecast)
    assert forecast["imported_leads"] == 2
    assert [r["region"] for r in forecast["regions"]] == ["CA"]
    assert forecast["existing_only_capacity"] == 2
    assert forecast["proposed_additional_numbers"] == 0
    assert forecast["regions"][0]["suggested_area_codes"]
    assert carrier.purchases == []
    all_summary = get(workspace, "/v1/admin/numbers/summary")
    assert all_summary["forecast"].get("list_name") is None


def test_state_search_picks_area_codes_without_buying(workspace, carrier):
    result = post(workspace, "/v1/admin/numbers/search", {"state": "CA", "list_name": ""})
    assert result["numbers"]
    assert result["searched_area_codes"]
    assert all(q["state"] == "CA" for q in result["numbers"])
    assert all(q["phone"][2:5] == q["region"] for q in result["numbers"])
    assert carrier.purchases == []
    post(workspace, "/v1/admin/numbers/search", {"state": "ZZ"}, status=422)
    post(
        workspace,
        "/v1/admin/numbers/proposal",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "Not my list",
        },
        status=404,
    )


def test_unknown_state_is_not_silently_inferred_from_invalid_explicit_location(workspace):
    result = post(
        workspace,
        "/v1/leads/import",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "Review location",
            "rows": [{"phone": "+14155550193", "state": "not a state", "timezone": "UTC"}],
        },
    )
    assert result["invalid"] == 1
    assert result["errors"][0]["reason"] == "INVALID_STATE"


def test_corrected_phone_refreshes_only_inferred_state(workspace):
    result = post(
        workspace,
        "/v1/leads/import",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "Corrections",
            "rows": [{"phone": "+14155550196"}, {"phone": "+14155550197", "state": "CA"}],
        },
    )
    inferred, provided = result["leads"]
    changed = call(
        workspace,
        "PATCH",
        f"/v1/leads/{inferred['id']}/endpoint",
        json={"phone": "+12125550196", "evidence": "Confirmed correction"},
    )
    assert changed["state"] == "NY" and changed["state_source"] == "phone_area_code"
    changed = call(
        workspace,
        "PATCH",
        f"/v1/leads/{provided['id']}/endpoint",
        json={"phone": "+12125550197", "evidence": "Confirmed correction"},
    )
    assert changed["state"] == "CA" and changed["state_source"] == "provided"


def test_list_expansion_proposes_a_state_not_a_generic_pool(workspace, carrier):
    post(
        workspace,
        "/v1/leads/import",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "Florida",
            "rows": [{"phone": "+14155550198", "state": "FL", "timezone": "America/New_York"}],
        },
    )
    post(
        workspace,
        "/v1/admin/numbers/proposal",
        {"campaign_id": workspace["campaign_id"], "list_name": "Florida"},
    )
    run_jobs(workspace, 3)
    forecast = get(workspace, "/v1/admin/numbers/summary", params={"list_name": "Florida"})["forecast"]
    assert forecast["regions"][0]["region"] == "FL"
    assert forecast["regions"][0]["proposed_additional"] == 1
    assert forecast["proposed_general_additional_numbers"] == 0
    assert carrier.purchases == []
