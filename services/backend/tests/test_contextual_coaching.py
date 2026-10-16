import pytest
from app.coaching.context import build_context, ground_guidance, retrieve_cards
from app.coaching.engine import CardRejected
from conftest import start

CARDS = [
    {
        "id": "provider",
        "tags": ["provider", "objection"],
        "text": "What would need to improve before you would consider changing providers?",
    },
    {
        "id": "budget",
        "tags": ["budget", "price", "objection"],
        "text": "What outcome would make the investment worthwhile?",
    },
    {"id": "timing", "tags": ["timing", "callback"], "text": "When would a brief follow-up be useful?"},
    {"id": "discovery", "tags": ["discovery"], "text": "What would make this conversation useful for you?"},
]


def segment(identifier, text, side="remote", **overrides):
    return {"segment_id": identifier, "text": text, "side": side, "is_final": True, **overrides}


def test_state_tracks_evidence_questions_negation_and_resolved_objections():
    dialogue = [
        segment("r1", "What is your budget?", "rep"),
        segment("s1", "We cannot afford another subscription."),
        segment("s2", "Actually the budget has been approved. Can we arrange a meeting?"),
    ]
    state = build_context(dialogue, version=3, facts=[], commitments=[])
    assert state["version"] == 3 and state["stage"] == "next_step"
    assert state["asked_questions"] and state["buying_signals"]
    assert not any(o["kind"] == "budget" and o["status"] == "unresolved" for o in state["objections"])
    assert all(x["evidence_segment_ids"] for x in state["buying_signals"])


def test_context_ignores_partials_system_audio_and_does_not_infer_emotion():
    state = build_context(
        [
            segment("partial", "We have no budget", is_final=False),
            segment("music", "Our provider costs more", system_audio=True),
        ],
        version=2,
        facts=[],
        commitments=[],
    )
    assert not state["objections"] and state["trigger"] == "NO_ACTION"
    assert "emotion" not in state and "personality" not in state


def test_quoted_semantic_state_survives_new_turns_but_not_a_revised_source():
    original = segment("s1", "Duplicated entries consume two hours every week.", revision=1)
    prior = {
        "version": 1,
        "needs": [
            {
                "text": original["text"],
                "quote": original["text"],
                "evidence_segment_ids": ["s1"],
                "evidence_revisions": {"s1": 1},
                "status": "confirmed",
            }
        ],
    }
    next_turn = segment("s2", "Can we arrange a meeting?", revision=1)
    state = build_context([original, next_turn], version=2, facts=[], commitments=[], prior=prior)
    assert state["needs"] and state["needs"][0]["text"] == original["text"]
    revised = {**original, "revision": 2, "text": "We do not have duplicated entries."}
    state = build_context([revised, next_turn], version=3, facts=[], commitments=[], prior=prior)
    assert not state["needs"]


def test_card_retrieval_matches_semantic_budget_objection_instead_of_first_card():
    state = build_context(
        [segment("s", "We cannot afford another subscription.")], version=1, facts=[], commitments=[]
    )
    assert retrieve_cards(state, CARDS)[0]["id"] == "budget"


def test_decline_and_already_answered_question_do_not_repeat_guidance():
    state = build_context(
        [segment("s", "No thanks, I am not interested. Goodbye.")], version=1, facts=[], commitments=[]
    )
    assert state["trigger"] == "NO_ACTION" and retrieve_cards(state, CARDS) == []
    state = build_context(
        [segment("r", CARDS[0]["text"], "rep"), segment("s", "Nothing, our provider works well.")],
        version=2,
        facts=[],
        commitments=[],
    )
    assert "provider" not in [c["id"] for c in retrieve_cards(state, CARDS)]


def test_grounding_rejects_unapproved_meaning_even_with_existing_source_ids():
    request = {
        "state_version": 1,
        "recent_dialogue": [segment("s", "We cannot afford another subscription.")],
        "approved_cards": [CARDS[1]],
        "facts": [],
    }
    card = {
        "action": "SHOW_CARD",
        "kind": "OBJECTION",
        "say_or_ask": CARDS[1]["text"],
        "reason": "Budget objection",
        "evidence_segment_ids": ["s"],
        "playbook_card_ids": ["budget"],
        "state_version": 1,
        "expires_in_ms": 7000,
    }
    assert ground_guidance(card, request)
    with pytest.raises(CardRejected):
        ground_guidance({**card, "say_or_ask": "Your current supplier is stealing your money."}, request)
    with pytest.raises(CardRejected):
        ground_guidance({**card, "say_or_ask": "Our price is $99 and your revenue will double."}, request)


def test_call_state_and_quality_feedback_are_persisted_and_scoped(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    response = c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "budget-turn",
            "revision": 1,
            "side": "remote",
            "text": "We cannot afford another subscription.",
            "is_final": True,
        },
    )
    assert response.status_code == 200
    live = c.get(f"/v1/calls/{call['id']}", headers=h).json()
    assert live["conversation_state"]["trigger"] == "budget"
    assert live["conversation_state"]["version"] == 1
    assert live["coach"] is not None
    feedback = c.post(
        f"/v1/calls/{call['id']}/coach/feedback",
        headers=h,
        json={
            "coach_event_id": live["coach"]["id"],
            "useful": True,
            "grounded": True,
            "timely": True,
            "distracting": False,
            "delivery_ms": 75,
        },
    )
    assert feedback.status_code == 200, feedback.text
    report = c.get("/v1/optimization", headers=h).json()["coaching"]
    assert report["reviewed_cards"] == 1 and report["useful_fraction"] == 1


def test_state_and_retrieval_timing_are_distinct_and_transcript_receipt_is_idempotent(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    segment = {
        "segment_id": "measured",
        "revision": 1,
        "side": "remote",
        "text": "We have no budget.",
        "is_final": True,
    }
    response = c.post(f"/v1/calls/{call['id']}/transcript", headers=h, json=segment)
    assert response.status_code == 200
    assert c.post(f"/v1/calls/{call['id']}/transcript", headers=h, json=segment).json()["duplicate"]
    detailed = c.get(f"/v1/calls/{call['id']}", headers=h).json()
    assert detailed["transcript"][0]["server_received_at"]
    assert detailed["coach"]["latency"]["state_ms"] >= 0
    assert detailed["coach"]["latency"]["retrieval_ms"] >= 0
