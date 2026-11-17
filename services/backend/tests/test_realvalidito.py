"""R9B.15 and R9B.17: the do-not-call check on CRM leads over recorded-shape RealValidito answers."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from app.leads import dnc
from app.leads.record import number_hold
from app.providers.realvalidito import BATCH, DNCUnavailable, RealValidito

FIXTURE = json.loads((Path(__file__).parent / "fixtures/realvalidito/validate.json").read_text())
NOW = datetime(2026, 11, 8, 17, tzinfo=UTC)
NUMBERS = [f"+1617555010{i}" for i in range(5)]


def client(respond):
    return RealValidito("key", "secret", http=httpx.AsyncClient(transport=httpx.MockTransport(respond)))


async def test_each_list_suppresses_with_its_reason_and_invalid_marks_bad():
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=FIXTURE)

    answers = await client(respond).validate(NUMBERS)
    assert sent[0]["numbers"] == [n[2:] for n in NUMBERS]
    assert (sent[0]["api_key"], sent[0]["api_secret"]) == ("key", "secret")
    expected = [[], ["federal"], ["state:FL"], ["tcpa_litigator"]]
    for number, lists in zip(NUMBERS, expected, strict=False):
        changes, suppression = dnc.outcome({"phone": number}, answers[number], NOW)
        assert changes["dnc"]["lists"] == lists
        assert (suppression is None) == (not lists)
        if suppression:
            assert suppression["reason"] == "do_not_call" and suppression["lists"] == lists
    changes, suppression = dnc.outcome({"phone": NUMBERS[4]}, answers[NUMBERS[4]], NOW)
    assert changes["number_status"] == "bad" and suppression is None
    assert (
        dnc.describe(["federal", "state:FL"]) == "the federal do-not-call list, the FL state do-not-call list"
    )


async def test_batches_are_at_most_1000_numbers():
    sizes = []

    def respond(request):
        numbers = json.loads(request.content)["numbers"]
        sizes.append(len(numbers))
        return httpx.Response(200, json={"data": [{**FIXTURE["data"][0], "number": n} for n in numbers]})

    numbers = [f"+1212{i:07d}" for i in range(BATCH + 5)]
    assert len(await client(respond).validate(numbers)) == BATCH + 5
    assert sizes == [BATCH, 5]


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (200, {"code": 603, "message": "no credits"}, "DNC_NO_CREDITS"),
        (604, {}, "DNC_NO_CREDITS"),
        (200, {"code": "600"}, "DNC_KEY_INVALID"),
        (200, {"status": "failed", "error": {"error_code": 604}}, "DNC_NO_CREDITS"),
        (502, {}, "DNC_UNAVAILABLE"),
        (200, {"data": [{"number": "6175550100", "federal_dnc": True}]}, "DNC_UNAVAILABLE"),
    ],
)
async def test_no_credits_a_bad_key_or_an_outage_never_clears_a_number(status, body, reason):
    with pytest.raises(DNCUnavailable) as caught:
        await client(lambda request: httpx.Response(status, json=body)).validate(NUMBERS[:1])
    assert caught.value.reason == reason
    lead = {"source": "crm", "phone": NUMBERS[0], "number_status": "confirmed"}
    lead |= dnc.failed(lead, caught.value.reason, NOW)
    assert lead["dnc"]["status"] == "not_checked"
    assert number_hold(lead) == "DNC_NOT_CHECKED"


def test_results_expire_within_30_days_and_csv_leads_are_never_due():
    checked = {"status": "checked", "phone": NUMBERS[0], "checked_at": (NOW - timedelta(days=28)).isoformat()}
    lead = {"source": "crm", "phone": NUMBERS[0], "dnc": checked}
    assert not dnc.due(lead, NOW)
    assert dnc.due(lead, NOW + timedelta(days=1))
    assert dnc.due({**lead, "phone": NUMBERS[1]}, NOW), "a changed number is checked again"
    assert not dnc.due({"source": "csv", "phone": NUMBERS[0]}, NOW)
    assert number_hold(lead) is None


@pytest.mark.parametrize("body", [{"credits": 100}, {"credits": 100, "data": {}}, 100])
async def test_legacy_credits_are_read_for_the_settings_check(body):
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=body)

    assert await client(respond).credits() == 100
    assert seen == ["/dnclookup/getcredits/key/secret"]


@pytest.mark.parametrize("balance", ["100", "0", 19])
async def test_settings_reads_the_published_credits_envelope(balance):
    provider = client(
        lambda request: httpx.Response(
            200, json={"status": "success", "data": {"available_credits": balance}}
        )
    )
    assert await provider.credits() == int(balance)


@pytest.mark.parametrize(
    ("body", "reason", "detail"),
    [
        ({"status": "failed", "error": {"error_code": 600}}, "DNC_KEY_INVALID", "refused"),
        (
            {"Response": {"Status": "Failed", "Errors": {"ErrorCode": "604"}}},
            "DNC_NO_CREDITS",
            "No credits left",
        ),
        ({"status": "failed", "error": {"error_code": "616"}}, "DNC_UNAVAILABLE", "Allowed IP addresses"),
        ({"status": "failed", "data": {"available_credits": "10"}}, "DNC_UNAVAILABLE", "rejected"),
        ({"status": "success", "data": {"available_credits": True}}, "DNC_UNAVAILABLE", "credit balance"),
        ({"status": "success", "data": {"available_credits": 4.5}}, "DNC_UNAVAILABLE", "credit balance"),
        ({"credits": -1}, "DNC_UNAVAILABLE", "credit balance"),
    ],
)
async def test_settings_names_rejections_and_never_accepts_an_invalid_balance(body, reason, detail):
    with pytest.raises(DNCUnavailable, match=detail) as caught:
        await client(lambda request: httpx.Response(200, json=body)).credits()
    assert caught.value.reason == reason


async def test_example_credentials_are_rejected_without_a_request():
    def unexpected(request):
        pytest.fail("Example credentials reached the provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
        with pytest.raises(DNCUnavailable, match="example placeholders"):
            await RealValidito("#api-key", "#api-secret", http=http).credits()
