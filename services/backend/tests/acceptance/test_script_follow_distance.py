"""Call insights plan P6 and C6: the script panel never jumps far by accident and never follows the prospect's voice.

Nearby parts (the current one, the next two in Markdown order, the previous one) follow the rep's speech as before.
A far part needs a distinctive phrase (six or more matched words) in two rep segments in a row; after the first it
is only offered as a suggestion. Objections, screening and the gatekeeper are reached from anywhere with the
distinctive phrase alone. A rep segment that repeats the prospect's words at the same moment is speaker bleed.
"""

from journey import get, post
from test_call_statistics import start
from test_campaign_prep import save_script

OPEN = "Hi {first_name}, this is {rep} with {seller}, thanks for picking up."
DISCOVERY = "What part of your customer follow-up process takes the most time each week?"
PITCH = "We take routine administrative work off your sales team so they can sell."
PROOF = "Teams like yours usually get back about a day each week within the first month."
NEXT = "The next step is usually a short look at how your team works today."
CLOSE = "Could we look at your workflow together next Tuesday afternoon? Does that make sense?"
BUDGET = "Totally fair, most teams fund this from the time it saves rather than a new budget line."
MARKDOWN = f"""# Opening
{OPEN}
# Discovery
{DISCOVERY}
# Pitch
{PITCH}
# Proof
{PROOF}
# Next steps
{NEXT}
# Close
{CLOSE}
# Objections
## Budget
{BUDGET}
"""


def say(ws, call_id, sequence, text, *, side="rep", at=None):
    start_ms = (at if at is not None else sequence) * 5000
    post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {
            "segment_id": f"line-{sequence}",
            "revision": 1,
            "side": side,
            "text": text,
            "is_final": True,
            "start_ms": start_ms,
            "end_ms": start_ms + 2500,
        },
    )
    return get(ws, f"/v1/calls/{call_id}")


def position(view):
    return (view["script_position"] or {}).get("key")


def suggestion(view):
    return (view["script_suggestion"] or {}).get("key")


def test_nearby_parts_follow_the_rep_as_before(workspace):
    save_script(workspace, MARKDOWN)
    call_id = start(workspace, 1)
    assert position(
        say(workspace, call_id, 1, "Hi Alex, this is Sam with Example Seller, thanks for picking up.")
    ) == ("opening:opening-1")
    assert position(say(workspace, call_id, 2, DISCOVERY)) == "discovery:discovery-1"
    assert position(say(workspace, call_id, 3, PITCH)) == "pitch:pitch-1"


def test_a_generic_phrase_from_a_far_part_never_moves_the_panel(workspace):
    save_script(workspace, MARKDOWN)
    call_id = start(workspace, 2)
    say(workspace, call_id, 1, "Hi Alex, this is Sam with Example Seller, thanks for picking up.")
    view = say(workspace, call_id, 2, "Does that make sense?")
    assert position(view) == "opening:opening-1"
    assert suggestion(view) is None


def test_a_far_part_is_suggested_once_and_followed_when_the_rep_keeps_reading_it(workspace):
    save_script(workspace, MARKDOWN)
    call_id = start(workspace, 3)
    say(workspace, call_id, 1, "Hi Alex, this is Sam with Example Seller, thanks for picking up.")
    first = say(workspace, call_id, 2, "Could we look at your workflow together next Tuesday afternoon?")
    assert position(first) == "opening:opening-1", "One far match is not enough to jump"
    assert suggestion(first) == "close:close-1", "It is offered instead"
    second = say(
        workspace, call_id, 3, "Could we look at your workflow together next Tuesday afternoon? I mean it."
    )
    assert position(second) == "close:close-1"
    assert suggestion(second) is None


def test_an_objection_response_is_reached_from_anywhere(workspace):
    save_script(workspace, MARKDOWN)
    call_id = start(workspace, 4)
    say(workspace, call_id, 1, "Hi Alex, this is Sam with Example Seller, thanks for picking up.")
    view = say(workspace, call_id, 2, BUDGET)
    assert position(view) == "objections:objections-1"


def test_the_prospects_voice_in_the_reps_microphone_never_moves_the_panel(workspace):
    save_script(workspace, MARKDOWN)
    call_id = start(workspace, 5)
    say(workspace, call_id, 1, "Hi Alex, this is Sam with Example Seller, thanks for picking up.")
    # The prospect reads the close aloud; the rep's microphone picks it up at the same moment.
    say(workspace, call_id, 2, "What part of your customer follow-up process takes most time?", side="remote")
    view = say(workspace, call_id, 3, "What part of your customer follow-up process takes most time?", at=2)
    assert position(view) == "opening:opening-1"
    bled = next(row for row in view["transcript"] if row["segment_id"] == "line-3")
    assert bled["bleed"] is True and not bled["script_section"]


def test_bleed_is_caught_when_the_prospect_segment_arrives_second(workspace):
    save_script(workspace, MARKDOWN)
    call_id = start(workspace, 6)
    say(workspace, call_id, 1, "Hi Alex, this is Sam with Example Seller, thanks for picking up.")
    early = say(workspace, call_id, 2, DISCOVERY)
    assert position(early) == "discovery:discovery-1"
    view = say(workspace, call_id, 3, DISCOVERY, side="remote", at=2)
    assert position(view) == "opening:opening-1", "The bled match no longer counts"
    assert next(row for row in view["transcript"] if row["segment_id"] == "line-2")["bleed"] is True
