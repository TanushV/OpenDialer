"""R3.11 and R8.2 groundwork: new leads carry their source and number status; test calls are marked at the source."""

from app.platform.db import MODELS
from journey import get, hang_up, import_leads, start, wrap_up


def stored(ws, kind, object_id):
    with ws["client"].app.state.service.db.transaction(False) as s:
        return s.get(MODELS[kind], object_id).data


def test_a_csv_lead_records_its_source_and_is_not_number_checked(workspace):
    ws = workspace
    import_leads(ws, [{"name": "Ada Lovelace", "phone": "+12025550111", "company": "Analytical Example"}])
    lead = get(ws, "/v1/workspace/leads", params={"q": "Ada"})["items"][0]
    data = stored(ws, "leads", lead["id"])
    assert (data["source"], data["external_id"], data["signals"], data["number_status"]) == (
        "csv",
        None,
        {"aligned": True, "items": []},
        "not_checked",
    )


def test_a_call_to_a_prospect_is_not_a_test_call_until_marked_as_a_delivery_test(workspace):
    ws = workspace
    call = start(ws)
    assert stored(ws, "call_attempts", call["id"])["is_test"] is False
    hang_up(ws, call["id"])
    wrap_up(ws, call["id"], outcome="no_answer", delivery_test=True)
    assert stored(ws, "call_attempts", call["id"])["is_test"] is True


def test_a_call_to_the_owners_test_line_is_a_test_call(workspace):
    ws = workspace
    import_leads(ws, [{"name": "Owner", "phone": "+12038211101", "company": "Test line"}])
    owner = get(ws, "/v1/workspace/leads", params={"q": "Owner"})["items"][0]
    service = ws["client"].app.state.service
    with service.db.transaction() as s:
        for lead in s.query(MODELS["leads"]).all():
            if lead.id != owner["id"]:
                lead.data = {**lead.data, "status": "archived", "archived": True}
    call = start(ws, key="owner-line")
    assert call["lead_id"] == owner["id"]
    assert stored(ws, "call_attempts", call["id"])["is_test"] is True
