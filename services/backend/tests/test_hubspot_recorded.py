"""R9B.1: the HubSpot provider, sync and outcome map over answers recorded from the owner's account.

Recorded November 10, 2026 with the account's Service Key, read-only, touching only the "OpenDialer Test" contact (the
owner's own test line). The account has no lists yet, which the recording keeps.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from app.aftercall import crm_log
from app.leads import hubspot_sync as sync
from app.providers.hubspot import HubSpot

RECORDED = json.loads((Path(__file__).parent / "fixtures/hubspot/recorded-2026-11-10.json").read_text())
TEST_CONTACT = "261425233976"


def replay(*, refuse=False):
    """Answer each request with the recorded answer for its method and path."""

    def respond(request):
        for entry in RECORDED:
            if (entry["method"], entry["path"]) == (request.method, request.url.path):
                if refuse != (entry["status"] == 401):
                    continue
                return httpx.Response(entry["status"], json=entry["response"])
        return httpx.Response(404, json={"message": "not recorded"})

    return HubSpot("recorded", http=httpx.AsyncClient(transport=httpx.MockTransport(respond)))


async def test_the_key_check_passes_on_the_recorded_account_and_names_scopes_when_refused():
    assert await replay().check() == (True, "Connected")
    ok, detail = await replay(refuse=True).check()
    assert not ok and "crm.lists.read" in detail and "crm.objects.contacts.read" in detail


async def test_the_account_has_no_lists_and_the_test_contact_maps_to_one_lead():
    hubspot = replay()
    assert await hubspot.lists() == []
    contacts = await hubspot.contacts([TEST_CONTACT], sync.PROPERTIES)
    assert [c["id"] for c in contacts] == [TEST_CONTACT]
    row = sync.row(contacts[0])
    assert (row["name"], row["phone"], row["company"]) == (
        "OpenDialer Test",
        "+12038211101",
        "OpenDialer test",
    )
    planned = sync.plan(contacts, [TEST_CONTACT], {}, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert [cid for cid, _ in planned["add"]] == [TEST_CONTACT]
    assert planned["opt_outs"] == [], "opendialer_opt_out is the string 'false', which is not an opt-out"


async def test_every_app_outcome_maps_to_the_accounts_dispositions():
    dispositions = await replay().dispositions()
    assert len(dispositions) == 7
    outcomes = [
        "no_answer",
        "voicemail_skipped",
        "busy",
        "voicemail",
        "meeting_booked",
        "callback_requested",
        "not_interested",
        "gatekeeper",
        "wrong_party",
        "invalid_endpoint",
    ]
    mapped, missing = crm_log.map_outcomes(outcomes, dispositions)
    assert missing == [] and set(mapped) == set(outcomes)
