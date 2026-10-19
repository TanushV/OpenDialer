"""E08/C032: only prior reviewed, available conversation evidence predicts a call."""

from datetime import timedelta

import pytest
from app.optimization.learning import freeze_features


@pytest.mark.parametrize(
    "case,expected",
    [("prior", ["approval-delay"]), ("future", []), ("provisional", []), ("current", []), ("foreign", [])],
)
def test_prior_observation_features_only(clock, case, expected):
    observation = {
        "id": "observation-1",
        "concept_id": "approval-delay",
        "text": "The approval process is lengthy.",
        "available_at": (clock.now - timedelta(hours=1)).isoformat(),
        "epistemic_status": "model_reviewed",
        "review_receipt_id": "review-1",
        "source_version": 1,
    }
    if case == "future":
        observation["available_at"] = (clock.now + timedelta(hours=1)).isoformat()
    if case == "provisional":
        observation["epistemic_status"] = "model_interpreted"
    calls = [
        {
            "id": "previous",
            "lead_id": "other" if case == "foreign" else "lead",
            "started_at": (clock.now if case == "current" else clock.now - timedelta(days=1)).isoformat(),
            "observations": [observation],
        }
    ]
    lead = {
        "id": "lead",
        "timezone": "UTC",
        "segment": "a",
        "current_call_id": "previous" if case == "current" else None,
    }
    frozen = freeze_features(lead, health={"score": None}, now=clock.now, calls=calls)
    assert frozen.get("feature_schema_version") == 2
    assert frozen.get("conversation_concepts") == expected
    assert frozen.get("conversation_evidence_missing") is (not expected)
    assert "approval process is lengthy" not in str(frozen), (
        "Features retain bounded concept/source IDs, not narrative copies"
    )
