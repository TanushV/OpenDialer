"""R10.1: what the live helper sees on each turn, and how its reply reaches the rep."""

from datetime import UTC, datetime, timedelta

import pytest
from app.helper.live import INSTRUCTIONS, QUIET, Tips, card, clean, messages

NOW = datetime(2026, 11, 9, 15, tzinfo=UTC)


def request_with(objections, dialogue=None, question=None):
    return {
        "seller": "Agite",
        "lead": {"name": "Robin", "company": "Example Co"},
        "script": {
            "sections": [
                {"key": "opening", "title": "Opening", "options": [{"text": "Hi Robin", "picked": True}]},
                {"key": "discovery", "title": "Discovery", "options": [{"text": "How do you track it?"}]},
                {
                    "key": "objections",
                    "title": "Objections",
                    "options": [
                        {
                            "id": f"objections-{i}",
                            "title": title,
                            "text": f"Response to {title}.",
                            "picked": i == 1,
                        }
                        for i, title in enumerate(objections, 1)
                    ],
                },
            ],
            "template": {"facts": ["Setup takes two weeks"], "never_say": ["Guaranteed savings"]},
        },
        "research": ["Example Co opened a second site in 2026."],
        "earlier_calls": [
            {
                "at": "2026-10-30T18:00:00",
                "outcome": "callback_requested",
                "summary": "Asked for pricing.",
                "next_step": "Call back Tuesday.",
                "notes": "",
            }
        ],
        "dialogue": dialogue or [{"side": "remote", "text": "We already have a vendor for this."}],
        "previous": None,
        "question": question,
    }


def test_every_scripted_objection_and_the_context_reach_the_helper():
    titles = ["Not interested", "Send an email", "Too expensive", "Have a vendor", "Bad timing"]
    system, user = messages(request_with(titles))
    assert system["content"] == INSTRUCTIONS
    prompt = user["content"]
    for title in titles:
        assert f"Response to {title}." in prompt
    assert "Setup takes two weeks" in prompt and "Guaranteed savings" in prompt
    assert "Example Co opened a second site in 2026." in prompt
    assert "Asked for pricing." in prompt and "Call back Tuesday." in prompt
    assert "Prospect: We already have a vendor for this." in prompt
    assert "Lead: name: Robin, company: Example Co" in prompt


def test_the_helper_is_told_how_to_handle_an_objection_and_when_to_stay_quiet():
    assert "closest response under Objections" in INSTRUCTIONS
    assert f"reply with exactly {QUIET}" in INSTRUCTIONS
    assert "untrusted speech" in INSTRUCTIONS


@pytest.mark.parametrize(
    ("dialogue", "stage"),
    [
        ([{"side": "remote", "text": "Hello?"}], "opening"),
        (
            [
                {"side": "rep", "text": "How do you track it?", "script_section": "discovery:0"},
                {"side": "remote", "text": "Spreadsheets."},
            ],
            "Discovery",
        ),
    ],
)
def test_the_current_script_section_reaches_the_helper(dialogue, stage):
    prompt = messages(request_with(["Bad timing"], dialogue=dialogue))[1]["content"]
    assert f"Current stage: {stage}" in prompt


def test_a_private_question_is_sent_as_the_reps_and_never_as_the_prospects():
    prompt = messages(request_with(["Bad timing"], question="What do we charge?"))[1]["content"]
    assert prompt.endswith("Private question from the rep: What do we charge?")


def test_a_stray_quiet_marker_never_reaches_the_rep():
    assert clean(QUIET) is None
    assert clean(f"  Ask about timing. {QUIET}") == "Ask about timing."
    assert clean(" . ") is None
    assert len(clean("x" * 2000)) == 600


def test_a_tip_lasts_for_the_next_moment_and_a_private_answer_long_enough_to_read():
    tip = card("t1", "Ask about timing.", private=False, done=True, now=NOW)
    answer = card("a1", "We charge $40.", private=True, done=False, now=NOW)
    assert tip["kind"] == "TIP" and tip["status"] == "ready"
    assert answer["kind"] == "ANSWER" and answer["status"] == "streaming"
    tips = Tips()
    tips.show("c1", tip)
    assert tips.current("c1", NOW + timedelta(seconds=29))["remaining_ms"] == 1000
    assert tips.current("c1", NOW + timedelta(seconds=31)) is None
    tips.show("c1", answer)
    assert tips.current("c1", NOW + timedelta(minutes=4))["id"] == "a1"


def test_dismiss_only_removes_the_tip_it_names():
    tips = Tips()
    tips.show("c1", card("t1", "Ask.", private=False, done=True, now=NOW))
    tips.dismiss("c1", "other")
    assert tips.current("c1", NOW)["id"] == "t1"
    tips.dismiss("c1", "t1")
    assert tips.current("c1", NOW) is None


@pytest.mark.asyncio
async def test_a_question_replaces_a_turn_still_writing_and_cancel_stops_only_its_own_request():
    import asyncio

    tips = Tips()
    automatic = asyncio.create_task(asyncio.sleep(10))
    tips.start("c1", "auto", automatic)
    assert tips.busy("c1")
    question = asyncio.create_task(asyncio.sleep(10))
    tips.start("c1", "q1", question)
    await asyncio.sleep(0)
    assert automatic.cancelled()
    assert not tips.cancel("c1", "someone-else")
    assert tips.cancel("c1", "q1")
    await asyncio.sleep(0)
    assert question.cancelled()
    tips.finish("c1", question)
    assert not tips.busy("c1")
