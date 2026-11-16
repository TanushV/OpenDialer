"""R10.1: what the live helper sees on each turn, and how its reply reaches the rep."""

from datetime import UTC, datetime, timedelta

import pytest
from app.helper.live import (
    INSTRUCTIONS,
    QUIET,
    Tips,
    answer,
    card,
    fallback,
    messages,
    script_parts,
)

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


def test_the_script_reaches_the_helper_in_authored_order():
    prompt = messages(request_with(["Bad timing"]))[1]["content"]
    lines = [
        "Opening: Hi Robin",
        "Discovery: How do you track it?",
        "Objections · Bad timing: Response to Bad timing.",
    ]
    assert [line for line in prompt.splitlines() if line in lines] == lines


def test_exact_script_wording_links_back_to_its_part():
    script = request_with(["Send an email"])["script"]
    script_wording = "Response to Send an email."
    spoken, part, position, source = answer(script_wording, script)
    assert spoken == script_wording
    assert part == {"label": "Objections · Send an email", "text": script_wording}
    assert position == "objections:objections-1" and source is None


def test_plain_spoken_lines_keep_contextual_estimates_and_typographic_spaces():
    spoken = "Roughly 60 hours for 20 sites. That assumes 3 hours per site."
    assert answer("Say: Roughly 60\u202fhours for 20 sites.\nThat assumes 3 hours per site.", {}) == (
        spoken,
        None,
        None,
        "suggestion",
    )


def test_local_reply_can_give_an_estimate_when_both_factors_are_stated():
    request = request_with(
        [],
        dialogue=[{"side": "remote", "text": "For 20 sites at 3 hours each, what would the rollout take?"}],
    )
    assert fallback(request) == "About 60 hours in total, at 3 hours each for 20 sites."


@pytest.mark.parametrize("output", [QUIET, "", "  ", "Say: ", "Hi, {{name}}"])
def test_blank_or_unfilled_replies_use_the_fallback(output):
    assert answer(output, {}) is None


def test_follow_up_notes_and_custom_reference_sections_have_no_spoken_option():
    request = request_with(["Bad timing"])
    script = request["script"]
    script["sections"].extend(
        [
            {"key": "follow_up", "title": "This call", "text": "Rep note: call the buyer next week."},
            {"key": "custom:handoff", "title": "Handoff", "options": [{"text": "I can introduce you."}]},
            {"key": "custom:reference", "title": "Reference", "text": "Checklist", "spoken": False},
        ]
    )
    parts = script_parts(script, spoken_only=True)
    assert [label for label, _, _ in parts] == ["Opening", "Discovery", "Objections · Bad timing", "Handoff"]
    assert len(script_parts(script)) == len(parts) + 1
    prompt = messages(request)[1]["content"]
    assert (
        "This call:" not in prompt and "Handoff: I can introduce you." in prompt and "Checklist" not in prompt
    )


def test_a_private_question_is_sent_as_the_reps_and_never_as_the_prospects():
    prompt = messages(request_with(["Bad timing"], question="What do we charge?"))[1]["content"]
    assert prompt.endswith("Private question from the rep: What do we charge?")


def test_a_tip_lasts_for_the_next_moment_and_a_private_answer_long_enough_to_read():
    tip = card("t1", "Is now a good time?", private=False, now=NOW)
    answer = card("a1", "What should the email cover?", private=True, now=NOW)
    assert tip["kind"] == "TIP" and tip["status"] == "ready"
    assert answer["kind"] == "ANSWER" and answer["status"] == "ready"
    tips = Tips()
    tips.show("c1", tip)
    assert tips.current("c1", NOW + timedelta(seconds=29))["remaining_ms"] == 1000
    assert tips.current("c1", NOW + timedelta(seconds=31)) is None
    tips.show("c1", answer)
    assert tips.current("c1", NOW + timedelta(minutes=4))["id"] == "a1"


def test_dismiss_only_removes_the_tip_it_names():
    tips = Tips()
    tips.show("c1", card("t1", "Ask.", private=False, now=NOW))
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
