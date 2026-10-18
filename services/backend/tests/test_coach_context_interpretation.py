"""A22: local phrase matches must not decide what a prospect meant."""

import pytest
from app.coaching.context import build_context, ground_guidance, merge_semantic_state
from app.db import get
from conftest import start
from test_remediation_coach import advice
from test_remediation_privacy import principal


def utterance(text):
    return {"segment_id": "nuance", "revision": 1, "side": "remote", "is_final": True, "text": text}


@pytest.mark.parametrize(
    "text",
    [
        "I am not interested in replacing it; can you help with the reporting?",
        "No thanks to another subscription, but tell me about usage-based costs.",
        "The previous supplier said goodbye after the sale. How do you support customers?",
    ],
)
def test_local_context_does_not_turn_a_phrase_into_a_global_refusal(text):
    state = build_context([utterance(text)], version=1, facts=[], commitments=[])
    assert state["trigger"] == "conversation"
    assert state["stage"] == "introduction", "Only contextual interpretation may change this stage"


def test_current_interpretation_can_reconsider_a_previous_decline():
    speech = utterance("Actually, I do have a question about the support process.")
    state = build_context([speech], version=2, facts=[], commitments=[], prior={"stage": "declined"})
    revised = merge_semantic_state(
        state,
        {
            "stage": "discovery",
            "open_questions": [
                {
                    "text": "The support workflow needs clarification.",
                    "status": "open",
                    "quote": "a question about the support process",
                    "evidence_segment_ids": ["nuance"],
                }
            ],
        },
        [speech],
    )
    assert revised["stage"] == "discovery"
    result = advice(2, "nuance", "Which part of the support process would you like to understand?")
    result["state_update"] = {"stage": "discovery"}
    assert ground_guidance(result, {"state_version": 2, "state": state, "recent_dialogue": [speech]})


async def test_nuanced_refusal_does_not_hide_contextual_help_but_opt_out_still_stops_it(
    workspace,
    app,
    monkeypatch,
):
    call = start(workspace)
    service, p, captured = app.state.service, principal(workspace), []

    class Coach:
        model = last_model = "fixture"
        last_provider = "Cerebras"
        last_usage = {"cost": 0.001}
        last_request_id = "nuanced-help"

        async def suggest(self, request, **kwargs):
            captured.append(request)
            return advice(
                request["state_version"],
                "nuance",
                "What reporting would be useful alongside the existing system?",
            )

    monkeypatch.setattr(service, "configured_coach", lambda p: (Coach(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    service._receive_transcript(
        p,
        call["id"],
        utterance("I am not interested in replacing it; can you help with the reporting?"),
        None,
        True,
    )
    result = await service.coach_turn(p, call["id"])
    assert result["action"] == "SHOW_CARD" and result["say_or_ask"].startswith("What reporting")
    with service.db.transaction(False) as s:
        phone = get(s, "leads", call["lead_id"]).data["phone"]
    service.suppress(p, phone, "opt_out", event_id="context-help-opt-out")
    denied = await service.coach_turn(p, call["id"], urgent=True)
    assert denied["action"] == "NO_ACTION" and len(captured) == 1
