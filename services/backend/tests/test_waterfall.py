"""R9B.14: the agreed waterfall rules, ported from the outside runner's 20 offline tests, against a fake Treg."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.leads import waterfall as w

LEAD, AIARK, TOMBA, WIZA = w.FINDERS + [w.WIZA]
A, B, C, D = "+12125550101", "+12125550102", "+12125550103", "+12125550104"
NOW = datetime(2026, 11, 8, 17, tzinfo=UTC)
CONTACT = {
    "name": "Pat Example",
    "company": "Acme",
    "domain": "acme.test",
    "linkedin_url": "https://www.linkedin.com/in/pat-example",
}


def ok(result, charged=0):
    return {"result": result, "_treg": {"http_status": 200, "call_id": "c", "charged_micro": charged}}


def lead_hit(*numbers):
    return ok({"data": {"found": True, "phones": [{"phone": n, "type": "mobile"} for n in numbers]}}, 5_000)


def lead_miss():
    return ok({"data": {"found": False}})


def aiark_hit(*numbers):
    return ok({"data": {"data": [list(numbers)]}}, 26_335)


def empty():
    return ok({"data": None})


def trestle(name_match=True, grade="A", valid=True):
    return ok({"phone": {"name_match": name_match, "contact_grade": grade, "is_valid": valid}}, 30_000)


def wiza(*numbers):
    found = {
        "status": "finished",
        "is_complete": True,
        "phones": [{"number": n, "type": "mobile"} for n in numbers],
    }
    return ok({"data": found}, 125_000 if numbers else 0)


class Fake:
    """Answers from a table. An endpoint or number it was not told about fails the test."""

    def __init__(self, finders=None, numbers=None, formats=None):
        self.finders, self.numbers, self.formats = finders or {}, numbers or {}, formats or {}
        self.log, self.keys = [], []

    async def call(self, endpoint, method, params, ceiling, key):
        self.log.append(endpoint)
        self.keys.append(key)
        if endpoint == w.FORMAT:
            number = "+" + params["telephone_number"]
            kind, country = self.formats.get(number, ("MOBILE", "USA"))
            return ok(
                {
                    "results": [
                        {
                            "error": "NONE",
                            "telephone_number_type": kind,
                            "original_network_details": {"country_iso3": country},
                        }
                    ]
                }
            )
        if endpoint == w.TRESTLE:
            return self.numbers[params["phone"]]
        return self.finders[endpoint]


def run(fake, contact=CONTACT, ledger=None, room=1_000_000):
    ledger = {} if ledger is None else ledger
    result = asyncio.run(
        w.Waterfall(ledger, fake, now=NOW, room_micro=room, save=lambda: None).run(dict(contact))
    )
    return result, ledger


def test_trestle_yes_stops_and_never_asks_anyone_else():
    fake = Fake({LEAD: lead_hit(A)}, {A: trestle()})
    result, _ = run(fake)
    assert (result["status"], result["phone"], result["finder"]) == ("confirmed", A, "leadocean")
    assert fake.log == [LEAD, w.FORMAT, w.TRESTLE]


def test_no_paid_live_line_check_on_any_path():
    assert not any("hlrlookup.people.phone.verify" == e for e in w.QUOTES)


def test_trestle_blank_uses_the_number_as_unconfirmed_and_stops():
    fake = Fake({LEAD: lead_hit(A, B)}, {A: trestle(name_match=None)})
    result, _ = run(fake)
    assert (result["status"], result["phone"]) == ("unconfirmed", A)
    assert fake.log.count(w.TRESTLE) == 1


def test_no_tries_the_providers_second_number_and_never_a_third():
    fake = Fake(
        {LEAD: lead_hit(A, B, C), WIZA: wiza(D)}, {A: trestle(False), B: trestle(False), D: trestle()}
    )
    result, _ = run(fake)
    checked = [x["number"] for x in result["checks"]]
    assert checked[:2] == [A, B] and C not in checked


def test_no_moves_to_the_next_provider_inside_the_stop():
    fake = Fake({LEAD: lead_hit(A), AIARK: aiark_hit(D)}, {A: trestle(False), D: trestle()})
    result, _ = run(fake)
    assert (result["status"], result["phone"], result["finder"]) == ("confirmed", D, "aiark")


def test_grade_f_is_rejected_even_when_the_name_matches():
    fake = Fake({LEAD: lead_hit(A, B)}, {A: trestle(True, "F"), B: trestle()})
    result, _ = run(fake)
    assert result["phone"] == B and result["checks"][0]["verdict"] == "bad_grade"


def test_invalid_number_is_rejected_even_when_the_name_matches():
    fake = Fake({LEAD: lead_hit(A, B)}, {A: trestle(True, "A", False), B: trestle()})
    assert run(fake)[0]["phone"] == B


def test_non_us_and_malformed_numbers_never_reach_trestle():
    fake = Fake(
        {LEAD: lead_hit("+442079460000", A, B), AIARK: empty(), TOMBA: empty(), WIZA: wiza()},
        {B: trestle()},
        formats={A: ("INVALID", "USA")},
    )
    result, _ = run(fake)
    assert [x["verdict"] for x in result["checks"]][:2] == ["not_us_format", "bad_format"]
    assert result["phone"] == B and fake.log.count(w.TRESTLE) == 1


def test_a_plus_one_number_outside_the_us_never_reaches_trestle():
    fake = Fake({LEAD: lead_hit(A, B)}, {B: trestle()}, formats={A: ("MOBILE", "CAN")})
    result, _ = run(fake)
    assert result["checks"][0]["verdict"] == "not_us_format" and result["phone"] == B


def test_a_saved_crm_number_skips_treg_even_with_no_spend_room():
    fake = Fake()
    result, _ = run(fake, {**CONTACT, "existing_phone": A}, room=0)
    assert (result["status"], result["phone"], result["finder"]) == ("unconfirmed", A, "crm")
    assert fake.log == []


@pytest.mark.parametrize("phone", [A, "not a number", "+442079460000"])
def test_an_unusable_crm_number_falls_through_to_the_finders(phone):
    fake = Fake({LEAD: lead_hit(B)}, {B: trestle()})
    result, _ = run(fake, {**CONTACT, "existing_phone": phone}, ledger={"wrong_numbers": [A]})
    assert (result["phone"], result["finder"]) == (B, "leadocean")
    assert fake.log == [LEAD, w.FORMAT, w.TRESTLE]


def test_ten_cent_stop_skips_the_next_finder_but_wiza_still_runs():
    # LeadOcean and two Trestle checks spend 6.5 cents. AI Ark and a check would reach 12.1, past the stop.
    fake = Fake({LEAD: lead_hit(A, B), WIZA: wiza(D)}, {A: trestle(False), B: trestle(False), D: trestle()})
    result, _ = run(fake)
    assert AIARK not in fake.log and TOMBA not in fake.log
    assert (result["status"], result["finder"]) == ("confirmed", "wiza")


def test_wiza_never_runs_when_an_earlier_number_was_accepted():
    fake = Fake({LEAD: lead_miss(), AIARK: aiark_hit(A)}, {A: trestle(name_match=None)})
    run(fake)
    assert WIZA not in fake.log


def test_wiza_hit_is_billed_and_checked_by_trestle():
    fake = Fake({LEAD: lead_miss(), AIARK: empty(), TOMBA: empty(), WIZA: wiza(A)}, {A: trestle()})
    result, _ = run(fake)
    assert (result["status"], result["phone"], result["finder"]) == ("confirmed", A, "wiza")
    assert result["spent_micro"] == 155_000


def test_trestle_no_on_every_number_is_a_conflict_dialable_only_by_hand():
    fake = Fake({LEAD: lead_miss(), AIARK: empty(), TOMBA: empty(), WIZA: wiza(A)}, {A: trestle(False)})
    result, _ = run(fake)
    assert (result["status"], result["phone"]) == ("conflict", A)


def test_only_rejected_numbers_end_as_no_valid_number():
    fake = Fake({LEAD: lead_miss(), AIARK: empty(), TOMBA: empty(), WIZA: wiza(A)}, {A: trestle(True, "F")})
    result, _ = run(fake)
    assert result["status"] == "no_valid_number" and result["phone"] is None


def test_wiza_miss_is_free_and_ends_as_no_valid_number():
    fake = Fake({LEAD: lead_miss(), AIARK: empty(), TOMBA: empty(), WIZA: wiza()})
    result, _ = run(fake)
    assert result["status"] == "no_valid_number" and result["spent_micro"] == 0


def test_a_provider_failure_is_not_reported_as_a_miss():
    down = {
        "result": {"error": "fair use"},
        "_treg": {"http_status": 402, "call_id": "c", "charged_micro": 0},
    }
    fake = Fake({LEAD: down, AIARK: empty(), TOMBA: empty(), WIZA: wiza()})
    assert run(fake)[0]["status"] == "provider_error"


def test_a_wrong_number_mark_stops_that_number_and_reuses_paid_answers():
    fake = Fake({LEAD: lead_hit(A, B)}, {A: trestle(), B: trestle()})
    first, ledger = run(fake)
    assert first["phone"] == A
    w.mark_wrong(ledger, A)
    paid_before = fake.log.count(LEAD)
    second, _ = run(fake, ledger=ledger)
    assert second["phone"] == B
    assert fake.log.count(LEAD) == paid_before, "The saved finder answer is reused, not bought again"


def test_a_repeat_sync_pays_nothing():
    fake = Fake({LEAD: lead_hit(A)}, {A: trestle()})
    _, ledger = run(fake)
    before = len(fake.log)
    again, _ = run(fake, ledger=ledger)
    assert again["status"] == "confirmed" and len(fake.log) == before


def test_the_list_limit_stops_the_job_and_the_contact_can_resume():
    fake = Fake({LEAD: lead_hit(A)}, {A: trestle()})
    ledger = {}
    with pytest.raises(w.SpendLimit):
        run(fake, ledger=ledger, room=4_000)
    assert fake.log == []
    assert run(fake, ledger=ledger)[0]["status"] == "confirmed"


def test_an_unsettled_request_replays_its_own_key_and_a_stale_one_is_held():
    class Flaky(Fake):
        async def call(self, endpoint, method, params, ceiling, key):
            if endpoint == LEAD and not getattr(self, "failed", False):
                self.failed = True
                self.keys.append(key)
                raise ConnectionError("lost")
            return await super().call(endpoint, method, params, ceiling, key)

    fake = Flaky({LEAD: lead_hit(A)}, {A: trestle()})
    ledger = {}
    with pytest.raises(ConnectionError):
        run(fake, ledger=ledger)
    assert ledger["calls"][0]["state"] == "reserved"
    assert run(fake, ledger=ledger)[0]["status"] == "confirmed"
    assert fake.keys[0] == fake.keys[1], "the retry reuses the original idempotency key"
    stale = {
        "calls": [
            {**ledger["calls"][0], "state": "reserved", "created_at": (NOW - timedelta(days=1)).isoformat()}
        ]
    }
    with pytest.raises(w.Held):
        run(Fake({LEAD: lead_hit(A)}, {A: trestle()}), ledger=stale)


def test_a_charge_above_the_quote_disables_that_finder():
    pricey = ok({"data": {"found": True, "phones": [{"phone": A}]}}, 50_000)
    _, ledger = run(Fake({LEAD: pricey}, {A: trestle()}))
    assert LEAD in ledger["disabled"]


def test_an_explicitly_different_profile_is_rejected():
    other = ok(
        {
            "data": {"found": True, "phones": [{"phone": A}]},
            "linkedin_url": "https://linkedin.com/in/someone-else",
        },
        5_000,
    )
    fake = Fake({LEAD: other, AIARK: empty(), TOMBA: empty(), WIZA: wiza()})
    result, _ = run(fake)
    assert result["notes"][0].get("identity_conflict") and w.TRESTLE not in fake.log
