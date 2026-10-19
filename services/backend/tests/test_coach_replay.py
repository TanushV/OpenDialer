import pytest
from app.coaching.replay import evaluate
from test_remediation_coach import ordinary_advice


@pytest.mark.asyncio
async def test_replay_measures_grounding_expected_silence_and_deadlines():
    class Provider:
        async def suggest(self, request, deadline_ms):
            if request["id"] == "quiet":
                return {"action": "NO_ACTION"}
            return {
                **ordinary_advice(request),
                "action": "SHOW_CARD",
                "kind": "DISCOVERY",
                "say_or_ask": "What would need to improve?",
                "reason": "Invite the prospect to describe priorities.",
                "state_version": 1,
                "expires_in_ms": 7000,
                "evidence_segment_ids": ["s1"],
                "playbook_card_ids": ["c1"],
            }

    cases = [
        {
            "id": "objection",
            "expected_action": "SHOW_CARD",
            "state_version": 1,
            "recent_dialogue": [{"segment_id": "s1", "text": "We use another provider.", "is_final": True}],
            "approved_cards": [{"id": "c1", "text": "What would need to improve?"}],
        },
        {
            "id": "quiet",
            "expected_action": "NO_ACTION",
            "state_version": 1,
            "recent_dialogue": [],
            "approved_cards": [],
        },
    ]
    report = await evaluate(cases, Provider(), model="contract")
    assert report["valid_fraction"] == 1
    assert report["expected_action_fraction"] == 1
    assert report["samples"] == 2 and report["p95_inference_ms"] >= 0
    assert report["evidence_scope"] == "synthetic replay; excludes STT, network delivery and hardware audio"


async def test_replay_rejects_unsupported_spoken_meaning_even_when_ids_exist():
    class Provider:
        async def suggest(self, request, deadline_ms):
            return {
                "action": "SHOW_CARD",
                "kind": "DISCOVERY",
                "say_or_ask": "Your revenue will double.",
                "reason": "Unsupported",
                "state_version": 1,
                "expires_in_ms": 7000,
                "evidence_segment_ids": ["s"],
                "playbook_card_ids": ["c"],
            }

    case = {
        "id": "unsupported",
        "state_version": 1,
        "expected_action": "SHOW_CARD",
        "recent_dialogue": [{"segment_id": "s", "text": "We have no budget.", "is_final": True}],
        "approved_cards": [{"id": "c", "text": "What would be useful?"}],
    }
    result = await evaluate([case], Provider(), model="fixture")
    assert result["valid_fraction"] == 0
