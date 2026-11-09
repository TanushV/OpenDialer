"""R4.3 and G4: one gate answers "can I dial?", with a code, a message and a fix for each blocker."""

import re
from pathlib import Path

import pytest
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
    assert app.state.service.carrier(workspace["tenant_id"]).dials == []


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
