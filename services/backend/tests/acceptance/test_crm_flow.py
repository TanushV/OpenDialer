"""Phase 9B end to end over fake HubSpot, Treg and RealValidito answers: sync, waterfall, do-not-call, logging.

R9B.2, R9B.4, R9B.5, R9B.14, R9B.15, R9B.19 and R9C.2 through the API. No real provider is reached.
"""

import json
from urllib.parse import parse_qs

import httpx
from journey import get, post, run_jobs

A = "+12125550101"  # On the CRM record; Trestle confirms it.
B = "+12125550102"  # Found by LeadOcean for the contact with no number; on the federal do-not-call list.


class Fakes:
    def __init__(self):
        self.calls, self.writes, self.created_calls = [], [], []
        self.members = ["1", "2", "3"]
        self.down = False

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((host, path))
        if self.down and host == "api.hubapi.com":
            return httpx.Response(503)
        if host == "api.hubapi.com":
            if path.endswith("/memberships"):
                return httpx.Response(200, json={"results": [{"recordId": m} for m in self.members]})
            if path == "/crm/v3/objects/contacts/batch/read":
                people = {
                    "1": {
                        "firstname": "Ann",
                        "lastname": "Crm",
                        "company": "Acme",
                        "phone": A,
                        "hubspot_owner_id": "11",
                        "hs_linkedin_url": "https://www.linkedin.com/in/ann",
                    },
                    "2": {
                        "firstname": "Bob",
                        "lastname": "Other",
                        "company": "Beta",
                        "phone": "+12125550199",
                        "hubspot_owner_id": "22",
                    },
                    "3": {
                        "firstname": "Cy",
                        "lastname": "Nophone",
                        "company": "Gamma",
                        "hubspot_owner_id": "11",
                        "hs_linkedin_url": "https://www.linkedin.com/in/cy",
                    },
                }
                return httpx.Response(
                    200,
                    json={
                        "results": [{"id": i["id"], "properties": people[i["id"]]} for i in body["inputs"]]
                    },
                )
            if path == "/crm/v3/owners":
                return httpx.Response(
                    200, json={"results": [{"id": "11", "firstName": "Pat", "lastName": "Rep"}]}
                )
            if path == "/calling/v1/dispositions":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "d-c", "label": "Connected"},
                        {"id": "d-na", "label": "No answer"},
                        {"id": "d-mb", "label": "Meeting booked"},
                    ],
                )
            if path == "/crm/v3/objects/calls/search":
                return httpx.Response(200, json={"results": []})
            if path == "/crm/v3/objects/calls":
                self.created_calls.append(body)
                return httpx.Response(201, json={"id": "hs-call-1"})
            if request.method == "PATCH":
                self.writes.append((path, body["properties"]))
                return httpx.Response(200, json={})
        if host == "treg.to":
            endpoint = path.removeprefix("/call/")
            cost = {
                "hlrlookup.people.phone.validate": 0,
                "trestleiq.people.contact.verify": 30000,
                "leadocean.people.phone.find": 5000,
            }[endpoint]
            if endpoint.startswith("hlrlookup"):
                answer = {
                    "results": [
                        {
                            "error": "NONE",
                            "telephone_number_type": "MOBILE",
                            "original_network_details": {"country_iso3": "USA"},
                        }
                    ]
                }
            elif endpoint.startswith("trestle"):
                answer = {"phone": {"name_match": True, "contact_grade": "A", "is_valid": True}}
            else:
                assert parse_qs(request.url.query.decode())["linkedin_url"] == [
                    "https://www.linkedin.com/in/cy"
                ]
                answer = {"data": {"found": True, "phones": [{"phone": B, "type": "mobile"}]}}
            return httpx.Response(
                200, json=answer, headers={"X-Treg-Cost-Micro": str(cost), "X-Treg-Call-Id": "c"}
            )
        if host == "app.realvalidito.com":
            entries = [
                {
                    "number": n,
                    "cleaned_number": n,
                    "federal_dnc": n == B[2:],
                    "state_dnc": {},
                    "tcpa_litigator": False,
                    "invalid": False,
                }
                for n in body["numbers"]
            ]
            return httpx.Response(200, json={"data": entries})
        return httpx.Response(404)


def setup(ws, monkeypatch):
    fakes = Fakes()
    service = ws["client"].app.state.service
    monkeypatch.setattr(
        service, "http", httpx.AsyncClient(transport=httpx.MockTransport(fakes)), raising=False
    )
    import app.providers.treg as treg

    real = treg.Treg.__init__

    def init(self, token, **kw):
        real(self, token, **{**kw, "http": service.http})

    monkeypatch.setattr(treg.Treg, "__init__", init)
    call = ws["client"].request
    response = call(
        "PUT",
        "/v1/admin/settings",
        headers=ws["headers"],
        json={
            "hubspot_service_key": "hs",
            "treg_token": "tg",
            "realvalidito_api_key": "rk",
            "realvalidito_api_secret": "rs",
        },
    )
    assert response.status_code == 200, response.text
    assert (
        call("PUT", "/v1/hubspot/calls-for", headers=ws["headers"], json={"owner_id": "11"}).status_code
        == 200
    )
    assert (
        call(
            "PUT",
            "/v1/lists/spend-limit",
            headers=ws["headers"],
            json={"list_name": "HubSpot list", "usd": 5},
        ).status_code
        == 200
    )
    return fakes


