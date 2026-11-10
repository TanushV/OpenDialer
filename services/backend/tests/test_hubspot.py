"""R9B.2 to R9B.4 over HubSpot answers shaped from its public API docs (R9B.1 replaces them with recordings).

Paging, one contact once, syncing again changing nothing, every list member synced whoever owns it, opt-outs that only add,
removed members kept with history, the last-contacted guard, the outcome map, retries that never duplicate a record,
and the transcript never leaving the Mac.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.aftercall import crm_log
from app.leads import hubspot_sync as sync
from app.providers.hubspot import HubSpot, HubSpotError

NOW = datetime(2026, 11, 8, 17, tzinfo=UTC)


def contact(cid, owner="11", **props):
    base = {
        "firstname": "Pat",
        "lastname": f"Example{cid}",
        "company": "Acme",
        "phone": f"+1617555{int(cid):04d}",
        "hubspot_owner_id": owner,
    }
    return {"id": str(cid), "properties": {**base, **props}}


async def nosleep(_):
    return None


def hubspot(respond):
    return HubSpot("key", http=httpx.AsyncClient(transport=httpx.MockTransport(respond)), sleep=nosleep)


async def test_list_members_page_and_appear_once():
    def respond(request):
        after = request.url.params.get("after")
        page = (
            {"results": [{"recordId": "1"}, {"recordId": "2"}], "paging": {"next": {"after": "p2"}}}
            if not after
            else {"results": [{"recordId": "2"}, {"recordId": "3"}]}
        )
        return httpx.Response(200, json=page)

    assert await hubspot(respond).members("77") == ["1", "2", "3"]


async def test_contacts_are_read_in_batches_of_100():
    sizes = []

    def respond(request):
        inputs = json.loads(request.content)["inputs"]
        sizes.append(len(inputs))
        return httpx.Response(200, json={"results": [contact(i["id"]) for i in inputs]})

    found = await hubspot(respond).contacts([str(i) for i in range(1, 251)], sync.PROPERTIES)
    assert sizes == [100, 100, 50] and len(found) == 250


async def test_rate_limits_back_off_then_succeed_and_errors_are_named():
    answers = iter(
        [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"results": []})]
    )
    assert await hubspot(lambda r: next(answers)).lists() == []
    with pytest.raises(HubSpotError) as caught:
        await hubspot(lambda r: httpx.Response(403, json={})).lists()
    assert caught.value.status == 403


def test_every_list_member_syncs_whoever_owns_it_in_hubspot():
    """One user (owner, November 10): the list picks who is called; HubSpot's contact owner does not filter."""
    contacts = [contact(1, "11"), contact(2, "22"), contact(3, "")]
    planned = sync.plan(contacts, ["1", "2", "3"], {}, now=NOW)
    assert sorted(cid for cid, _ in planned["add"]) == ["1", "2", "3"]


def test_syncing_again_changes_nothing_and_an_edit_updates_the_row():
    contacts = [contact(1)]
    first = sync.plan(contacts, ["1"], {}, now=NOW)
    existing = dict(first["add"])
    again = sync.plan(contacts, ["1"], existing, now=NOW)
    assert again["add"] == again["update"] == again["drop"] == []
    edited = sync.plan([contact(1, jobtitle="VP Sales")], ["1"], existing, now=NOW)
    assert edited["update"][0][1]["title"] == "VP Sales"


def test_a_removed_member_is_dropped_and_a_reassigned_owner_changes_nothing():
    existing = {"1": sync.row(contact(1)), "2": sync.row(contact(2))}
    moved = sync.plan([contact(1, "22"), contact(2)], ["1"], existing, now=NOW)
    assert moved["drop"] == ["2"]


def test_an_opt_out_in_hubspot_is_suppressed():
    contacts = [contact(1, opendialer_opt_out="true", opendialer_opt_out_reason="do_not_call")]
    planned = sync.plan(contacts, ["1"], {}, now=NOW)
    assert planned["opt_outs"] == [{"contact_id": "1", "phone": "+16175550001", "reason": "do_not_call"}]


