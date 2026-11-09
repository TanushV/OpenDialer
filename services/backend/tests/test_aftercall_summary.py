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
