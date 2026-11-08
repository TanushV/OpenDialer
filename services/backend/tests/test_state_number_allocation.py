from app.optimization.numbering import select_number


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
    assert select_number(lead, inventory)["number_id"] == "la"
    inventory[2]["used_today"] = 20
    assert select_number(lead, inventory)["number_id"] == "sf"
    assert select_number(lead, inventory, bound_id="ny")["number_id"] == "ny"
    assert select_number(lead, [number("sf", "415", lifecycle="paused_for_outbound")])["number_id"] is None


def test_out_of_state_is_last_choice_and_respects_binding_and_capacity():
    lead = {"state": "CA"}
    local, general, other = number("sf", "415"), number("pool", "general"), number("ny", "212")
    assert select_number(lead, [other, general, local])["number_id"] == "sf"
    assert select_number(lead, [other, general])["number_id"] == "pool"
    exhausted = {**local, "used_today": 20}
    result = select_number(lead, [exhausted, other])
    assert (result["number_id"], result["reason"]) == ("ny", "out_of_state_fallback")
    assert select_number(lead, [exhausted, other], bound_id="sf")["number_id"] is None
    assert select_number(lead, [{**other, "active_obligations": 1}])["number_id"] is None
    assert select_number(lead, [{**other, "lifecycle": "paused_for_outbound"}])["number_id"] is None


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