def test_a_contact_another_dialer_reached_recently_waits_but_our_own_call_does_not():
    reached = (NOW - timedelta(hours=5)).isoformat()
    planned = sync.plan([contact(1, notes_last_contacted=reached)], ["1"], {}, now=NOW)
    assert planned["recent"]["1"] == (NOW - timedelta(hours=5) + sync.SPACING).isoformat()
    ours = sync.plan(
        [contact(1, notes_last_contacted=reached)],
        ["1"],
        {},
        now=NOW,
        last_called={"1": NOW - timedelta(hours=5)},
    )
    assert ours["recent"] == {}


def test_every_outcome_maps_or_setup_says_which_does_not():
    dispositions = [
        {"id": "d-na", "label": "No answer"},
        {"id": "d-b", "label": "Busy"},
        {"id": "d-c", "label": "Connected"},
        {"id": "d-mb", "label": "Meeting booked"},
    ]
    mapped, missing = crm_log.map_outcomes(
        [
            "no_answer",
            "voicemail_skipped",
            "busy",
            "meeting_booked",
            "not_interested",
            "wrong_party",
            "voicemail",
        ],
        dispositions,
    )
    assert (
        mapped["voicemail_skipped"] == "d-na"
        and mapped["meeting_booked"] == "d-mb"
        and mapped["not_interested"] == "d-c"
    )
    assert sorted(missing) == ["voicemail", "wrong_party"]


async def test_a_retry_after_an_uncertain_write_leaves_one_record_and_never_sends_a_transcript():
    created, searched = [], []

    def respond(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/search"):
            searched.append(body)
            return httpx.Response(200, json={"results": [{"id": "hs-1"}] if created else []})
        created.append(body)
        return httpx.Response(201, json={"id": "hs-1"})

    record = crm_log.CallRecord(
        "call-1",
        "501",
        "meeting_booked",
        "2026-11-08T17:00:00Z",
        83_000,
        "+14155550100",
        "+16175550100",
        "Booked for Tuesday.",
    )
    hs = hubspot(respond)
    assert await crm_log.log_call(hs, record, {"meeting_booked": "d-mb"}) == "hs-1"
    assert await crm_log.log_call(hs, record, {"meeting_booked": "d-mb"}, uncertain=True) == "hs-1"
    assert len(created) == 1 and searched
    props = created[0]["properties"]
    assert (props["opendialer_call_id"], props["hs_call_disposition"]) == ("call-1", "d-mb")
    assert "hubspot_owner_id" not in props
    assert created[0]["associations"][0]["to"]["id"] == "501"
    assert "transcript" not in json.dumps(created[0]).lower()


async def test_setup_creates_its_properties_once():
    made = []

    def respond(request):
        if request.method == "GET":
            return (
                httpx.Response(200, json={"name": "x"})
                if "phone_status" in request.url.path
                else httpx.Response(404)
            )
        made.append(json.loads(request.content)["name"])
        return httpx.Response(201, json={})

    created = await hubspot(respond).ensure_properties()
    assert "phone_status" not in made and "opendialer_call_id" in made and len(created) == len(made)


async def test_logging_a_call_already_in_hubspot_finds_it_instead_of_failing():
    """Live, November 9: HubSpot refuses a second record with the same opendialer_call_id (HTTP 400)."""
    created = []

    def respond(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [{"id": "hs-1"}] if created else []})
        if created:
            return httpx.Response(400, json={"category": "VALIDATION_ERROR", "message": "duplicate value"})
        created.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "hs-1"})

    record = crm_log.CallRecord(
        "call-2", "501", "no_answer", "2026-11-09T11:55:00Z", 12_000, "+14155550100", "+16175550100"
    )
    hs = hubspot(respond)
    assert await crm_log.log_call(hs, record, {"no_answer": "d-na"}) == "hs-1"
    assert await crm_log.log_call(hs, record, {"no_answer": "d-na"}) == "hs-1"
    assert len(created) == 1
