"""E08/C033: finite response approaches are distinct from generated sentences."""

import pytest
from app.optimization.learning import learn_distribution
from test_live_memory import setup, turn


def test_finite_strategy_assignment():
    arms = ["control", "clarify-owner", "clarify-impact"]
    history = [
        {
            "action": "clarify-owner",
            "conversion": i < 18,
            "segment": "a",
            "propensity": 1 / 3,
            "window_status": "matured",
        }
        for i in range(20)
    ]
    history += [
        {
            "action": "clarify-impact",
            "conversion": 0,
            "segment": "a",
            "propensity": 1 / 3,
            "window_status": "matured",
        }
        for _ in range(20)
    ]
    result = learn_distribution(arms, history, segment="a", exploration=0.1)
    probabilities = result["probabilities"]
    assert set(probabilities) == set(arms)
    assert sum(probabilities.values()) == pytest.approx(1)
    assert probabilities["clarify-owner"] == pytest.approx(0.9 + 0.1 / 3)
    assert probabilities["control"] == pytest.approx(0.1 / 3)
    assert "sentence_propensity" not in result


@pytest.mark.asyncio
async def test_displayed_guidance_is_not_assumed_used_or_converted(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "exposure")
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction(False) as s:
        events = [r.data for r in service.scoped(s, "coach_events", p)]
        assert events
        for event in events:
            exposure = event.get("strategy_exposure")
            assert exposure is not None, (
                "Display metadata must retain the actual strategy and exposure boundary"
            )
            assert exposure["used_by_rep"] is None
            assert exposure["conversion"] is None
            assert exposure["sentence_propensity"] is None
        assert not service.scoped(s, "conversion_events", p)