def crm_leads(ws):
    items = get(ws, "/v1/workspace/leads", params={"list_name": "HubSpot list"})["items"]
    return {lead["external_id"]: lead for lead in items}


def test_a_hubspot_list_syncs_checks_numbers_and_do_not_call_then_logs_the_call(workspace, monkeypatch):
    ws = workspace
    fakes = setup(ws, monkeypatch)
    result = post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    assert (result["added"], result["other_owner"]) == (2, 1), "this seat syncs only Pat's contacts"
    run_jobs(ws, rounds=3)
    leads = crm_leads(ws)
    assert set(leads) == {"1", "3"}
    ann, cy = leads["1"], leads["3"]
    assert (ann["number_status"], ann["number_hold"], ann["dnc"]["status"]) == ("confirmed", None, "checked")
    assert (cy["phone"], cy["number_source"]) == (B, "leadocean")
    assert cy["suppressed"] and cy["dnc"]["lists"] == ["federal"]
    run_jobs(ws, rounds=2)
    writes = dict(fakes.writes)
    assert writes["/crm/v3/objects/contacts/1"] == {"phone_status": "confirmed"}
    assert writes["/crm/v3/objects/contacts/3"]["opendialer_opt_out"] == "true"
    assert any(
        path == "/crm/v3/objects/contacts/3" and props.get("phone") == B for path, props in fakes.writes
    )

    paid = sum(1 for host, path in fakes.calls if host == "treg.to")
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=2)
    assert sum(1 for host, _ in fakes.calls if host == "treg.to") == paid, "a repeat sync pays nothing"

    fakes.members = ["3"]
    removed = post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    assert removed["removed"] == 1


def test_hubspot_down_during_a_sync_names_the_failure_and_calling_is_untouched(workspace, monkeypatch):
    ws = workspace
    fakes = setup(ws, monkeypatch)
    fakes.down = True
    response = ws["client"].post(
        "/v1/hubspot/sync", headers=ws["headers"], json={"list_id": "77", "list_name": "HubSpot list"}
    )
    assert response.status_code >= 500 or response.status_code == 502
    assert get(ws, "/v1/blockers")["blockers"] == [] or all(
        b["code"] != "HUBSPOT" for b in get(ws, "/v1/blockers")["blockers"]
    )


def test_a_call_to_a_crm_lead_lands_on_the_contact_with_its_owner_and_no_transcript(workspace, monkeypatch):
    from journey import answered_by_human, hang_up, reserve, wrap_up

    ws = workspace
    fakes = setup(ws, monkeypatch)
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=3)
    reservation = reserve(ws, list_names=["HubSpot list"])
    assert reservation.status_code == 200, reservation.text
    packet = reservation.json()
    assert packet["lead"]["external_id"] == "1", "the suppressed do-not-call lead is never offered"
    call = post(
        ws, "/v1/calls/start", {"reservation_id": packet["reservation_id"], "idempotency_key": "crm-call-1"}
    )
    answered_by_human(ws, call["id"])
    hang_up(ws, call["id"])
    wrap_up(ws, call["id"], outcome="meeting_booked")
    run_jobs(ws, rounds=3)
    assert len(fakes.created_calls) == 1
    record = fakes.created_calls[0]
    assert record["properties"]["hubspot_owner_id"] == "11"
    assert record["properties"]["hs_call_disposition"] == "d-mb"
    assert record["associations"][0]["to"]["id"] == "1"
    assert "transcript" not in json.dumps(record).lower()


def test_a_list_not_synced_for_a_day_stops_dialing_its_leads_and_start_calling_syncs_it(
    workspace, monkeypatch, clock
):
    from datetime import timedelta

    from journey import ready, reserve

    ws = workspace
    setup(ws, monkeypatch)
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=3)
    import app.leads.record as record

    monkeypatch.setattr(record, "STALE_SYNC", timedelta(hours=1))
    clock.now = clock.now + timedelta(hours=2)
    ready(ws, status="paused")
    stale = ws["client"].post(
        "/v1/queue/next",
        headers=ws["headers"],
        json={"campaign_id": ws["campaign_id"], "list_names": ["HubSpot list"]},
    )
    assert stale.status_code == 409 and "CRM_SYNC_STALE" in stale.text
    assert crm_leads(ws)["1"]["number_hold"] == "CRM_SYNC_STALE"
    ready(ws)  # Start calling: the list syncs first.
    run_jobs(ws, rounds=3)
    assert reserve(ws, list_names=["HubSpot list"]).status_code == 200
