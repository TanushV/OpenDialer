"""Adding and buying numbers set them up; one workspace test call opens them all, over HTTP."""

import asyncio
import base64
import json
from datetime import timedelta

import httpx
import pytest
from app.platform.db import get as db_get
from app.platform.db import update
from journey import call, get, post, run_jobs
from test_feature_calling import Telnyx, add_user, inbound, inbound_task, ready_as

ROUTE = "test-route"
SF, NYC, OTHER = "+16282564157", "+19293799649", "+16505550123"


def connect(ws, **extra):
    call(ws, "PUT", "/v1/admin/settings", json={"telnyx_connection_id": ROUTE, **extra})


def own_numbers(carrier):
    row = {"status": "active", "call_recording_enabled": False}
    carrier.numbers = {
        "sf": {"id": "sf", "phone_number": SF, "connection_id": None, **row},
        "nyc": {"id": "nyc", "phone_number": NYC, "connection_id": ROUTE, **row},
        "other": {"id": "other", "phone_number": OTHER, "connection_id": "another-app", **row},
    }


def numbers(ws):
    return {n["phone"]: n for n in get(ws, "/v1/workspace/numbers")["items"]}


@pytest.fixture
def fresh(client):
    """A real first run: no seeded numbers and no test call yet."""
    r = client.post(
        "/v1/setup",
        headers={"X-Local-Token": "test-local-secret"},
        json={
            "seller_name": "Example Seller",
            "campaign_name": "Acceptance campaign",
            "email": "operator@example.test",
            "password": "a-test-only-password-123",
        },
    )
    assert r.status_code == 200, r.text
    ws = {"client": client, "headers": {"Authorization": f"Bearer {r.json()['access_token']}"}, **r.json()}
    ws["campaign_id"] = get(ws, "/v1/workspace")["campaign"]["id"]
    rows = [{"name": "Blue Bottle", "phone": "+14155550177", "company": "Blue Bottle"}]
    post(ws, "/v1/leads/import", {"campaign_id": ws["campaign_id"], "rows": rows})
    return ws


def sign_in_again(ws):
    token = ws["client"].post(
        "/v1/auth/session",
        json={"email": "operator@example.test", "password": "a-test-only-password-123"},
    )
    ws["headers"] = {"Authorization": "Bearer " + token.json()["access_token"]}


def pause_seeded(ws):
    for phone in ("+14155550100", "+12125550100"):
        call(
            ws,
            "PATCH",
            f"/v1/admin/numbers/{numbers(ws)[phone]['id']}",
            json={"lifecycle": "paused_for_outbound", "reason": "Only the numbers under test"},
        )


def answered_test_call(ws, telnyx, to, tag="test-in", caller="+12025550123"):
    telnyx.deliver("call.initiated", inbound(tag, caller, to=to), "evt-" + tag)
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    task = inbound_task(ws, tag)
    leg = {"inbound_task": task["id"], "attempt": task["routing_attempts"][0]["id"]}
    state = base64.b64encode(json.dumps(leg).encode()).decode()
    telnyx.deliver(
        "call.answered", {"call_control_id": tag + "-leg", "client_state": state}, "evt-answer-" + tag
    )
    telnyx.deliver("call.bridged", {"call_control_id": tag + "-leg", "client_state": state}, "evt-leg-" + tag)
    run_jobs(ws)
    telnyx.deliver(
        "call.hangup", {"call_control_id": tag + "-leg", "client_state": state}, "evt-leg-end-" + tag
    )
    telnyx.deliver("call.hangup", {"call_control_id": tag}, "evt-end-" + tag)
    run_jobs(ws)


