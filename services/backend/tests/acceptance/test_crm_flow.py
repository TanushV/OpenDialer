"""Phase 9B end to end over fake HubSpot, Treg and RealValidito answers: sync, waterfall, do-not-call, logging.

R9B.2, R9B.4, R9B.5, R9B.14, R9B.15, R9B.19 and R9C.2 through the API. No real provider is reached.
"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from journey import get, post, ready, run_jobs

A = "+12125550101"  # Already on the CRM record.
B = "+12125550102"  # Found by LeadOcean for the contact with no number; on the federal do-not-call list.
C = "+12125550103"  # Saved by the rep in HubSpot.


class Fakes:
    def __init__(self):
        self.calls, self.writes, self.created_calls = [], [], []
        self.members = ["1", "3"]
        self.people = {
            "1": {
                "firstname": "Ann",
                "lastname": "Crm",
                "company": "Acme",
                "phone": A,
                "hubspot_owner_id": "11",
                "hs_linkedin_url": "https://www.linkedin.com/in/ann",
            },
            "3": {
                "firstname": "Cy",
                "lastname": "Nophone",
                "company": "Gamma",
                "hubspot_owner_id": "11",
                "hs_linkedin_url": "https://www.linkedin.com/in/cy",
            },
        }
        self.mobile_saved_during_lookup = None
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
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": item["id"],
                                "properties": {
                                    k: v
                                    for k, v in self.people[item["id"]].items()
                                    if k in body["properties"]
                                },
                            }
                            for item in body["inputs"]
                        ]
                    },
                )
            if path == "/calling/v1/dispositions":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "d-c", "label": "Connected"},
                        {"id": "d-na", "label": "No answer"},
                        {"id": "d-mb", "label": "Meeting booked"},
                        {"id": "d-wn", "label": "Wrong number"},
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
                if self.mobile_saved_during_lookup:
                    self.people["3"]["mobilephone"] = self.mobile_saved_during_lookup
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


@pytest.mark.slow
def test_a_rep_syncs_its_segment_and_dials_the_pool_binding_selected_by_the_relay(
    workspace, carrier, monkeypatch
):
    ws = workspace
    setup(ws, monkeypatch)
    managed = ws["client"].app.state.service.managed
    with monkeypatch.context() as startup:
        startup.setattr(managed, "config", {"tenant_id": ws["tenant_id"]})
        post(ws, "/v1/hubspot/sync", {"list_id": "88", "list_name": "Other rep"}, status=409)
    chosen = "+12125550190"
    managed.apply_pool_snapshot(
        ws["tenant_id"],
        {
            "id": "team",
            "segment_id": "77",
            "numbers": [
                {"phone": "+12125550189", "daily_limit": 90, "used": 0, "outbound_enabled": True},
                {"phone": chosen, "daily_limit": 90, "used": 1, "outbound_enabled": True},
            ],
        },
    )
    requests = []

    class RPC:
        def rpc(self, name, args):
            requests.append((name, args))

            async def execute():
                result = {"phone": chosen} if name == "relay_pool_reserve" else {"submitted": True}
                return SimpleNamespace(data=result)

            return SimpleNamespace(execute=execute)

    monkeypatch.setattr(managed, "client", RPC())
    assert post(ws, "/v1/hubspot/sync", {"list_id": "88", "list_name": "Other rep"}, status=403)
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=3)
    ready(ws)
    selected = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]})
    assert selected["lead"]["phone"] == A and selected["number"]["phone"] == chosen
    response = post(
        ws,
        "/v1/calls/start",
        {
            "reservation_id": selected["reservation_id"],
            "idempotency_key": "pool-call-1",
        },
    )
    assert response["number_id"] == selected["number"]["id"]
    assert carrier.dials[-1]["from"] == chosen
    assert [name for name, _ in requests] == ["relay_pool_reserve", "relay_pool_submit"]


def test_a_hubspot_list_syncs_checks_numbers_and_do_not_call_then_logs_the_call(workspace, monkeypatch):
    ws = workspace
    fakes = setup(ws, monkeypatch)
    result = post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    assert result["added"] == 2 and "other_owner" not in result
    run_jobs(ws, rounds=3)
    leads = crm_leads(ws)
    assert set(leads) == {"1", "3"}
    ann, cy = leads["1"], leads["3"]
    assert (ann["number_status"], ann["number_hold"], ann["dnc"]["status"]) == (
        "unconfirmed",
        None,
        "checked",
    )
    assert (cy["phone"], cy["number_source"]) == (B, "leadocean")
    assert cy["suppressed"] and cy["dnc"]["lists"] == ["federal"]
    run_jobs(ws, rounds=2)
    writes = dict(fakes.writes)
    assert writes["/crm/v3/objects/contacts/1"] == {"phone_status": "unconfirmed"}
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


def test_an_unusable_hubspot_phone_uses_the_saved_mobile_and_reformatting_keeps_it(
    workspace, carrier, monkeypatch
):
    ws = workspace
    fakes = setup(ws, monkeypatch)
    fakes.members = ["1"]
    fakes.people["1"].update(phone="N/A", mobilephone="(212) 555-0103")
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=3)
    lead = crm_leads(ws)["1"]
    assert (lead["phone"], lead["number_source"], lead["number_hold"]) == (C, "crm", None)
    assert lead["dnc"]["phone"] == C
    assert not any(host == "treg.to" for host, _ in fakes.calls)
    assert all("phone" not in props and "mobilephone" not in props for _, props in fakes.writes)

    previous_requests = [path for host, path in fakes.calls if host == "treg.to"]
    fakes.people["1"]["mobilephone"] = "+1 212-555-0103"
    synced = post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    assert synced["updated"] == 0
    run_jobs(ws)
    assert [path for host, path in fakes.calls if host == "treg.to"] == previous_requests
    ready(ws)
    reservation = post(
        ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"], "list_names": ["HubSpot list"]}
    )
    assert reservation["lead"]["id"] == lead["id"]
    post(
        ws,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": "saved-mobile"},
    )
    assert carrier.dials[-1]["to"] == C


def test_removing_a_saved_phone_invalidates_the_reservation_and_the_replacement_gets_its_own_check(
    workspace, carrier, monkeypatch
):
    ws = workspace
    fakes = setup(ws, monkeypatch)
    fakes.members = ["1"]
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=3)
    ready(ws)
    selection = {"campaign_id": ws["campaign_id"], "list_names": ["HubSpot list"]}
    reservation = post(ws, "/v1/queue/next", selection)
    assert reservation["lead"]["phone"] == A

    fakes.people["1"]["phone"] = None
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    assert crm_leads(ws)["1"]["phone"] == ""
    post(
        ws,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": "removed-phone"},
        status=409,
    )
    assert not carrier.dials
    post(ws, "/v1/queue/release")

    fakes.people["1"]["phone"] = "212-555-0103"
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    lead = crm_leads(ws)["1"]
    assert (lead["id"], lead["phone"], lead["number_hold"]) == (
        reservation["lead"]["id"],
        C,
        "DNC_NOT_CHECKED",
    )
    post(ws, "/v1/queue/next", selection, status=409)
    run_jobs(ws, rounds=3)
    assert crm_leads(ws)["1"]["dnc"]["phone"] == C
    assert post(ws, "/v1/queue/next", selection)["lead"]["phone"] == C


def test_enrichment_skips_phone_writeback_when_hubspot_received_a_mobile_during_lookup(
    workspace, monkeypatch
):
    ws = workspace
    fakes = setup(ws, monkeypatch)
    fakes.members = ["3"]
    fakes.mobile_saved_during_lookup = C
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=4)
    assert fakes.people["3"]["mobilephone"] == C
    assert any("phone_status" in props for _, props in fakes.writes)
    assert all("phone" not in props for _, props in fakes.writes)


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


@pytest.mark.parametrize("outcome, disposition", [("meeting_booked", "d-mb"), ("wrong_party", "d-wn")])
def test_a_crm_call_records_the_dialed_number_after_a_hubspot_edit(
    workspace, monkeypatch, outcome, disposition
):
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
    fakes.people["1"]["phone"] = C
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    wrap_up(ws, call["id"], outcome=outcome)
    run_jobs(ws, rounds=3)
    assert len(fakes.created_calls) == 1
    record = fakes.created_calls[0]
    assert "hubspot_owner_id" not in record["properties"]
    assert record["properties"]["hs_call_disposition"] == disposition
    assert record["properties"]["hs_call_to_number"] == A
    assert record["associations"][0]["to"]["id"] == "1"
    assert "transcript" not in json.dumps(record).lower()
    lead = crm_leads(ws)["1"]
    assert lead["phone"] == C and not lead["suppressed"]
    assert lead["dnc"]["phone"] == C


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


def test_checking_the_hubspot_key_creates_the_apps_fields_once(workspace, monkeypatch):
    ws = workspace
    service = ws["client"].app.state.service
    made = []

    def respond(request):
        if request.url.path == "/crm/v3/lists/search":
            return httpx.Response(200, json={"lists": []})
        if request.method == "GET":
            return httpx.Response(404)
        made.append(json.loads(request.content)["name"])
        return httpx.Response(201, json={})

    setup(ws, monkeypatch)
    monkeypatch.setattr(
        service, "http", httpx.AsyncClient(transport=httpx.MockTransport(respond)), raising=False
    )
    monkeypatch.setattr(service.settings, "simulation", False)
    rows = post(ws, "/v1/settings/rows/hubspot/check")
    row = next(r for r in rows["rows"] if r["id"] == "hubspot")
    assert row["state"] == "ok" and "created 4 fields" in row["detail"]
    assert set(made) == {
        "phone_status",
        "opendialer_opt_out",
        "opendialer_opt_out_reason",
        "opendialer_call_id",
    }


def test_a_test_call_to_the_owners_hubspot_contact_is_written_to_hubspot(workspace, monkeypatch):
    """R11.7 (section 9B): a test session dials only the rep's own phone, so a test call to a HubSpot contact is the
    owner's test contact; it is written, which lets L15 to L18 run on the owner's line."""
    from journey import answered_by_human, hang_up, reserve, wrap_up

    ws = workspace
    fakes = setup(ws, monkeypatch)
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    run_jobs(ws, rounds=3)
    response = ws["client"].put("/v1/admin/settings", headers=ws["headers"], json={"test_phone": A})
    assert response.status_code == 200, response.text
    post(ws, "/v1/settings/test-call")
    reservation = reserve(ws)
    assert reservation.status_code == 200, reservation.text
    packet = reservation.json()
    assert packet["lead"]["external_id"] == "1" and packet["lead"]["phone"] == A
    call = post(
        ws, "/v1/calls/start", {"reservation_id": packet["reservation_id"], "idempotency_key": "crm-test-1"}
    )
    answered_by_human(ws, call["id"])
    hang_up(ws, call["id"])
    wrap_up(ws, call["id"], outcome="wrong_party")
    run_jobs(ws, rounds=3)
    assert get(ws, f"/v1/calls/{call['id']}")["is_test"] is True
    assert len(fakes.created_calls) == 1
    assert ("/crm/v3/objects/contacts/1", {"phone_status": "wrong_number"}) in fakes.writes


@pytest.mark.parametrize("retry_scheduled", [False, True])
def test_a_test_call_uses_the_saved_hubspot_phone_before_screening(
    workspace, carrier, monkeypatch, clock, retry_scheduled
):
    from datetime import timedelta

    from app import ops
    from app.platform.db import update
    from helpers import principal
    from journey import reserve

    ws = workspace
    fakes = setup(ws, monkeypatch)
    fakes.members = ["1"]
    post(
        ws,
        "/v1/leads/import",
        {
            "campaign_id": ws["campaign_id"],
            "list_name": "Test line",
            "rows": [{"name": "Older CSV copy", "phone": A}],
            "prepare": False,
        },
    )
    post(ws, "/v1/hubspot/sync", {"list_id": "77", "list_name": "HubSpot list"})
    ready(ws)
    held = reserve(ws, list_names=["HubSpot list"])
    assert held.status_code == 409 and "DNC_NOT_CHECKED" in held.text

    # A previous Test session left the built-in test number in the Test line list.
    client, headers = ws["client"], ws["headers"]
    assert client.put("/v1/admin/settings", headers=headers, json={"test_phone": "+12038211101"}).is_success
    post(ws, "/v1/settings/test-call")
    assert client.put("/v1/admin/settings", headers=headers, json={"test_phone": A}).is_success
    started = post(ws, "/v1/settings/test-call")
    assert started["lead_id"] == crm_leads(ws)["1"]["id"]
    if retry_scheduled:
        # Maintenance can schedule a CRM lead again while the Test session is still armed.
        service = client.app.state.service
        with service.db.transaction() as s:
            lead = ops.required(service, s, "leads", started["lead_id"], principal(ws))
            update(lead, next_attempt_at=(clock.now + timedelta(days=1)).isoformat())

    assert get(ws, "/v1/calling/readiness")["callable_now"] == 1
    packet = reserve(ws)
    assert packet.status_code == 200, packet.text
    packet = packet.json()
    assert packet["lead"]["id"] == started["lead_id"] and packet["lead"]["phone"] == A
    call = post(
        ws, "/v1/calls/start", {"reservation_id": packet["reservation_id"], "idempotency_key": "test"}
    )
    assert call["is_test"] and [dial["to"] for dial in carrier.dials] == [A]
    assert crm_leads(ws)["1"]["number_hold"] == "DNC_NOT_CHECKED"
