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


@pytest.mark.parametrize("invalidation", ["before", "after", "unknown"])
def test_feature_source_invalidation_uses_the_decision_cutoff(clock, invalidation):
    at = clock.now
    item = {
        "id": "o",
        "concept_id": "approval",
        "available_at": (at - timedelta(hours=1)).isoformat(),
        "epistemic_status": "model_reviewed",
        "review_receipt_id": "receipt",
        "state": "stale",
        "version": 2,
    }
    if invalidation != "unknown":
        item["invalidated_at"] = (at + timedelta(minutes=1 if invalidation == "after" else -1)).isoformat()
    calls = [
        {
            "id": "prior",
            "lead_id": "lead",
            "started_at": (at - timedelta(days=1)).isoformat(),
            "observations": [item],
        }
    ]
    result = freeze_features({"id": "lead"}, health={}, now=at, calls=calls)
    assert result.get("conversation_concepts") == (["approval"] if invalidation == "after" else [])
    assert result.get("local_time_uncertain") is True
    assert result.get("decision_at") == at.isoformat()


def test_review_received_after_decision_is_not_prior_knowledge(clock):
    call = {
        "id": "c",
        "lead_id": "l",
        "started_at": (clock.now - timedelta(days=1)).isoformat(),
        "observations": [
            {
                "id": "o",
                "concept_id": "approval",
                "available_at": (clock.now - timedelta(hours=1)).isoformat(),
                "reviewed_at": (clock.now + timedelta(minutes=1)).isoformat(),
                "epistemic_status": "model_reviewed",
                "review_receipt_id": "r",
            }
        ],
    }
    assert (
        freeze_features({"id": "l"}, health={}, now=clock.now, calls=[call]).get("conversation_concepts")
        == []
    )


def test_persisted_decision_owns_its_sources_goal_and_allowed_actions(workspace, app):
    from app.db import rows
    from conftest import start

    call = start(workspace)
    with app.state.db.transaction(False) as s:
        frozen = next(r.data for r in rows(s, "feature_snapshots") if r.id == call["feature_snapshot_id"])
    assert frozen["decision_at"] == frozen["as_of"]
    assert frozen["source_versions"]["lead"]["id"] == call["lead_id"]
    assert frozen.get("company_id"), "Evaluation must group the company known at the decision"
    assert frozen["goal"] == "meeting_held" and frozen["outcome_window_days"] == 14
    assert frozen["chosen_action"] in frozen["allowed_actions"]
    assert frozen["action_distribution"][frozen["chosen_action"]] == frozen["propensity"]
    assert frozen["policy_version"] and frozen["model_version"]


def test_source_changed_after_scoring_is_reselected_before_snapshot(workspace, app, monkeypatch):
    from app.db import rows, update
    from conftest import start

    service = app.state.service
    claim = service._claim_selection
    changed = []

    def change(p, campaign_id, plan):
        if not changed:
            with service.db.transaction() as s:
                lead = service.required(s, "leads", plan["selection"]["lead_id"], p)
                update(lead, segment="corrected-before-claim")
                changed.append(lead.id)
        return claim(p, campaign_id, plan)

    monkeypatch.setattr(service, "_claim_selection", change)
    call = start(workspace)
    with service.db.transaction(False) as s:
        frozen = next(r.data for r in rows(s, "feature_snapshots") if r.id == call["feature_snapshot_id"])
        lead = service.required(
            s, "leads", call["lead_id"], {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")}
        )
        assert frozen["features"]["segment"] == lead.data["segment"]


def test_current_review_state_and_versions_are_frozen_in_the_actual_next_decision(workspace, app, clock):
    from app.db import put, rows, update
    from conftest import disposition_body, start

    service, c, h = app.state.service, workspace["client"], workspace["headers"]
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    call = start(workspace)
    clock.now += timedelta(seconds=30)
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="right_party"),
    )
    observation = {
        "id": "o",
        "version": 1,
        "kind": "approach",
        "text": "A lengthy approval process.",
        "concept_id": "approval",
        "available_at": clock.now.isoformat(),
        "state": "confirmed",
        "epistemic_status": "human_confirmed",
        "review_status": "reviewed",
        "reviewer_id": p["user_id"],
        "review_receipt_id": None,
        "sources": [],
    }
    with service.db.transaction() as s:
        for lead in service.scoped(s, "leads", p):
            update(
                lead,
                status="eligible" if lead.id == call["lead_id"] else "needs_correction",
                next_attempt_at=clock.now.isoformat(),
            )
        memory = service._by_key(s, p, "conversation_states", call["id"])
        values = {"call_id": call["id"], "schema_version": 2, "observations": [observation]}
        if memory:
            update(memory, **values)
        else:
            put(s, "conversation_states", p["tenant_id"], p["seller_id"], values, key=call["id"])
        lead = service.required(s, "leads", call["lead_id"], p)
        service.refresh_packet_in(s, p, lead)
    clock.now += timedelta(hours=2)
    second = start(workspace, key="prior-context-second")
    with service.db.transaction(False) as s:
        frozen = next(r.data for r in rows(s, "feature_snapshots") if r.id == second["feature_snapshot_id"])
    assert frozen["features"].get("conversation_concepts") == ["approval"]
    assert frozen["features"]["conversation_sources"][0]["observation_version"] == 1
    assert "lengthy approval" not in str(frozen)
    with service.db.transaction() as s:
        memory = service._by_key(s, p, "conversation_states", call["id"])
        update(
            memory,
            observations=[
                {**observation, "state": "retracted", "invalidated_at": clock.now.isoformat(), "version": 2}
            ],
        )
        lead = service.required(s, "leads", call["lead_id"], p)
        service.refresh_packet_in(s, p, lead)
        current = service.prediction_features(s, p, lead, calls=[])
        assert current["conversation_concepts"] == []
        assert (
            next(r.data for r in rows(s, "feature_snapshots") if r.id == second["feature_snapshot_id"])
            == frozen
        )