def test_telnyx_numbers_are_listed_and_added_only_on_request(workspace, carrier):
    ws = workspace
    connect(ws)
    own_numbers(carrier)
    listing = post(ws, "/v1/admin/numbers/sync")
    assert {n["phone"]: n["route"] for n in listing["owned"]} == {
        SF: "unassigned",
        NYC: "this_app",
        OTHER: "other_app",
    }
    assert not {n["phone"] for n in listing["owned"] if n["added"]}
    assert not {SF, NYC, OTHER} & set(numbers(ws))

    added = post(ws, "/v1/admin/numbers/add", {"phone": SF})
    assert added["lifecycle"] == "limited_real_use" and added["configuration_status"] == "confirmed"
    assert added["warmup_started_at"] and carrier.numbers["sf"]["connection_id"] == ROUTE
    relisted = post(ws, "/v1/admin/numbers/sync")
    assert {n["phone"] for n in relisted["owned"] if n["added"]} == {SF}

    refused = post(ws, "/v1/admin/numbers/add", {"phone": OTHER}, status=409)
    assert refused["detail"]["code"] == "NUMBER_ON_ANOTHER_APP"
    assert carrier.numbers["other"]["connection_id"] == "another-app"
    assert OTHER not in numbers(ws)
    moved = post(ws, "/v1/admin/numbers/add", {"phone": OTHER, "move_from_other_app": True})
    assert moved["lifecycle"] == "limited_real_use"
    assert carrier.numbers["other"]["connection_id"] == ROUTE

    post(ws, "/v1/admin/numbers/add", {"phone": "+13035550100"}, status=404)
    post(ws, "/v1/admin/numbers", {"phone": "+16505550199"}, status=404)


def test_a_bought_number_is_ready_to_warm_up_without_another_click(workspace, carrier):
    ws = workspace
    connect(ws, maximum_owned_numbers=9, number_initial_limit_usd=9, number_monthly_limit_usd=9)
    quote = post(ws, "/v1/admin/numbers/search", {"region": "929"})["numbers"][0]
    order = post(ws, "/v1/admin/numbers/purchase", {"quote_id": quote["id"], "approve_recurring": True})
    assert order["status"] == "ordered"
    number = numbers(ws)[quote["phone"]]
    assert number["lifecycle"] == "limited_real_use" and number["configuration_status"] == "confirmed"
    assert number["connection_id"] == ROUTE


def test_a_number_that_arrives_late_is_set_up_when_the_order_is_checked(workspace, carrier, monkeypatch):
    ws = workspace
    connect(ws, maximum_owned_numbers=9, number_initial_limit_usd=9, number_monthly_limit_usd=9)
    arrives = carrier.list_numbers

    async def not_yet():
        return []

    monkeypatch.setattr(carrier, "list_numbers", not_yet)
    quote = post(ws, "/v1/admin/numbers/search", {"region": "929"})["numbers"][0]
    order = post(ws, "/v1/admin/numbers/purchase", {"quote_id": quote["id"], "approve_recurring": True})
    assert quote["phone"] not in numbers(ws)
    monkeypatch.setattr(carrier, "list_numbers", arrives)
    post(ws, f"/v1/admin/numbers/orders/{order['id']}/reconcile")
    assert numbers(ws)[quote["phone"]]["lifecycle"] == "limited_real_use"


def test_an_initially_pending_order_reports_current_number_activation(workspace, carrier, monkeypatch):
    ws = workspace
    connect(ws, maximum_owned_numbers=9, number_initial_limit_usd=9, number_monthly_limit_usd=9)
    purchase = carrier.purchase_number

    async def pending(phone, reference):
        receipt = await purchase(phone, reference)
        number = next(n for n in carrier.numbers.values() if n["phone_number"] == phone)
        number["status"] = "purchase-pending"
        return {**receipt, "status": "pending"}

    monkeypatch.setattr(carrier, "purchase_number", pending)
    quote = post(ws, "/v1/admin/numbers/search", {"region": "929"})["numbers"][0]
    order = post(ws, "/v1/admin/numbers/purchase", {"quote_id": quote["id"], "approve_recurring": True})
    assert order["provider_status"] == "pending"
    [waiting] = get(ws, "/v1/admin/numbers/orders")
    assert not waiting["ready"] and waiting["number_status"] == "purchase-pending"

    number = next(n for n in carrier.numbers.values() if n["phone_number"] == quote["phone"])
    number["status"] = "active"
    post(ws, "/v1/admin/numbers/sync")
    [active] = get(ws, "/v1/admin/numbers/orders")
    assert active["ready"] and active["number_status"] == "active"
    assert len(carrier.purchases) == 1


