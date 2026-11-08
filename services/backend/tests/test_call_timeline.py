"""R4.12 and G6: every call has a readable timeline of what happened, in order."""

from app.calling.timeline import build
from conftest import start


def test_the_timeline_merges_call_steps_carrier_events_and_commands_in_order():
    call = {
        "created_at": "2026-11-08T12:00:00",
        "started_at": "2026-11-08T12:00:00+00:00",
        "submitted_at": "2026-11-08T12:00:01+00:00",
        "ended_at": "2026-11-08T12:00:30+00:00",
    }
    events = [
        {
            "event_type": "call.hangup",
            "received_at": "2026-11-08T12:00:29+00:00",
            "payload": {"hangup_source": "callee", "hangup_cause": "normal_clearing"},
        }
    ]
    commands = [
        {"kind": "dial", "status": "completed", "sent_at": "2026-11-08T12:00:01.5+00:00", "created_at": "x"}
    ]
    entries = build(call, events, commands)
    assert [e["label"] for e in entries] == [
        "Call prepared",
        "Dial sent",
        "dial",
        "call.hangup",
        "Call ended",
    ]
    assert entries[3]["detail"] == "they hung up, normal_clearing"


def test_a_call_timeline_is_served_for_the_review_page(workspace):
    client, headers = workspace["client"], workspace["headers"]
    call = start(workspace, key="timeline-call")
    for event in ("call.answered", "call.hangup"):
        client.post(
            "/v1/simulation/event",
            headers=headers,
            json={"event_id": f"t-{event}", "event_type": event, "call_id": call["id"], "payload": {}},
        )
    entries = client.get(f"/v1/calls/{call['id']}/timeline", headers=headers).json()["entries"]
    labels = [e["label"] for e in entries]
    assert labels[0] == "Call prepared" and "call.answered" in labels and "call.hangup" in labels
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)
