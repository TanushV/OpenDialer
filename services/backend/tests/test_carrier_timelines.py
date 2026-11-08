"""The recorded carrier timelines (R0.1) are sanitized and shaped like the 7 real calls they came from."""

import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "carrier_timelines"
PLACEHOLDER_PHONES = {"test-line", "our-number", "rep-sip", None}
FAKE_PHONE = re.compile(r"^\+1555010\d{4}$")


def timelines():
    return {path.name: json.loads(path.read_text()) for path in sorted(FIXTURES.glob("call_*.json"))}


def test_seven_real_calls_are_recorded_with_both_hangup_sources():
    calls = timelines()
    assert len(calls) == 7
    sources = {call["attempt"]["provider_hangup_source"] for call in calls.values()}
    assert sources == {"caller", "callee"}


def test_no_real_number_provider_id_or_speech_survives():
    for name, call in timelines().items():
        text = json.dumps(call)
        assert not re.search(r"v3:[A-Za-z0-9_-]{10,}", text), name
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", text), name
        assert "transcript" not in text and "speech" not in text, name
        for event in call["events"]:
            for field in ("from", "to"):
                value = event[field]
                assert value in PLACEHOLDER_PHONES or FAKE_PHONE.match(value), (name, value)


def test_every_timeline_is_ordered_and_ends_the_prospect_leg():
    for name, call in timelines().items():
        times = [event["t"] for event in call["events"]]
        assert times == sorted(times), name
        hangups = [e for e in call["events"] if e["event_type"] == "call.hangup" and e["leg"] == "prospect"]
        assert len(hangups) == 1, name