@pytest.mark.slow
def test_one_answered_test_call_opens_every_number_immediately_and_never_expires(fresh, app, carrier, clock):
    ws = fresh
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    post(ws, "/v1/admin/numbers/add", {"phone": NYC})
    board = numbers(ws)
    assert not board[SF]["outbound_ready"] and not board[NYC]["outbound_ready"]
    # The upgrade must release numbers that already have an unexpired app hold.
    with app.state.db.transaction() as s:
        number = db_get(s, "number_inventory", board[SF]["id"], ws["tenant_id"])
        update(number, outbound_hold_until=(clock() + timedelta(hours=24)).isoformat())
    assert get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"] is False
    assert "CALLER_NUMBER_CALLBACK_TEST_REQUIRED" in get(ws, "/v1/admin/preflight")["blockers"]

    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    answered_test_call(ws, Telnyx(ws, clock), SF)
    assert get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"] is True
    board = numbers(ws)
    assert all(board[phone]["outbound_ready"] for phone in (SF, NYC))

    clock.now += timedelta(days=45)
    sign_in_again(ws)
    board = numbers(ws)
    assert board[SF]["outbound_ready"] and board[NYC]["outbound_ready"]
    assert get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"] is True


def test_callers_never_dialed_hear_the_greeting_and_do_not_ring_reps(workspace, clock, carrier):
    ws = workspace
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    telnyx = Telnyx(ws, clock)
    telnyx.deliver("call.initiated", inbound("stranger", "+12025550123"), "evt-stranger")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    task = inbound_task(ws, "stranger")
    assert task["route"] == "callback_queue" and task["routed_rep_id"] is None
    assert not [a for a in carrier.actions if a["action"] == "transfer"]
    spoken = [a["payload"]["payload"] for a in carrier.actions if a["action"] == "speak"]
    assert spoken == [
        "Thanks for calling Example Seller. No one can take your call right now, and we will follow up. Goodbye."
    ]

    lead = get(ws, "/v1/workspace/leads")["items"][0]
    telnyx.deliver("call.initiated", inbound("known", lead["phone"]), "evt-known")
    run_jobs(ws)
    assert inbound_task(ws, "known")["routed_rep_id"]


def test_until_the_test_call_passes_any_caller_rings_a_rep(fresh, carrier, clock):
    ws = fresh
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    Telnyx(ws, clock).deliver("call.initiated", inbound("first", "+12025550123", to=SF), "evt-first")
    run_jobs(ws)
    assert inbound_task(ws, "first")["routed_rep_id"]


def test_a_saved_live_test_without_answer_evidence_needs_a_retest(fresh, app, clock):
    ws = fresh
    seller_id = get(ws, "/v1/workspace")["seller"]["id"]
    with app.state.db.transaction() as s:
        seller = db_get(s, "sellers", seller_id, ws["tenant_id"])
        update(seller, callback_test={"at": clock().isoformat(), "simulated": False})

    assert not get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"]


