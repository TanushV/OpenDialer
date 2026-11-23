"""Manual voicemail control with no detection (work package section 6).

The rep ends a mailbox with End as voicemail. Inside the carrier's short-call window (Telnyx counts connected calls
of 6 seconds or less) the rep is freed at once, but the carrier hang-up waits until 7 seconds after answer. Escape
and any end before answer stay immediate. Late detection receipts from older calls change nothing.
"""

from datetime import timedelta

from journey import event, get, post, start, wrap_up


def hangups(carrier, provider_id):
    return [h for h in carrier.hangups if h["provider_id"] == provider_id]


def answered(ws, clock, call_id):
    event(ws, call_id, "call.answered")
    return clock.now


def test_end_as_voicemail_inside_seven_seconds_holds_the_carrier_hangup(workspace, carrier, clock):
    ws = workspace
    placed = start(ws)
    at = answered(ws, clock, placed["id"])
    clock.now = at + timedelta(seconds=3)
    ended = post(ws, f"/v1/calls/{placed['id']}/hangup", {"mark_voicemail": True})
    provider = get(ws, f"/v1/calls/{placed['id']}")["provider_id"]
    assert ended["state"] == "ENDING"
    assert ended["carrier_hold_until"] == (at + timedelta(seconds=7)).isoformat()
    assert not hangups(carrier, provider), "No carrier hang-up inside the short-call window"
    clock.now = at + timedelta(seconds=6)
    post(ws, "/v1/admin/recovery")
    assert not hangups(carrier, provider)
    clock.now = at + timedelta(seconds=7)
    post(ws, "/v1/admin/recovery")
    assert len(hangups(carrier, provider)) == 1
    post(ws, "/v1/admin/recovery")
    assert len(hangups(carrier, provider)) == 1, "The held stop is sent once"


def test_end_as_voicemail_after_seven_seconds_and_escape_are_immediate(workspace, carrier, clock):
    ws = workspace
    first = start(ws, key="late-voicemail")
    at = answered(ws, clock, first["id"])
    clock.now = at + timedelta(seconds=9)
    post(ws, f"/v1/calls/{first['id']}/hangup", {"mark_voicemail": True})
    assert len(hangups(carrier, get(ws, f"/v1/calls/{first['id']}")["provider_id"])) == 1
    event(ws, first["id"], "call.hangup")
    wrap_up(ws, first["id"], outcome="voicemail", voicemail_message="none")
    second = start(ws, key="escape")
    at = answered(ws, clock, second["id"])
    clock.now = at + timedelta(seconds=2)
    post(ws, f"/v1/calls/{second['id']}/hangup")
    assert len(hangups(carrier, get(ws, f"/v1/calls/{second['id']}")["provider_id"])) == 1


def test_a_carrier_end_during_the_hold_cancels_the_held_stop(workspace, carrier, clock):
    ws = workspace
    placed = start(ws)
    at = answered(ws, clock, placed["id"])
    clock.now = at + timedelta(seconds=2)
    post(ws, f"/v1/calls/{placed['id']}/hangup", {"mark_voicemail": True})
    event(ws, placed["id"], "call.hangup", hangup_source="callee")
    clock.now = at + timedelta(seconds=8)
    post(ws, "/v1/admin/recovery")
    assert not hangups(carrier, get(ws, f"/v1/calls/{placed['id']}")["provider_id"])
    assert get(ws, f"/v1/calls/{placed['id']}")["state"] == "ENDED"


def test_end_as_voicemail_waits_for_the_rep_to_say_whether_a_message_was_left(workspace, clock):
    ws = workspace
    placed = start(ws)
    at = answered(ws, clock, placed["id"])
    clock.now = at + timedelta(seconds=20)
    post(ws, f"/v1/calls/{placed['id']}/hangup", {"mark_voicemail": True})
    event(ws, placed["id"], "call.hangup")
    ended = get(ws, f"/v1/calls/{placed['id']}")
    assert ended["state"] == "ENDED" and not ended.get("outcome"), "Neither choice is preselected"
    saved = wrap_up(ws, placed["id"], outcome="voicemail", voicemail_message="left")
    assert saved["outcome"] == "voicemail"
    assert get(ws, f"/v1/calls/{placed['id']}")["disposition_input"]["voicemail_message"] == "left"


def test_late_detection_receipts_change_nothing(workspace, carrier, clock):
    ws = workspace
    placed = start(ws)
    answered(ws, clock, placed["id"])
    for kind in (
        "call.machine.premium.detection.ended:machine",
        "call.machine.premium.greeting.ended:beep_detected",
        "call.machine.premium.call_screening.detected:screening",
    ):
        event_type, result = kind.split(":")
        post(
            ws,
            "/v1/simulation/event",
            {
                "event_id": kind,
                "call_id": placed["id"],
                "event_type": event_type,
                "payload": {"result": result},
            },
        )
    live = get(ws, f"/v1/calls/{placed['id']}")
    assert live["state"] == "ANSWERED_UNCLASSIFIED"
    assert not live.get("machine_detected") and not live.get("screening_detected")
    assert not carrier.hangups


def test_the_voicemail_wording_in_a_call_carries_its_caller_id(workspace):
    """Work package 6.13: `{callback_number}` becomes the number this call presents."""
    ws = workspace
    version = get(ws, "/v1/optimization/script")["base"]["version"]
    markdown = (
        "# Opening\nHi {first_name}, this is {rep} with {seller}.\n\n# Close\nWould next week work?\n\n"
        "# Voicemail\nHi {first_name}, {rep} with {seller}. Call me back at {callback_number}.\n"
    )
    post(ws, "/v1/admin/script/import", {"markdown": markdown, "expected_version": version, "preview": False})
    placed = start(ws)
    snapshot = get(ws, f"/v1/calls/{placed['id']}")["script_snapshot"]
    voicemail = next(s for s in snapshot["sections"] if s["key"] == "voicemail")
    assert "{callback_number}" not in voicemail["text"]
    assert "Call me back at " in voicemail["text"] and voicemail["text"].rstrip(".")[-12:-9].isdigit()
