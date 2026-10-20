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
        # The packet refresh job (refresh_continuity) recomputes features off the save path.
        service.refresh_conversation_features_in(s, p, lead)
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
        service.refresh_conversation_features_in(s, p, lead)
        from app.db import doc
        from app.optimization.learning import freeze_features

        current = freeze_features(doc(lead), health={"score": None, "samples": 0}, now=clock())
        assert current["conversation_concepts"] == []
        assert (
            next(r.data for r in rows(s, "feature_snapshots") if r.id == second["feature_snapshot_id"])
            == frozen
        )


def test_brief_exposes_conditional_support_and_actual_prior_policy(workspace, app):
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        lead = service.scoped(s, "leads", p)[0]
        config = service.optimization_config(s, p)
    brief = service.packet(p, lead.id)["statistical_guidance"]
    assert brief["prior_only"] is True
    assert brief["estimate_policy"] == config["estimate_policy"]
    assert brief["work_assumption_seconds"] == config["handling_seconds"]
    assert brief["stage_support"]["right_party"]["cell_known"] == 0
    assert brief["stage_support"]["conversion"]["observed_interval"] == [0, 1]


def test_active_packet_uses_frozen_selection_prediction_and_policy(workspace, app, clock):
    from app.db import get, update
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        snapshot = get(s, "feature_snapshots", call["feature_snapshot_id"])
        prediction = {
            **snapshot.data["predictions"],
            "p_human": 0.87,
            "model_version": "owned-reviewed-model",
        }
        policy = service.optimization_config(s, p)["estimate_policy"]
        update(snapshot, predictions=prediction)
        assert snapshot.data.get("estimate_policy") == policy, "Freeze the actual estimation assumptions too"
        campaign = get(s, "campaigns", workspace["campaign_id"])
        config = service.optimization_config(s, p)
        update(campaign, optimization={**config, "conversion_goal": "sale_won", "handling_seconds": 600})
    clock.now += timedelta(minutes=3)
    brief = service.packet(p, call["lead_id"])["statistical_guidance"]
    assert brief["human_probability"] == 0.87
    assert brief["model_version"] == "owned-reviewed-model"
    assert brief["estimate_basis"] == "reviewed_model"
    assert "pooled comparison" in brief["uncertainty"]
    assert brief["estimate_policy"] == policy
    assert brief["conversion_goal"] == "meeting_held"
    assert brief["work_assumption_seconds"] == 120
    assert brief["source"] == "frozen_call_decision"
    assert brief["decision_id"] == call["feature_snapshot_id"]
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    next_brief = service.packet(p, call["lead_id"])["statistical_guidance"]
    assert next_brief["source"] == "current_pooled_estimate"
    assert next_brief["conversion_goal"] == "sale_won"


def test_stale_saved_estimate_does_not_claim_historical_as_of_time(workspace, app):
    from app.db import get, update
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        campaign = get(s, "campaigns", workspace["campaign_id"])
        update(
            campaign,
            selection_profile={
                "goal": "meeting_held",
                "as_of": "2020-01-01T00:00:00Z",
                "estimate_policy_version": "retired",
            },
        )
    brief = service.packet(p, lead.id)["statistical_guidance"]
    assert brief["prior_only"]
    assert brief["as_of"] is None


def test_reserved_packet_uses_only_current_owned_decision(workspace, app, clock):
    from app.db import get, update
    from conftest import reserve
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    result = reserve(workspace)
    assert result.status_code == 200, result.text
    selected = result.json()
    with service.db.transaction() as s:
        reservation = get(s, "reservations", selected["reservation_id"])
        snapshot = get(s, "feature_snapshots", reservation.data["feature_snapshot_id"])
        update(snapshot, predictions={**snapshot.data["predictions"], "p_human": 0.73})
    stats = service.packet(p, selected["lead"]["id"])["statistical_guidance"]
    assert stats["human_probability"] == 0.73
    assert stats["source"] == "frozen_reservation_decision"
    clock.now += timedelta(minutes=3)
    expired = service.packet(p, selected["lead"]["id"])["statistical_guidance"]
    assert expired["source"] == "current_pooled_estimate"
    assert expired["human_probability"] != 0.73