@pytest.mark.parametrize("result", ["answered", "unanswered"])
def test_an_early_transfer_bridge_is_not_an_answer(fresh, carrier, clock, result):
    ws = fresh
    connect(ws, sip_username="workspace-audio")
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    ready_as(ws)
    telnyx = Telnyx(ws, clock)
    telnyx.deliver("call.initiated", inbound("early", "+12025550123", to=SF), "evt-early")
    run_jobs(ws)
    post(ws, "/v1/admin/control/run")
    task = inbound_task(ws, "early")
    leg = {"inbound_task": task["id"], "attempt": task["routing_attempts"][0]["id"]}
    payload = {
        "call_control_id": "early-leg",
        "client_state": base64.b64encode(json.dumps(leg).encode()).decode(),
    }
    telnyx.deliver("call.bridged", payload, "evt-early-bridge")
    run_jobs(ws)

    assert inbound_task(ws, "early")["inbound_state"] == "ringing"
    assert not get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"]
    assert [a["payload"]["early_media"] for a in carrier.actions if a["action"] == "transfer"] == [False]

    if result == "answered":
        telnyx.deliver("call.answered", payload, "evt-early-answer")
        run_jobs(ws)
        assert inbound_task(ws, "early")["inbound_state"] == "connected"
        assert get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"]
    else:
        telnyx.deliver("call.hangup", {**payload, "sip_hangup_cause": "487"}, "evt-early-hangup")
        run_jobs(ws)
        assert inbound_task(ws, "early")["route"] == "callback_queue"
        assert not get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"]


def test_first_inbound_call_uses_the_ready_owners_workspace_sip(fresh, carrier, clock):
    ws = fresh
    connect(ws, sip_username="workspace-audio")
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    owner = get(ws, "/v1/workspace")["user"]["id"]
    ready_as(ws)
    answered_test_call(ws, Telnyx(ws, clock), SF)

    assert inbound_task(ws, "test-in")["routed_rep_id"] == owner
    transfers = [action["payload"]["to"] for action in carrier.actions if action["action"] == "transfer"]
    assert transfers == ["sip:workspace-audio@sip.telnyx.com"]
    assert get(ws, "/v1/admin/numbers/summary")["inventory"]["workspace_test_passed"]


def test_rep_without_sip_does_not_inherit_the_workspace_sip(fresh, carrier, clock):
    ws = fresh
    connect(ws, sip_username="workspace-audio")
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    _, rep = add_user(ws, "rep@example.test")
    ready_as(ws, rep)
    Telnyx(ws, clock).deliver("call.initiated", inbound("first", "+12025550123", to=SF), "evt-first")
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")

    assert inbound_task(ws, "first")["route"] == "callback_queue"
    assert not [action for action in carrier.actions if action["action"] == "transfer"]


@pytest.mark.slow
def test_a_new_number_warms_up_from_15_a_day_toward_60_and_shows_it(workspace, carrier):
    ws = workspace
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    number = numbers(ws)[SF]
    assert number["daily_limit"] == 60 and number["today_limit"] == 15


def test_caller_name_is_optional_and_applied_to_every_number(workspace, carrier):
    ws = workspace
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    assert carrier.caller_names == {}
    assert numbers(ws)[SF].get("caller_name") is None

    saved = call(ws, "PUT", "/v1/admin/caller-name", json={"name": "Agite"})
    assert saved["name"] == "AGITE" and carrier.caller_names == {"sf": "AGITE"}
    assert numbers(ws)[SF]["caller_name"]["status"] == "submitted"
    post(ws, "/v1/admin/numbers/add", {"phone": NYC})
    assert carrier.caller_names == {"sf": "AGITE", "nyc": "AGITE"}
    assert get(ws, "/v1/admin/settings")["caller_name"] == "AGITE"

    call(ws, "PUT", "/v1/admin/caller-name", json={"name": None})
    assert carrier.caller_names == {"sf": None, "nyc": None}
    assert not get(ws, "/v1/admin/settings")["caller_name"]
    call(ws, "PUT", "/v1/admin/caller-name", json={"name": "not allowed!"}, status=422)


