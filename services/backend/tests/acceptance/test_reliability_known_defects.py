"""Known defects from CALLING_SCRIPT_RELIABILITY_PLAN.md section 2, reproduced at 5466b36 (audit of November 23, 2026).

Each test states the required behavior and is marked `xfail(strict=True)`, so it documents the defect without
turning `main` red and fails loudly once the work package fixes it (then remove the marker). They are not fixes.
Rows that live in the external generator (`saved_playbook()` tuple order, batch checkpointing) are outside this
repository and have no test here.
"""

import json
from datetime import timedelta

import pytest
from app import ops
from app.platform.db import update
from conftest import ready
from helpers import principal
from journey import call, event, get, post, run_jobs
from test_feature_live_helper import chunk, save_helper_script, with_helper
from test_number_setup import SF, connect, fresh, own_numbers  # noqa: F401 - `fresh` is a fixture

PHONE = "+12025550185"
ROW = "CALLING_SCRIPT_RELIABILITY_PLAN.md section 2, row "


def _call_doc(app, ws, call_id):
    service = app.state.service
    with service.db.transaction(False) as s:
        return dict(ops.required(service, s, "call_attempts", call_id, principal(ws)).data)


def _test_call(ws, *, from_list, key="known-defect"):
    """Place one test call to PHONE through the real Settings and Dialer routes, as the existing tests do."""
    request = {"campaign_id": ws["campaign_id"], "test_only": True}
    if from_list:
        call(ws, "PUT", "/v1/settings/test-list", json={"rows": [{"phone": PHONE, "name": "Test phone"}]})
        request["test_run_id"] = post(ws, "/v1/settings/test-list/start")["id"]
    else:
        call(ws, "PUT", "/v1/admin/settings", json={"test_phone": PHONE})
        post(ws, "/v1/settings/test-call")
    ready(ws)
    held = post(ws, "/v1/queue/next", request)
    assert held["lead"]["phone"] == PHONE
    return post(ws, "/v1/calls/start", {"reservation_id": held["reservation_id"], "idempotency_key": key})


@pytest.mark.xfail(
    strict=True, reason=ROW + "'Test script': calling/dial.py:101-109 stores no script for a test"
)
@pytest.mark.parametrize("from_list", [False, True])
def test_a_test_call_carries_the_saved_base_script(workspace, app, from_list):
    save_helper_script(workspace)
    placed = _test_call(workspace, from_list=from_list)
    snapshot = _call_doc(app, workspace, placed["id"]).get("script_snapshot")
    assert snapshot and snapshot.get("sections"), "the saved base script must be snapshotted on the test call"


@pytest.mark.xfail(
    strict=True,
    reason=ROW
    + "'Test helper': privacy.py:721 drops test transcripts; live_help.py:75 treats tests as ended",
)
def test_an_answered_test_call_gets_a_helper_reply(workspace, app, clock):
    ws = workspace
    with_helper(ws)
    placed = _test_call(ws, from_list=False)
    event(ws, placed["id"], "call.answered")
    received = chunk(ws, placed["id"], "s1", "We have no budget for this right now.")
    assert received.get("unavailable") != "test_call", received
    clock.now += timedelta(seconds=1)
    run_jobs(ws)
    assert get(ws, f"/v1/calls/{placed['id']}")["coach"], "the helper must answer a practice turn"


@pytest.mark.xfail(
    strict=True,
    reason=ROW
    + "'Test speech startup': telephony/workflows.py:189 starts transcription only with an outreach lead",
)
def test_an_answered_test_call_starts_transcription(workspace, app):
    ws = workspace
    placed = _test_call(ws, from_list=False)
    event(ws, placed["id"], "call.answered")
    assert _call_doc(app, ws, placed["id"]).get("transcription_layout"), "speech setup never ran for the test"


def test_control_an_answered_outreach_call_starts_transcription(workspace, app):
    """Control for the test above: the same check passes on an ordinary call, so the xfail is test-specific."""
    from journey import start

    call_id = start(workspace)["id"]
    event(workspace, call_id, "call.answered")
    assert _call_doc(app, workspace, call_id).get("transcription_layout")


@pytest.mark.xfail(
    strict=True,
    reason=ROW
    + "'Test eligibility': calling/test_calls.py:29 reads shared outreach holds (administration.py:675)",
)
@pytest.mark.parametrize("hold", ["temporary_hold", "wrong_party"])
def test_an_outreach_hold_on_the_same_phone_does_not_block_practice(workspace, app, hold):
    ws = workspace
    service = app.state.service
    p = principal(ws)
    post(
        ws,
        "/v1/leads/import",
        {
            "campaign_id": ws["campaign_id"],
            "list_name": "Outreach",
            "rows": [{"name": "Prospect", "phone": PHONE}],
        },
    )
    with service.db.transaction() as s:
        if hold == "temporary_hold":
            ops.suppress(service, p, PHONE, "temporary_hold", event_id="audit-hold", session=s)
        else:
            update(ops._by_key(service, s, p, "phone_endpoints", PHONE), ownership_status="wrong_party")
    placed = _test_call(ws, from_list=False)
    assert placed["is_test"]


@pytest.mark.xfail(
    strict=True,
    reason="CALLING_SCRIPT_RELIABILITY_PLAN.md section 3.7: rep_workflows.py:918-927 turns a practice opt-out into "
    "a workspace-wide suppression of the same phone",
)
def test_a_practice_opt_out_stays_in_the_test_context(workspace, app):
    ws = workspace
    service = app.state.service
    p = principal(ws)
    post(
        ws,
        "/v1/leads/import",
        {
            "campaign_id": ws["campaign_id"],
            "list_name": "Outreach",
            "rows": [{"name": "Prospect", "phone": PHONE}],
        },
    )
    placed = _test_call(ws, from_list=False)
    event(ws, placed["id"], "call.answered")
    event(ws, placed["id"], "call.hangup", hangup_source="callee")
    from helpers import disposition_body

    body = disposition_body(ws["client"], ws["headers"], placed["id"], outcome="opt_out")
    post(ws, f"/v1/calls/{placed['id']}/disposition", body)
    with service.db.transaction(False) as s:
        assert not ops.suppressed(service, s, p, PHONE), "practice speech/outcomes must not suppress outreach"


@pytest.mark.xfail(
    strict=True, reason=ROW + "'Detection': calling/dial.py:75 requests premium detection for test lists"
)
def test_a_test_list_call_disables_answering_machine_detection(workspace, carrier):
    _test_call(workspace, from_list=True)
    assert carrier.dials[-1]["answering_machine_detection"] == "disabled"


@pytest.mark.xfail(
    strict=True, reason=ROW + "'Detection': calling/dial.py:75 requests premium detection for outreach"
)
def test_an_outreach_call_disables_answering_machine_detection(workspace, carrier):
    from journey import start

    start(workspace)
    assert carrier.dials[-1]["answering_machine_detection"] == "disabled"


@pytest.mark.xfail(
    strict=True,
    reason=ROW
    + "'Callback verification copy': administration.py:322 shows CATALOG text with a literal {phone}",
)
def test_settings_shows_a_usable_callback_verification_step(request, carrier):
    ws = request.getfixturevalue("fresh")
    connect(ws)
    own_numbers(carrier)
    post(ws, "/v1/admin/numbers/add", {"phone": SF})
    assert "CALLER_NUMBER_CALLBACK_TEST_REQUIRED" in get(ws, "/v1/admin/preflight")["blockers"]
    rows = json.dumps(get(ws, "/v1/settings/rows"))
    assert "{phone}" not in rows and "One test call left" not in rows, rows
