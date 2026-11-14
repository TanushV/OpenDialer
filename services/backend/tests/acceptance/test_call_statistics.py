"""Replay speech through the call API and export both versions of the script."""

import base64
import csv
import io
import zipfile

from journey import answered_by_human, get, hang_up, import_leads, post, reserve, wrap_up
from test_campaign_prep import save_script

DISCOVERY = "What part of your customer follow-up process takes the most time each week?"
CUSTOM = (
    "The handoff breaks when the sales team has to copy the customer details into three different systems."
)
CLOSE = "Could we review your workflow together next Tuesday?"
MARKDOWN = f"""# Opening
Hi {{first_name}}, this is {{rep}} with {{seller}}.
# Discovery
{DISCOVERY}
# Handoff
{CUSTOM}
# Pitch
We can handle routine administrative tasks for your sales team.
# Close
{CLOSE}
"""


def start(ws, index):
    name = f"Script pilot {index}"
    import_leads(
        ws,
        [{"name": name, "phone": f"+1415555089{index}", "timezone": "America/Los_Angeles"}],
        list_name=name,
    )
    reservation = reserve(ws, list_names=[name]).json()
    placed = post(
        ws,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": name},
    )
    answered_by_human(ws, placed["id"])
    return placed["id"]


def say(ws, identity, sequence, text, *, side="rep", system=False):
    post(
        ws,
        f"/v1/calls/{identity}/transcript",
        {
            "segment_id": f"line-{sequence}",
            "revision": 1,
            "side": side,
            "text": text,
            "is_final": True,
            "system_audio": system,
            "start_ms": sequence * 5000,
            "end_ms": sequence * 5000 + 2500,
        },
    )


def test_transcript_tracking_and_export_use_each_calls_frozen_structure(workspace):
    ws = workspace
    old = save_script(ws, MARKDOWN)["version"]
    first = start(ws, 1)
    frozen = get(ws, f"/v1/calls/{first}")["script_snapshot"]
    assert [s["title"] for s in frozen["sections"] if s.get("options")] == [
        "Opening",
        "Discovery",
        "Handoff",
        "Pitch",
        "Close",
    ]
    new = save_script(ws, MARKDOWN.replace("# Handoff", "# Implementation"))["version"]
    say(ws, first, 1, DISCOVERY)
    say(ws, first, 2, CLOSE, side="remote")
    say(ws, first, 3, CLOSE, system=True)
    say(ws, first, 4, "The handoff breaks when the sales team")
    say(ws, first, 5, "has to copy the customer details into three different systems.")
    say(ws, first, 6, "I just moved apartments last summer.")
    say(ws, first, 7, DISCOVERY)
    speech = get(ws, f"/v1/calls/{first}")["transcript"]
    accepted = [s["script_section"] for s in speech if s.get("script_section")]
    assert accepted[0] == accepted[-1] == "discovery:discovery-1"
    assert "custom:handoff:custom:handoff-1" in accepted
    assert all(
        not s.get("script_section") for s in speech if s["segment_id"] in {"line-2", "line-3", "line-6"}
    )
    hang_up(ws, first)
    wrap_up(ws, first, outcome="not_interested")

    second = start(ws, 2)
    say(ws, second, 1, CUSTOM)
    hang_up(ws, second)
    wrap_up(ws, second, outcome="not_interested")
    payload = base64.b64decode(get(ws, "/v1/review/call-statistics/export")["archive"])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        calls = list(csv.DictReader(io.StringIO(archive.read("calls.csv").decode())))
        events = list(csv.DictReader(io.StringIO(archive.read("script-events.csv").decode())))
        structure = list(csv.DictReader(io.StringIO(archive.read("script-structure.csv").decode())))
        stages = list(csv.DictReader(io.StringIO(archive.read("stage-statistics.csv").decode())))
    by_call = {c["call_id"]: c for c in calls}
    assert by_call[first]["base_version"] == str(old)
    assert by_call[second]["base_version"] == str(new)
    assert by_call[first]["structure_id"] != by_call[second]["structure_id"]
    assert [e["section_title"] for e in events if e["call_id"] == first] == [
        "Discovery",
        "Handoff",
        "Discovery",
    ]
    assert [e["section_title"] for e in events if e["call_id"] == second] == ["Implementation"]
    assert [s["section_title"] for s in structure if s["structure_id"] == by_call[first]["structure_id"]] == [
        "Opening",
        "Discovery",
        "Handoff",
        "Pitch",
        "Close",
    ]
    assert any(row["section_title"] == "Discovery" and row["ended"] == "1" for row in stages)
    assert all(row["section_title"] != "Close" for row in events)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert all(DISCOVERY.encode() not in archive.read(name) for name in archive.namelist())
    assert "14155550891" not in str(calls)


def test_script_sections_keep_reference_headings_and_distinct_matches(workspace):
    ws = workspace
    markdown = MARKDOWN.replace("# Pitch", "# Instructions\nWait for the answer.\n# Pitch")
    save_script(ws, markdown)
    identity = start(ws, 3)
    snapshot = get(ws, f"/v1/calls/{identity}")["script_snapshot"]
    assert [s["title"] for s in snapshot["sections"] if not s.get("purpose")] == [
        "Opening",
        "Discovery",
        "Handoff",
        "Instructions",
        "Pitch",
        "Close",
    ]
    say(ws, identity, 1, "Wait for the answer.")
    assert get(ws, f"/v1/calls/{identity}")["script_position"] is None
    say(ws, identity, 2, "What part of your customer follow-up process takes the most effort each week?")
    assert get(ws, f"/v1/calls/{identity}")["script_position"]["key"] == "discovery:discovery-1"
