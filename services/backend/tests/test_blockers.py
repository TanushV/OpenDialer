"""R4.3 and G4: one gate answers "can I dial?", with a code, a message and a fix for each blocker."""

import re
from pathlib import Path

import pytest
from app import ops
from app.calling.blockers import CATALOG, Facts, blocked, blockers, refuse
from fastapi import HTTPException

APP = Path(__file__).resolve().parents[1] / "app"
FIXES = {"settings", "numbers", "leads", "dialer", "attention"}


def codes(**facts):
    return [b.code for b in blockers(Facts(**facts))]


def test_a_ready_workspace_has_no_blockers():
    assert codes() == []
    assert codes(rep_audio_ready=True) == []


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        ({"integrity_ok": False}, "DATABASE_INTEGRITY_FAILURE"),
        ({"restore_hold": True}, "RESTORE_RECONCILIATION_REQUIRED"),
        ({"telnyx_configured": False}, "TELNYX_NOT_CONFIGURED"),
        ({"signature_key": False}, "WEBHOOK_SIGNATURE_KEY_MISSING"),
        ({"audio_setup": False}, "AUDIO_SETUP_MISSING"),
        ({"connection": "CONNECTION_RECONNECTING"}, "CONNECTION_RECONNECTING"),
        ({"campaign": False}, "NO_CAMPAIGN"),
        ({"number": "NO_CALLER_NUMBER"}, "NO_CALLER_NUMBER"),
        ({"pending_optouts": 2}, "PENDING_LOCAL_OPTOUTS"),
        ({"rep_audio_ready": False}, "REP_AUDIO_NOT_READY"),
    ],
)
def test_each_fact_names_its_own_blocker(facts, code):
    assert codes(**facts) == [code]


def test_the_most_basic_blocker_comes_first():
    assert codes(rep_audio_ready=False, telnyx_configured=False, campaign=False) == [
        "TELNYX_NOT_CONFIGURED",
        "NO_CAMPAIGN",
        "REP_AUDIO_NOT_READY",
    ]


def test_the_replay_carrier_needs_no_carrier_setup_but_still_waits_for_the_relay():
    assert codes(carrier_setup_required=False, telnyx_configured=False, audio_setup=False) == []
    assert codes(carrier_setup_required=False, connection="CONNECTION_SETUP_CHANGING") == [
        "CONNECTION_SETUP_CHANGING"
    ]


def test_every_blocker_has_a_message_and_a_known_fix():
    for code, (message, fix) in CATALOG.items():
        assert message.endswith(".") and fix in FIXES, code
    assert "+14155550100" in blocked("CALLER_NUMBER_CALLBACK_TEST_REQUIRED", "+14155550100").message


def test_refuse_sends_the_first_blocker_and_nothing_when_clear():
    refuse([])
    with pytest.raises(HTTPException) as refused:
        refuse(blockers(Facts(campaign=False, rep_audio_ready=False)))
    assert refused.value.status_code == 409
    assert refused.value.detail == {
        "code": "NO_CAMPAIGN",
        "message": CATALOG["NO_CAMPAIGN"][0],
        "fix": "leads",
    }


# Where a readiness code may be named: the gate, the facts it reads, and two lower layers that guard their own calls.
ALLOWED = {
    "calling/blockers.py",
    "managed_webhooks.py",  # connection facts
    "administration.py",  # number facts and preflight's report
    "providers/carrier.py",  # cannot build a carrier without a key
    "compliance/policy.py",  # per-call authorization after a restore
    "calling/audio_leg.py",  # the audio leg's own setup check when the rep connects
}


def test_no_other_code_path_names_a_readiness_blocker():
    pattern = re.compile("|".join(f'"{code}"' for code in CATALOG))
    found = sorted(
        f"{path.relative_to(APP).as_posix()}:{number}"
        for path in APP.rglob("*.py")
        if "__pycache__" not in path.parts and path.relative_to(APP).as_posix() not in ALLOWED
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    )
    assert found == []


def test_the_blockers_route_lists_what_stops_the_rep_now(workspace):
    from conftest import ready

    client, headers = workspace["client"], workspace["headers"]
    before = client.get("/v1/blockers", headers=headers).json()["blockers"]
    assert [b["code"] for b in before] == ["REP_AUDIO_NOT_READY"]
    assert before[0]["fix"] == "dialer"
    ready(workspace)
    assert client.get("/v1/blockers", headers=headers).json() == {"blockers": []}


def test_a_test_session_dials_only_the_owners_test_line(workspace, app):
    """R6.6: in a test session, code refuses every number but the test line."""
    from app.calling.blockers import test_line_refusal
    from conftest import reserve

    assert test_line_refusal("+16175550100", test_session=False, test_lines={"+12038211101"}) == []
    assert test_line_refusal("+12038211101", test_session=True, test_lines={"+12038211101"}) == []
    client, headers = workspace["client"], workspace["headers"]
    # A test session needs a test phone; without one it cannot be turned on.
    assert client.put("/v1/admin/settings", headers=headers, json={"test_session": True}).status_code == 409
    body = {"test_session": True, "test_phone": "+12038211101"}
    assert client.put("/v1/admin/settings", headers=headers, json=body).status_code == 200
    # The queue offers nothing outside the Test line list, so no prospect is reserved.
    assert reserve(workspace).status_code == 409
    assert ops.carrier(app.state.service, workspace["tenant_id"]).dials == []