def test_a_refused_next_call_says_when_a_number_is_the_blocker(workspace):
    ws = workspace
    ready_as(ws)
    pause_seeded(ws)
    refused = call(ws, "POST", "/v1/queue/next", json={"campaign_id": ws["campaign_id"]}, status=409)
    assert refused["detail"]["code"] == "CALLER_NUMBER_PAUSED"
    assert "Numbers" in refused["detail"]["message"] and refused["detail"]["fix"] == "numbers"


def test_a_refused_next_call_asks_for_the_test_call(fresh, carrier):
    ws = fresh
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    ready_as(ws)
    refused = call(ws, "POST", "/v1/queue/next", json={"campaign_id": ws["campaign_id"]}, status=409)
    assert "test call" in refused["detail"]["message"] and SF in refused["detail"]["message"]


def test_queue_uses_another_state_after_the_workspace_test_passes(fresh, carrier, clock):
    ws = fresh
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": NYC})
    _, rep = add_user(ws, "rep@example.test", sip="rep-line-1")
    ready_as(ws, rep)
    answered_test_call(ws, Telnyx(ws, clock), NYC)
    clock.now += timedelta(hours=24)
    sign_in_again(ws)
    ready_as(ws)
    assert get(ws, "/v1/admin/preflight")["screening"]["dialable_queue_count"] == 1
    selected = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]})
    assert selected["lead"]["state"] == "CA" and selected["number"]["phone"] == NYC


def test_number_search_can_name_a_city_and_shows_each_numbers_city(workspace, carrier, monkeypatch):
    ws = workspace
    seen = []

    async def search(region, city=None, limit=10):
        seen.append((region, city, limit))
        return [
            {
                "phone_number": SF,
                "cost_information": {"currency": "USD", "upfront_cost": "1.00", "monthly_cost": "1.00"},
                "region_information": [
                    {"region_type": "location", "region_name": "SAN FRANCISCO"},
                    {"region_type": "state", "region_name": "CA"},
                ],
            }
        ]

    monkeypatch.setattr(carrier, "search_numbers", search)
    connect(ws, maximum_owned_numbers=9, number_initial_limit_usd=9, number_monthly_limit_usd=9)
    quote = post(ws, "/v1/admin/numbers/search", {"region": "628", "city": "San Francisco"})["numbers"][0]
    assert quote["city"] == "San Francisco" and seen[0] == ("628", "San Francisco", 10)
    post(ws, "/v1/admin/numbers/purchase", {"quote_id": quote["id"], "approve_recurring": True})
    # The price recheck repeats the same search and looks further down its results.
    assert seen[-1] == ("628", "San Francisco", 250)


def test_every_setup_applies_the_daily_spend_cap(workspace, carrier):
    ws = workspace
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    assert carrier.spend_caps == [25]
    assert call(ws, "PUT", "/v1/admin/numbers/spend-cap", json={"daily_usd": 40})["daily_usd"] == 40
    assert carrier.spend_caps == [25, 40]
    post(ws, "/v1/admin/numbers/add", {"phone": NYC})
    assert carrier.spend_caps == [25, 40, 40]
    assert get(ws, "/v1/admin/settings")["telnyx_daily_spend_limit_usd"] == 40
    call(ws, "PUT", "/v1/admin/numbers/spend-cap", json={"daily_usd": 0}, status=422)


def test_pausing_stops_a_number_and_keeps_it_listed(workspace, carrier, clock):
    ws = workspace
    connect(ws)
    own_numbers(carrier)
    added = post(ws, "/v1/admin/numbers/add", {"phone": SF})
    clock.now += timedelta(hours=24)
    sign_in_again(ws)
    assert numbers(ws)[SF]["outbound_ready"]
    call(
        ws,
        "PATCH",
        f"/v1/admin/numbers/{added['id']}",
        json={"lifecycle": "paused_for_outbound", "reason": "Excluded by the owner"},
    )
    assert not numbers(ws)[SF]["outbound_ready"]
    assert {n["phone"] for n in post(ws, "/v1/admin/numbers/sync")["owned"] if n["added"]} == {SF}


