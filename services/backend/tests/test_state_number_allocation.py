from app.optimization.numbering import capacity_forecast, select_number


def number(identity, region, **values):
    return {
        "id": identity,
        "region": region,
        "lifecycle": "healthy",
        "daily_limit": 20,
        "capacity": 1,
        **values,
    }


def test_same_state_reuses_capacity_across_metros_without_rotating_bound_identity():
    lead = {"region": "213", "state": "CA"}
    inventory = [number("sf", "415"), number("ny", "212"), number("la", "213", used_today=19)]
    assert select_number(lead, inventory)["number_id"] == "sf"
    assert select_number(lead, inventory, bound_id="ny")["number_id"] == "ny"
    assert select_number(lead, [number("ny", "212")])["number_id"] is None
    assert select_number(lead, [number("sf", "415", lifecycle="paused_for_outbound")])["number_id"] is None


def test_area_code_only_breaks_ties_after_state_capacity_and_health():
    lead = {"region": "213", "state": "CA"}
    assert select_number(lead, [number("a-sf", "415"), number("z-la", "213")])["number_id"] == "z-la"


def test_explicit_lead_state_wins_over_ported_phone_and_unknown_is_not_a_state():
    assert (
        select_number({"region": "212", "state": "CA"}, [number("ca", "415"), number("ny", "212")])[
            "number_id"
        ]
        == "ca"
    )
    assert select_number({"region": "416"}, [number("ca", "415")])["number_id"] is None


def test_capacity_uses_one_state_pool_across_area_codes(clock):
    leads = [
        {"id": str(i), "region": r, "timezone": "UTC", "status": "eligible"}
        for i, r in enumerate(["213", "415", "650"])
    ]
    result = capacity_forecast(
        leads, [number("owned", "619")], now=clock.now.replace(hour=10), policy={"max_attempts": 1}
    )
    assert result["existing_only_capacity"] == 3
    assert result["minimum_additional_numbers"] == 0
    assert len(result["regions"]) == 1
    assert result["regions"][0]["region"] == "CA"


def test_number_comparison_groups_shared_state_inventory_and_rejects_old_basis(workspace, app):
    from app.db import doc, update
    from helpers import principal

    service = app.state.service
    p = principal(workspace)
    response = workspace["client"].post(
        "/v1/leads/import",
        headers=workspace["headers"],
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [{"phone": "+16505550199", "timezone": "America/Los_Angeles"}],
        },
    )
    assert response.status_code == 200
    response = workspace["client"].post(
        "/v1/admin/optimization/comparisons",
        headers=workspace["headers"],
        json={
            "family": "number_allocation",
            "review_reference": "State-based comparison",
            "duration_days": 7,
        },
    )
    assert response.status_code == 200
    assert response.json()["allocation_basis"] == "state"
    with service.db.transaction() as s:
        assignments = [
            doc(r)
            for r in service.scoped(s, "experiment_assignments", p)
            if r.data.get("family") == "number_allocation"
        ]
        california = [r for r in assignments if r.get("cluster") == "CA"]
        assert len(california) == 2 and len({r["arm"] for r in california}) == 1
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        studies = campaign.data["controlled_comparisons"]
        studies[0].pop("allocation_basis")
        update(campaign, controlled_comparisons=studies)
        lead = service.scoped(s, "leads", p)[0]
        assert service.comparison_assignment(s, p, campaign, lead, "number_allocation") is None
