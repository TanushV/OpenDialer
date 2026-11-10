"""R10.2 and R10.3: one request over the whole transcript; each field stands or falls on its own."""

import asyncio

from app.aftercall import summary
from app.providers.inference import InferenceError


def seg(i, side, text, **extra):
    return {"segment_id": f"s{i}", "side": side, "text": text, "start_ms": i * 1000, **extra}


class Client:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.seen = data, error, []

    async def complete(self, instructions, context, schema):
        self.seen.append(context)
        if self.error:
            raise self.error
        return type("R", (), {"data": self.data})()


def test_the_whole_conversation_is_sent_not_a_page_of_32_segments():
    segments = [seg(i, "rep" if i % 2 else "remote", f"line {i}") for i in range(100)]
    text = summary.transcript_text(segments)
    assert "line 0" in text and "line 99" in text and text.count("\n") == 99


def test_a_very_long_call_keeps_its_opening_and_its_end():
    segments = [seg(i, "remote", "x" * 900 + f" end{i}") for i in range(200)]
    text = summary.transcript_text(segments)
    assert len(text) <= summary.MAX_CHARS + 60
    assert "end0" in text and "end199" in text and "left out" in text


def test_a_bad_field_drops_alone_and_the_rest_stand():
    parsed = summary.parse(
        {"summary": "They asked for a callback on Tuesday.", "suggested_outcome": "made_up", "next_step": 42}
    )
    assert parsed["status"] == "ready" and parsed["summary"] == "They asked for a callback on Tuesday."
    assert parsed["suggested_outcome"] is None and parsed["next_step"] is None


def test_no_summary_is_a_failure_with_a_reason_never_partial():
    parsed = summary.parse(
        {"summary": "", "suggested_outcome": "not_interested", "outcome_reason": "Said no."}
    )
    assert parsed["status"] == "failed" and parsed["error"]
    assert parsed["suggested_outcome"] == {"code": "not_interested", "reason": "Said no."}


def test_one_request_returns_summary_outcome_and_next_step():
    client = Client(
        {
            "summary": "They agreed to a demo next week.",
            "suggested_outcome": "meeting_booked",
            "outcome_reason": "Prospect said Thursday at 2 works.",
            "next_step": "Send the calendar invite.",
        }
    )
    result = asyncio.run(
        summary.summarize(
            client,
            [seg(1, "rep", "Can we meet?"), seg(2, "remote", "Thursday at 2 works.")],
            {"name": "Dana"},
            None,
        )
    )
    assert len(client.seen) == 1 and "Prospect: Thursday at 2 works." in client.seen[0]["transcript"]
    assert result["status"] == "ready" and result["suggested_outcome"]["code"] == "meeting_booked"
    assert result["next_step"] == "Send the calendar invite."


def test_no_speech_makes_no_request_and_a_failure_says_why():
    client = Client({})
    assert asyncio.run(summary.summarize(client, [], {}, None))["status"] == "no_speech" and not client.seen
    failing = Client(error=InferenceError("Inference HTTP 503"))
    result = asyncio.run(summary.summarize(failing, [seg(1, "remote", "Hello")], {}, None))
    assert result["status"] == "failed" and "503" in result["error"]


PARTS = [
    ("Opening", "Hey Dana, it's Sam from Agite. Can I take 27 seconds?", ""),
    ("Pitch · Short", "We take CRM work off your reps so they sell more.", ""),
    ("Objections · Too busy", "Totally fair; when is a better time?", ""),
]


def test_the_summary_names_the_script_part_where_the_call_ended():
    """D31, R11.6: stage drop-off reads one number from the summary; a number outside the script drops alone."""
    client = Client(
        {
            "summary": "They said they were busy.",
            "suggested_outcome": "callback_requested",
            "outcome_reason": "Call me next week.",
            "next_step": "Call next week.",
            "ended_at_part": 3,
        }
    )
    result = asyncio.run(
        summary.summarize(client, [seg(1, "rep", "Can I take 27 seconds?")], {"name": "Dana"}, None, PARTS)
    )
    assert [p["number"] for p in client.seen[0]["script_parts"]] == [1, 2, 3]
    assert client.seen[0]["script_parts"][1]["label"] == "Pitch · Short"
    assert result["ended_at_part"] == {"number": 3, "label": "Objections · Too busy", "stage": "Objections"}
    for bad in (0, 4, "2", None, -1, True):
        parsed = summary.parse({"summary": "Short.", "ended_at_part": bad}, PARTS)
        assert parsed["ended_at_part"] is None and parsed["status"] == "ready"


def test_drop_off_counts_answered_calls_by_the_stage_they_ended_in_script_order():
    from app.aftercall.dropoff import drop_off

    def call(stage, outcome, human=True):
        return {
            "human": human,
            "outcome": outcome,
            "ended_at_part": {"number": 1, "label": stage, "stage": stage} if stage else None,
        }

    calls = [
        call("Opening", "not_interested"),
        call("Opening", "not_interested"),
        call("Pitch", "callback_requested"),
        call("Close", "meeting_booked"),
        call(None, "not_interested"),
        call("Opening", "voicemail", human=False),
    ]
    report = drop_off(calls, ["Opening", "Pitch", "Objections", "Close"])
    assert report["answered"] == 5 and report["placed"] == 4
    assert [(s["stage"], s["calls"]) for s in report["stages"]] == [
        ("Opening", 2),
        ("Pitch", 1),
        ("Objections", 0),
        ("Close", 1),
    ]
    assert report["stages"][0]["outcomes"] == {"not_interested": 2}
