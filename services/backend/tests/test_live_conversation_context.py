"""What the live helper sees when a prospect objects: every scripted response, approved talk
tracks, reviewed examples, research and the lead's history."""

import pytest
from app.coaching.conversation import INSTRUCTIONS, messages


def request_with(objections):
    return {
        "state_version": 1,
        "prepared_script": {
            "sections": [
                {
                    "key": "follow_up",
                    "title": "This call",
                    "text": "Last conversation: they use a spreadsheet.",
                },
                {
                    "key": "objections",
                    "title": "Objections",
                    "options": [
                        {
                            "id": f"objections-{i}",
                            "title": title,
                            "text": f"Response to {title}.",
                            "picked": True,
                        }
                        for i, title in enumerate(objections, 1)
                    ],
                },
            ],
            "template": {"facts": ["Setup takes two weeks"], "never_say": ["Guaranteed savings"]},
        },
        "approved_cards": [
            {"id": "budget", "tags": ["budget"], "text": "What outcome would make it worthwhile?"}
        ],
        "reviewed_examples": [
            {"id": "ex1", "text": "Acknowledge, then ask what changed since they chose it."}
        ],
        "facts": [{"statement": "Example Co opened a second site in 2026."}],
        "lead_context": {"name": "Robin", "company": "Example Co"},
        "prior_call_context": [
            {"at": "2026-10-30T18:00:00", "outcome": "callback_requested", "rundown": "Asked for pricing."}
        ],
        "recent_dialogue": [
            {"segment_id": "s1", "side": "remote", "text": "We already have a vendor for this."}
        ],
    }


def test_every_scripted_objection_and_the_approved_context_reach_the_helper():
    titles = [
        "Not interested",
        "Send an email",
        "Too expensive",
        "Have a vendor",
        "Bad timing",
        "Need approval",
    ]
    prompt = messages(request_with(titles))[1]["content"]
    for title in titles:
        assert f"Objections ({title}): Response to {title}." in prompt
    assert "What outcome would make it worthwhile?" in prompt
    assert "Acknowledge, then ask what changed since they chose it." in prompt
    assert "Example Co opened a second site in 2026." in prompt and "Setup takes two weeks" in prompt
    assert "Last conversation: they use a spreadsheet." in prompt and "Asked for pricing." in prompt
    assert "Guaranteed savings" in prompt
    assert "Prospect: We already have a vendor for this." in prompt


def test_the_helper_is_told_how_to_handle_an_objection():
    assert "objection" in INSTRUCTIONS.casefold()


def test_unpicked_objection_responses_remain_available_to_the_helper():
    request = request_with(["Have a vendor", "Too expensive"])
    request["prepared_script"]["sections"][1]["options"][1]["picked"] = False
    prompt = messages(request)[1]["content"]
    assert "Objections (Have a vendor): Response to Have a vendor." in prompt
    assert "Objections (Too expensive): Response to Too expensive." in prompt


@pytest.mark.parametrize("stage", ["introduction", "discovery", "objection", "next_step", "declined"])
def test_current_stage_reaches_the_live_helper(stage):
    request = request_with(["Have a vendor"])
    request["state"] = {"stage": stage}
    assert f"Current stage: {stage}" in messages(request)[1]["content"]


def test_a_stray_quiet_marker_never_reaches_the_rep():
    from app.coaching.conversation import reply_card

    request = {
        "state_version": 1,
        "recent_dialogue": [{"segment_id": "s1", "side": "remote", "text": "Price?"}],
    }
    assert reply_card("Say you will send pricing after the review. (nothing)", request)["say_or_ask"] == (
        "Say you will send pricing after the review."
    )
    assert reply_card("(nothing).", request)["action"] == "NO_ACTION"
