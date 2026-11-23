"""Test calls use the normal call experience: base script, speech and the live helper (work package section 3).

Practice speech and outcomes stay inside the test: no outreach holds, suppressions or CRM work.
"""

from datetime import timedelta

import pytest
from journey import call, event, get, post, ready, run_jobs, wrap_up

PHONE = "+12025550185"
OPENING = "Hi {first_name}, this is {rep} with {seller}."
BUDGET = "Is the budget set for this year?"
SCRIPT = f"""# Opening
{OPENING}

# Objections
## Budget
{BUDGET}

## Not interested
Fair enough. Is that because it is handled, or because now is not the time?

# Close
Would a short call next week be useful?
"""


def save_script(ws, markdown=SCRIPT):
    version = get(ws, "/v1/optimization/script")["base"]["version"]
    post(ws, "/v1/admin/script/import", {"markdown": markdown, "expected_version": version, "preview": False})


def with_helper(ws):
    save_script(ws)
    call(ws, "PUT", "/v1/admin/settings", json={"openrouter_api_key": "test-openrouter"})


def place_test_call(ws, *, from_list=False, key="practice-1"):
    request = {"campaign_id": ws["campaign_id"], "test_only": True}
    if from_list:
        call(ws, "PUT", "/v1/settings/test-list", json={"rows": [{"phone": PHONE, "name": "Pat Practice"}]})
        request["test_run_id"] = post(ws, "/v1/settings/test-list/start")["id"]
        ready(ws)
        held = post(ws, "/v1/queue/next", request)
    else:
        call(ws, "PUT", "/v1/admin/settings", json={"test_phone": PHONE})
        post(ws, "/v1/settings/test-call")
        ready(ws)
        held = post(ws, "/v1/calls/test/reserve")
    placed = post(ws, "/v1/calls/start", {"reservation_id": held["reservation_id"], "idempotency_key": key})
    assert placed["test_contact"]
    return held, placed


def spoken(script):
    return [option["text"] for section in script["sections"] for option in section.get("options", [])]


def chunk(ws, call_id, segment_id, text, *, side="remote", final=True):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": 1, "side": side, "is_final": final, "text": text},
    )


@pytest.mark.parametrize("from_list", [False, True])
def test_a_test_call_shows_the_saved_base_script_before_and_during_the_call(workspace, from_list):
    ws = workspace
    save_script(ws)
    held, placed = place_test_call(ws, from_list=from_list)
    before = get(ws, f"/v1/test-contacts/{held['lead']['id']}/packet")
    assert BUDGET in spoken(before["script"])
    snapshot = get(ws, f"/v1/calls/{placed['id']}")["script_snapshot"]
    assert snapshot and BUDGET in spoken(snapshot)
    assert any(text.startswith("Hi Pat") for text in spoken(snapshot)) or not from_list


def test_a_master_edit_during_a_test_keeps_the_snapshot_and_the_next_test_takes_the_new_master(
    workspace, carrier
):
    ws = workspace
    save_script(ws)
    _, first = place_test_call(ws)
    save_script(ws, SCRIPT.replace(BUDGET, "Is money set aside for this?"))
    assert BUDGET in spoken(get(ws, f"/v1/calls/{first['id']}")["script_snapshot"])
    event(ws, first["id"], "call.hangup", hangup_cause="no_answer")
    _, second = place_test_call(ws, key="practice-2")
    assert "Is money set aside for this?" in spoken(get(ws, f"/v1/calls/{second['id']}")["script_snapshot"])
    assert len(carrier.dials) == 2


def test_test_call_speech_starts_on_answer_and_reaches_the_live_helper(workspace, clock):
    ws = workspace
    with_helper(ws)
    _, placed = place_test_call(ws)
    event(ws, placed["id"], "call.answered")
    assert get(ws, f"/v1/calls/{placed['id']}").get("transcription_layout")
    chunk(ws, placed["id"], "s1", "We have no budget for this right now.")
    clock.now += timedelta(seconds=1)
    run_jobs(ws)
    live = get(ws, f"/v1/calls/{placed['id']}")
    assert live["coach"] and live["coach"]["say_or_ask"], "A practice turn gets a helper reply"
    assert live["state"] not in {"ENDING", "ENDED", "FINALIZED"}


def test_practice_stop_calling_creates_no_hold_and_the_test_stays_callable(workspace, clock):
    ws = workspace
    with_helper(ws)
    _, placed = place_test_call(ws)
    event(ws, placed["id"], "call.answered")
    chunk(ws, placed["id"], "s1", "Stop calling me, take me off your list.")
    clock.now += timedelta(seconds=1)
    run_jobs(ws)
    live = get(ws, f"/v1/calls/{placed['id']}")
    assert live["state"] not in {"ENDING", "ENDED", "FINALIZED"}, "Practice dialogue never hangs up"
    event(ws, placed["id"], "call.hangup")
    wrap_up(ws, placed["id"], outcome="not_interested")
    _, again = place_test_call(ws, key="practice-2")
    assert again["id"] != placed["id"]


def test_an_outreach_hold_on_the_same_number_does_not_block_a_test_or_get_erased(workspace):
    ws = workspace
    post(
        ws,
        "/v1/leads/import",
        {
            "campaign_id": ws["campaign_id"],
            "list_name": "Outreach",
            "rows": [{"name": "Prospect", "phone": PHONE}],
        },
    )
    post(ws, "/v1/suppressions", {"phone": PHONE, "reason": "temporary_hold"})
    _, placed = place_test_call(ws)
    assert placed["test_contact"]
    leads = get(ws, "/v1/workspace/leads", params={"q": "Prospect"})["items"]
    assert [lead["suppressed"] for lead in leads if lead["phone"] == PHONE] == [True]


@pytest.mark.parametrize("kind", ["single", "list", "outreach"])
def test_every_new_dial_disables_voicemail_detection(workspace, carrier, kind):
    ws = workspace
    if kind == "outreach":
        ready(ws)
        held = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]})
        post(ws, "/v1/calls/start", {"reservation_id": held["reservation_id"], "idempotency_key": "amd"})
    else:
        place_test_call(ws, from_list=kind == "list")
    assert carrier.dials[-1]["answering_machine_detection"] == "disabled"
