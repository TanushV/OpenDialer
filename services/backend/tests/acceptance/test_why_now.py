"""R9.8: a prepared lead carries its checked trigger and bridge into the call packet."""

from journey import get, import_leads, run_jobs


def test_a_prepared_lead_has_its_trigger_and_bridge_in_the_packet(workspace):
    result = import_leads(
        workspace,
        [
            {
                "name": "Wren Why",
                "phone": "+14155550177",
                "company": "Why Co",
                "timezone": "America/Los_Angeles",
            }
        ],
        list_name="Why now",
        prepare=True,
    )
    lead_id = result["leads"][0]["id"]
    run_jobs(workspace, rounds=3)
    lead = get(workspace, f"/v1/leads/{lead_id}/packet")["lead"]
    assert lead["trigger"]["type"] == "none"
    assert lead["bridge"]["text"].startswith("I looked at what Why Co")
    assert lead["trigger_label"] == "No trigger found"
