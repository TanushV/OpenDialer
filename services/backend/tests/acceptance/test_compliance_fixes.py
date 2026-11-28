"""Call insights plan section 4, fixes C1 and C2: the app blocks what is unmodeled and never uses carrier speech.

C1 is covered with the carrier contracts in `test_telnyx_contracts.py` (no transcription command is ever sent and a
stray carrier speech event is closed without its text). This file covers C2 through the API.
"""

import pytest
from app.leads.record import number_hold
from app.optimization.geography import us_area
from journey import get, import_leads, reserve

ROW = {
    "name": "Casey Example",
    "company": "Northwind",
    "phone": "+16175550100",
    "title": "VP Sales",
}


@pytest.mark.parametrize(
    ("phone", "area"),
    [
        ("+16175550100", "state"),  # Massachusetts
        ("+12025550100", "state"),  # District of Columbia
        ("+18085550100", "state"),  # Hawaii
        ("+18005550100", "toll_free"),
        ("+14165550100", None),  # Toronto
        ("+16045550100", None),  # Vancouver
        ("+18765550100", None),  # Jamaica
        ("+17875550100", None),  # Puerto Rico
        ("+16715550100", None),  # Guam
        ("+19005550100", None),  # premium rate
        ("+442071234567", None),
    ],
)
def test_only_the_50_states_dc_and_toll_free_are_us_numbers(phone, area):
    assert us_area(phone) == area
    assert (number_hold({"phone": phone}) == "OUTSIDE_US") is (area is None)


def lead_named(ws, name):
    page = get(ws, "/v1/workspace/leads", params={"q": name})
    return next(lead for lead in page["items"] if lead["name"] == name)


def test_a_number_outside_the_us_is_held_and_the_lead_says_why(workspace):
    import_leads(workspace, [{**ROW, "name": "Toronto Lead", "phone": "+14165550123"}], list_name="Canada")
    lead = lead_named(workspace, "Toronto Lead")
    assert lead["number_hold"] == "OUTSIDE_US"
    assert "outside the 50 US states and DC" in lead["number_hold_reason"]
    response = reserve(workspace, list_names=["Canada"])
    assert response.status_code == 409, response.text
    assert "OUTSIDE_US" in response.text


def test_a_toll_free_number_stays_callable(workspace):
    import_leads(
        workspace, [{**ROW, "name": "Toll Free Lead", "phone": "+18885550123"}], list_name="Toll free"
    )
    assert lead_named(workspace, "Toll Free Lead")["number_hold"] is None
