"""List-scoped, state-first planning; quoting never authorizes a carrier order."""

from datetime import datetime
from zoneinfo import ZoneInfo

from journey import call, get, post, run_jobs, start


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
    inferred_lead = next(lead for lead in imported["leads"] if not lead["timezone"])
    attempt = next(row for row in forecast["next_attempts"] if row["lead_id"] == inferred_lead["id"])
    assert (
        attempt["local_hour"]
        == datetime.fromisoformat(attempt["at"]).astimezone(ZoneInfo("America/Los_Angeles")).hour
    )
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
    for number in get(workspace, "/v1/workspace/numbers")["items"]:
        call(
            workspace,
            "PATCH",
            f"/v1/admin/numbers/{number['id']}",
            json={"lifecycle": "paused_for_outbound", "reason": "Inventory unavailable for this plan"},
        )
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


def test_dialer_and_planner_use_owned_out_of_state_capacity(workspace, carrier):
    remote, _ = post(
        workspace,
        "/v1/leads/import",
        {
            "campaign_id": workspace["campaign_id"],
            "list_name": "Coast to coast",
            "rows": [
                {"phone": "+12035550198", "timezone": "America/New_York", "requested_contact": True},
                {"phone": "+14155550198", "timezone": "America/Los_Angeles"},
            ],
        },
    )["leads"]
    post(
        workspace,
        "/v1/admin/numbers/proposal",
        {"campaign_id": workspace["campaign_id"], "list_name": "Coast to coast"},
    )
    run_jobs(workspace, 3)
    forecast = get(workspace, "/v1/admin/numbers/summary", params={"list_name": "Coast to coast"})["forecast"]
    regions = {r["region"]: r for r in forecast["regions"]}
    assert forecast["existing_only_capacity"] == 2
    assert regions["CA"]["usable_existing_capacity"] == 1
    assert regions["CA"]["out_of_state_capacity"] == 0
    assert regions["CT"]["usable_existing_capacity"] == regions["CT"]["general_fallback_capacity"] == 0
    assert regions["CT"]["out_of_state_capacity"] == 1
    assert forecast["minimum_additional_numbers"] == 0
    attempt = start(workspace)
    assert attempt["lead_id"] == remote["id"]
    assert attempt["number_id"] in {n["id"] for n in get(workspace, "/v1/workspace/numbers")["items"]}
    assert [dial["to"] for dial in carrier.dials] == [remote["phone"]]
    assert carrier.purchases == []
