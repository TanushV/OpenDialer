"""R9E.1 and R9E.2: the one calling decision, checked over every combination of its inputs.

Every blocker alone and in pairs, every claim, both steps, a good and a bad reservation and every queue answer.
"""

from itertools import combinations, product

import pytest
from app.calling.blockers import CATALOG, EXITS, blocked
from app.calling.decide import OUTS, Situation, decide, require
from fastapi import HTTPException

CODES = sorted(set(CATALOG) & set(EXITS))
BLOCKER_SETS = [(), *[(c,) for c in CODES], *combinations(CODES, 2)]


def situations():
    for codes, claim, step, reservation_ok, queue in product(
        BLOCKER_SETS,
        ("none", "reservation", "live_call"),
        ("reserve", "dial"),
        (False, True),
        ("unasked", "lead", "none", "scanning"),
    ):
        yield Situation(tuple(blocked(c) for c in codes), claim, step, reservation_ok, queue)


def test_every_situation_obeys_the_calling_laws():
    checked = 0
    for situation in situations():
        decision = decide(situation)
        checked += 1
        if situation.blockers:
            assert not decision.go and decision.code == situation.blockers[0].code, situation
        if situation.claim == "live_call":
            assert not decision.go, "a live call is never joined by a second dial"
        if situation.step == "reserve" and situation.claim == "reservation":
            assert not decision.go, "a lead already held is never held twice"
        if situation.step == "dial" and not situation.reservation_ok:
            assert not decision.go, "a dial needs the rep's own live reservation"
        if situation.step == "reserve" and situation.queue in {"none", "scanning"}:
            assert not decision.go
        if not decision.go:
            assert decision.clears_within or decision.control, f"{decision.code} has no way out"
        else:
            assert not situation.blockers and situation.claim != "live_call"
    assert checked > 3_000


def test_a_clear_situation_goes_ahead():
    assert decide(Situation(queue="lead")).go
    assert decide(Situation(step="dial", reservation_ok=True, claim="reservation")).go


def test_every_refusal_that_is_not_a_blocker_names_its_way_out():
    for code, decision in OUTS.items():
        assert decision.code == code and (decision.clears_within or decision.control)


def test_require_refuses_with_the_first_blocker_or_the_given_detail():
    with pytest.raises(HTTPException) as caught:
        require(Situation(blockers=(blocked("NO_CAMPAIGN"), blocked("PENDING_LOCAL_OPTOUTS"))))
    assert caught.value.detail["code"] == "NO_CAMPAIGN"
    with pytest.raises(HTTPException) as caught:
        require(Situation(queue="none"), {"code": "NO_ELIGIBLE_LEADS"})
    assert caught.value.detail == {"code": "NO_ELIGIBLE_LEADS"}
    require(Situation(queue="lead"))
