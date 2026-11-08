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