def test_a_test_call_dials_only_the_typed_test_phone_and_counts_as_a_test(workspace, app):
    """R7.5: Test call adds the test phone as a lead and the queue offers only it."""
    client, headers = workspace["client"], workspace["headers"]
    assert client.post("/v1/settings/test-call", headers=headers).status_code == 409
    client.put("/v1/admin/settings", headers=headers, json={"test_phone": "+12025550199"})
    started = client.post("/v1/settings/test-call", headers=headers).json()
    assert started["phone"] == "+12025550199"
    from conftest import ready

    ready(workspace)
    packet = client.post(
        "/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]}
    ).json()
    assert packet["lead"]["id"] == started["lead_id"]
    call = client.post(
        "/v1/calls/start",
        headers=headers,
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "test-call"},
    ).json()
    assert call["is_test"] is True
    assert client.delete("/v1/settings/test-call", headers=headers).json() == {"test_session": False}


def test_every_blocker_has_a_way_out_and_claims_to_clear_itself_only_when_it_does():
    """G10: no blocker is a dead end. One that says it clears by itself has a promised time."""
    from app.calling.blockers import CATALOG, EXITS

    assert set(EXITS) == set(CATALOG)
    for code, way_out in EXITS.items():
        assert way_out.by_itself_within or way_out.control, code
        assert way_out.by_itself_within is None or way_out.by_itself_within <= 120, code
        if "by itself" in CATALOG[code][0]:
            assert way_out.by_itself_within, f"{code} says it clears by itself but nothing clears it"


def test_every_combination_of_facts_names_blockers_that_each_have_a_way_out():
    """R9E.2: all combinations, not samples. Anything wrong yields a blocker; each has an exit; all fine yields none."""
    from itertools import product

    from app.calling.blockers import EXITS, Facts, blockers

    flags = {
        "integrity_ok": [True, False],
        "restore_hold": [False, True],
        "telnyx_configured": [True, False],
        "signature_key": [True, False],
        "audio_setup": [True, False],
        "connection": [
            None,
            "CONNECTION_RECONNECTING",
            "CONNECTION_DELIVERY_GAP",
            "CONNECTION_SIGN_IN_REQUIRED",
        ],
        "campaign": [True, False],
        "number": [None, "NO_CALLER_NUMBER", "CALLER_NUMBER_PAUSED"],
        "pending_optouts": [0, 2],
        "rep_audio_ready": [None, True, False],
        "channel_limited": [False, True],
    }
    checked = 0
    for values in product(*flags.values()):
        facts = Facts(**dict(zip(flags, values, strict=True)))
        found = blockers(facts)
        bad = (
            not facts.integrity_ok
            or facts.restore_hold
            or not facts.telnyx_configured
            or not facts.signature_key
            or not facts.audio_setup
            or facts.connection
            or not facts.campaign
            or facts.number
            or facts.pending_optouts
            or facts.rep_audio_ready is False
            or facts.channel_limited
        )
        assert bool(found) == bool(bad), facts
        assert len({b.code for b in found}) == len(found), "a blocker is listed twice"
        for b in found:
            assert EXITS[b.code].by_itself_within or EXITS[b.code].control
            assert b.message and b.fix
        checked += 1
    assert checked > 9_000


def test_a_test_call_to_the_owners_line_goes_ahead_at_night(workspace, app, clock):
    """Live check, November 10: at 11 PM Eastern the test call was refused as outside calling hours. Calling
    hours protect prospects; the owner's own test line in a test session ignores them (an opt-out still stops it)."""
    from datetime import UTC, datetime

    from conftest import ready

    client = workspace["client"]
    clock.now = datetime(2026, 11, 10, 4, 40, tzinfo=UTC)  # 11:40 PM in New York, 8:40 PM in California.
    token = client.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"}).json()
    headers = workspace["headers"] = {"Authorization": f"Bearer {token['access_token']}"}
    client.put("/v1/admin/settings", headers=headers, json={"test_phone": "+12038211101"})
    started = client.post("/v1/settings/test-call", headers=headers).json()
    ready(workspace)
    answer = client.post("/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["lead"]["id"] == started["lead_id"]


def test_a_test_call_reaches_an_old_test_lead_that_still_carries_a_delivery_hold(workspace, app):
    """Live check, November 10: the owner's test phone was an older lead held as `delivery_test`, so the queue never
    offered it and the Dialer said no one was left. Starting a test session clears holds on that one lead."""
    from app.platform.db import MODELS, update
    from conftest import ready
    from sqlalchemy import select

    client, headers = workspace["client"], workspace["headers"]
    client.put("/v1/admin/settings", headers=headers, json={"test_phone": "+12038211101"})
    first = client.post("/v1/settings/test-call", headers=headers).json()
    with app.state.db.transaction() as s:
        lead = s.scalars(select(MODELS["leads"]).where(MODELS["leads"].id == first["lead_id"])).one()
        update(lead, outreach_hold="delivery_test", attempt_count=3)
    client.delete("/v1/settings/test-call", headers=headers)
    again = client.post("/v1/settings/test-call", headers=headers).json()
    assert again["lead_id"] == first["lead_id"]
    ready(workspace)
    answer = client.post("/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["lead"]["id"] == first["lead_id"]