def test_a_city_search_tries_the_overlays_and_keeps_only_that_city(workspace, carrier, monkeypatch):
    # Telnyx's nearby results ignore the city: 415 answers with Marin numbers in 628.
    def number(phone, place):
        return {
            "phone_number": phone,
            "cost_information": {"currency": "USD", "upfront_cost": "1.00", "monthly_cost": "1.00"},
            "region_information": [{"region_type": "location", "region_name": place}],
        }

    stock = {
        "415": [number("+16282028848", "BELVEDERE")],
        "628": [number("+16282564157", "SAN FRANCISCO"), number("+16282028854", "NOVATO")],
    }

    async def search(region, city=None, limit=10):
        return stock.get(region, [])

    monkeypatch.setattr(carrier, "search_numbers", search)
    found = post(workspace, "/v1/admin/numbers/search", {"region": "415", "city": "san francisco"})["numbers"]
    assert [(q["phone"], q["city"], q["region"]) for q in found] == [("+16282564157", "San Francisco", "628")]


def test_new_york_finds_the_city_telnyx_calls_new_york_city(workspace, carrier, monkeypatch):
    seen = set()

    async def search(region, city=None, limit=10):
        seen.add((region, city))
        return [
            {
                "phone_number": "+19295423176",
                "cost_information": {"currency": "USD", "upfront_cost": "1.00", "monthly_cost": "1.00"},
                "region_information": [{"region_type": "location", "region_name": "NEW YORK CITY"}],
            }
        ]

    monkeypatch.setattr(carrier, "search_numbers", search)
    found = post(workspace, "/v1/admin/numbers/search", {"region": "212", "city": "New York"})["numbers"]
    assert [q["phone"] for q in found] == ["+19295423176"]
    assert ("929", "New York City") in seen


def priced(phone, place):
    return {
        "phone_number": phone,
        "cost_information": {"currency": "USD", "upfront_cost": "1.00", "monthly_cost": "1.00"},
        "region_information": [{"region_type": "location", "region_name": place}],
    }


def rate_limited():
    request = httpx.Request("GET", "https://api.telnyx.com/v2/available_phone_numbers")
    return httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))


def test_a_rate_limited_area_code_does_not_sink_a_city_search(workspace, carrier, monkeypatch):
    async def search(region, city=None, limit=10):
        if region == "347":
            raise rate_limited()
        return [priced("+19295423176", "NEW YORK CITY")] if region == "929" else []

    monkeypatch.setattr(carrier, "search_numbers", search)
    found = post(workspace, "/v1/admin/numbers/search", {"region": "212", "city": "New York"})["numbers"]
    assert [q["phone"] for q in found] == ["+19295423176"]


def test_a_city_search_asks_telnyx_one_area_code_at_a_time_and_stops_when_it_has_enough(
    workspace, carrier, monkeypatch
):
    running, most, seen = [0], [0], []

    async def search(region, city=None, limit=10):
        running[0] += 1
        most[0] = max(most[0], running[0])
        await asyncio.sleep(0)
        seen.append(region)
        running[0] -= 1
        if region != "646":
            return []
        return [priced(f"+1646555{i:04d}", "NEW YORK CITY") for i in range(10)]

    monkeypatch.setattr(carrier, "search_numbers", search)
    found = post(workspace, "/v1/admin/numbers/search", {"region": "212", "city": "New York"})["numbers"]
    assert len(found) == 10 and most[0] == 1
    assert seen == ["212", "332", "646"]


def test_a_search_telnyx_refuses_everywhere_says_so(workspace, carrier, monkeypatch):
    async def search(region, city=None, limit=10):
        raise rate_limited()

    monkeypatch.setattr(carrier, "search_numbers", search)
    refused = post(workspace, "/v1/admin/numbers/search", {"region": "929"}, status=502)
    assert "Telnyx" in refused["detail"]
