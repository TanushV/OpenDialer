"""New semantic boundaries: facts are constrained, wording is not a phrasebook."""

import pytest
from app.coaching.context import build_context, ground_guidance, merge_semantic_state
from app.coaching.engine import CardRejected
from conftest import NOW
from test_remediation_coach import advice


def test_unfamiliar_short_turn_is_context_without_invented_topic_or_resolution():
    turn = {
        "segment_id": "s1",
        "text": "Union approval pending.",
        "side": "remote",
        "is_final": True,
        "revision": 1,
    }
    state = build_context([turn], version=1, facts=[], commitments=[])
    assert state["trigger"] == "conversation"
    assert state["last_remote"]["text"] == turn["text"]
    assert state["objections"] == [], "Local regex must not manufacture semantic labels"


def test_interpreted_state_is_paraphrased_and_quoted_evidence_is_only_for_validation():
    segments = [{"segment_id": "s1", "text": "The budget is not approved.", "is_final": True, "revision": 1}]
    state = {"stage": "discovery", "objections": []}
    result = merge_semantic_state(
        state,
        {
            "stage": "objection",
            "objections": [
                {
                    "text": "Spending authorization remains pending.",
                    "quote": segments[0]["text"],
                    "evidence_segment_ids": ["s1"],
                    "status": "open",
                }
            ],
        },
        segments,
        at=NOW,
    )
    assert result["objections"][0]["text"] == "Spending authorization remains pending."
    assert "quote" not in result["objections"][0]


def test_guidance_rejects_numerical_claim_disguised_as_a_question():
    request = {
        "state_version": 1,
        "recent_dialogue": [{"segment_id": "s1", "text": "What can you do?", "is_final": True}],
        "approved_cards": [],
        "facts": [],
    }
    with pytest.raises(CardRejected):
        ground_guidance(advice(text="Would our $99 price and 50% improvement work for you?"), request)
