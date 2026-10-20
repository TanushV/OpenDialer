"""W5b learning acceptance: exact labels, honest propensities and proportionate learning."""

from datetime import UTC, datetime, timedelta

import pytest
from app.db import put, update
from test_remediation_privacy import principal

T0 = datetime(2026, 1, 5, 15, tzinfo=UTC)


def call(identity, at, outcome="right_party", **values):
    return {
        "id": identity,
        "lead_id": "lead",
        "started_at": at.isoformat(),
        "submitted_at": at.isoformat(),
        "feature_snapshot_id": "f-" + identity,
        "outcome": outcome,
        "outcome_source": "rep_reported",
        "outcome_available_at": (at + timedelta(minutes=5)).isoformat(),
        **values,
    }


def snapshot(identity, at):
    return {"id": "f-" + identity, "as_of": at.isoformat(), "features": {"segment": "s", "local_hour": 9}}


def rows(calls, events, now, goal="meeting_held", **kwargs):
    from app.optimization.learning import training_rows

    return training_rows(
        calls,
        [snapshot(c["id"], timestampless(c["started_at"])) for c in calls],
        events,
        now=now,
        goal=goal,
        window_days=7,
        **kwargs,
    )


def timestampless(value):
    return datetime.fromisoformat(value)


# Group 4: labels follow EXECUTION_SPEC 20.2 exactly.


@pytest.mark.parametrize("code", ["right_party", "qualified_conversation"])
def test_right_party_requires_a_rep_confirmed_source(code):
    from app.scheduling.models import derive_labels

    assert derive_labels({"outcome": code, "outcome_source": "rep_reported"})["right_party"] is True
    for source in ("legacy_unknown", "outcome_file_import", None):
        labels = derive_labels({"outcome": code, "outcome_source": source})
        assert labels["human"] is True and labels["right_party"] is None, source
    # A contact role only establishes the target when the rep confirmed it.
    role = {"outcome": "callback_requested", "contact_role": "right_party"}
    assert derive_labels({**role, "outcome_source": "rep_reported"})["right_party"] is True
    assert derive_labels({**role, "outcome_source": "outcome_file_import"})["right_party"] is None
    assert derive_labels({"outcome": "gatekeeper"})["right_party"] is False


def test_non_matching_confirmed_codes_leave_goal_labels_pending():
    from app.scheduling.models import derive_labels

    labels = derive_labels({"outcome": "right_party", "outcome_source": "rep_reported"})
    assert labels["qualified"] is None and labels["meeting_held"] is None and labels["sale_won"] is None
    held = derive_labels({"outcome": "meeting_held", "outcome_source": "rep_reported"})
    assert held["meeting_held"] is True and held["sale_won"] is None


def test_unclassified_attempts_never_mature_into_negative_conversions():
    unclassified = call("u", T0, outcome=None)
    unclassified.pop("outcome_available_at")
    data = rows([unclassified], [], T0 + timedelta(days=30), include_unclassified=True)
    assert len(data) == 1 and data[0]["conversion"] is None


def test_lead_level_conversion_credits_exactly_one_call():
    early, late = call("early", T0), call("late", T0 + timedelta(days=2))
    event = {
        "id": "event",
        "lead_id": "lead",
        "call_id": None,
        "kind": "meeting_held",
        "occurred_at": (T0 + timedelta(days=3)).isoformat(),
        "recorded_at": (T0 + timedelta(days=3)).isoformat(),
    }
    data = {r["call_id"]: r for r in rows([early, late], [event], T0 + timedelta(days=20))}
    assert data["late"]["conversion"] == 1
    assert data["early"]["conversion"] is None, "Ambiguous attribution is not credit or a negative"
    assert data["early"]["conversion_event_ids"] == []


def test_estimate_reports_the_level_actually_used():
    from app.scheduling.models import pooled_predictions

    history = [
        {"segment": "s", "local_hour": 9, "local_weekday": 0, "attempt_count": 0, "human": 1}
        for _ in range(5)
    ]
    empty = pooled_predictions(
        history, features={"segment": "s", "local_hour": 15, "local_weekday": 0, "attempt_count": 0}
    )
    assert empty["estimate_level"] == "pooled"
    supported = pooled_predictions(
        history, features={"segment": "s", "local_hour": 9, "local_weekday": 0, "attempt_count": 0}
    )
    assert supported["estimate_level"] == "segment_time_attempt"


def test_conversion_score_defaults_come_from_the_estimate_policy():
    from app.optimization.learning import conversion_score
    from app.scheduling.models import EstimatePolicy

    policy = EstimatePolicy()
    means = [a / (a + b) for a, b in (policy.human_prior, policy.right_prior, policy.goal_prior)]
    assert conversion_score({}) == pytest.approx(means[0] * means[1] * means[2] / 120)
    custom = EstimatePolicy(goal_prior=(1, 1))
    assert conversion_score({}, estimate_policy=custom) == pytest.approx(means[0] * means[1] * 0.5 / 120)


def test_trial_values_use_availability_and_retraction_time():
    from app.optimization.learning import trial_values

    assignment = {"lead_id": "lead", "arm": "candidate", "assigned_at": T0.isoformat()}
    now = T0 + timedelta(days=20)
    base = {
        "id": "e",
        "lead_id": "lead",
        "kind": "meeting_held",
        "occurred_at": (T0 + timedelta(days=1)).isoformat(),
    }
    future = {**base, "recorded_at": (now + timedelta(days=1)).isoformat()}
    assert (
        trial_values(assignment, [future], now=now, goal="meeting_held", window_days=7)[
            "candidate:conversions"
        ]
        == 0
    )
    later_retraction = {
        **base,
        "recorded_at": (T0 + timedelta(days=1)).isoformat(),
        "retracted": True,
        "retracted_at": (now + timedelta(days=1)).isoformat(),
    }
    assert (
        trial_values(assignment, [later_retraction], now=now, goal="meeting_held", window_days=7)[
            "candidate:conversions"
        ]
        == 1
    )


def test_missing_availability_times_are_excluded_not_set_to_the_cutoff():
    from app.optimization.learning import conversion_at, outcome_at

    assert outcome_at({"outcome": "right_party", "outcome_source": "rep_reported"}, T0) is None
    event = {"id": "e", "lead_id": "l", "kind": "meeting_held", "occurred_at": T0.isoformat()}
    assert conversion_at(event, T0 + timedelta(days=1)) is None
    unknown = call("u", T0)
    unknown.pop("outcome_available_at")
    assert rows([unknown], [], T0 + timedelta(days=30)) == []


# Group 5: irreversible assignment records carry true probabilities.


def comparison(service, p, campaign, **values):
    with service.db.transaction() as s:
        row = service.required(s, "campaigns", campaign, p)
        study = {
            "id": "study",
            "family": "coaching",
            "population": [lead.id for lead in service.scoped(s, "leads", p)],
            "starts_at": service.clock().isoformat(),
            "ends_at": (service.clock() + timedelta(days=7)).isoformat(),
            "goal": "meeting_held",
            "window_days": 14,
            **values,
        }
        update(row, controlled_comparisons=[study])
        return study


def test_coaching_block_positions_record_the_marginal_assignment_probability(workspace, app):
    service, p = app.state.service, principal(workspace)
    comparison(service, p, workspace["campaign_id"])
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        first, second = service.scoped(s, "leads", p)[:2]
        a = service.comparison_assignment(s, p, campaign, first, "coaching", rep_id=p["user_id"])
        s.flush()
        b = service.comparison_assignment(s, p, campaign, second, "coaching", rep_id=p["user_id"])
    assert {a["arm"], b["arm"]} == {"control", "candidate"}
    assert a["propensity"] == b["propensity"] == 0.5


def test_stale_adaptive_policy_holds_the_last_approved_probabilities(workspace, app):
    service, p = app.state.service, principal(workspace)
    approved = {"probabilities": {"control": 0.3, "approach": 0.7}}
    comparison(
        service,
        p,
        workspace["campaign_id"],
        strategy_ids=["control", "approach"],
        strategy_policy="adaptive",
        initial_strategy_policy=approved,
        report_key="missing-report",
        candidate_probability=0.5,
    )
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        lead = service.scoped(s, "leads", p)[0]
        choice = service.comparison_assignment(s, p, campaign, lead, "coaching", rep_id=p["user_id"])
    assert choice["strategy_probabilities"] == approved["probabilities"]
    assert choice["propensity"] == approved["probabilities"][choice["strategy_id"]]


def test_zero_or_missing_propensity_is_rejected_not_coerced():
    from app.optimization.experiments import reward_values
    from app.optimization.learning import reward_counts

    matured = {"window_status": "matured", "conversion": 1, "action": "a"}
    for propensity in (0, None, -1, 2):
        counts = reward_counts([{**matured, "propensity": propensity}])
        assert "reward:a:weight" not in counts and counts["reward:a:rejected_propensity"] == 1
        assert "reward:a:weight" not in reward_values({**matured, "propensity": propensity})
    assert reward_counts([{**matured, "propensity": 0.5}])["reward:a:weight"] == 2


def test_commit_time_treatment_is_the_protected_default_without_a_registered_comparison(
    workspace, app, monkeypatch
):
    import secrets

    from conftest import reserve

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for name in ("a-default", "b-variant"):
            put(
                s,
                "treatment_variants",
                p["tenant_id"],
                p["seller_id"],
                {"approved": True, "campaign_id": workspace["campaign_id"], "name": name},
                now=service.clock(),
            )
        actions = sorted(r.id for r in service.scoped(s, "treatment_variants", p))
    monkeypatch.setattr(secrets, "randbelow", lambda n: n - 1)
    response = reserve(workspace)
    assert response.status_code == 200, response.text
    with service.db.transaction(False) as s:
        decision = service.scoped(s, "policy_decisions", p)[-1].data
    assert decision["propensity"] == 1.0
    assert decision["action_distribution"][decision["action"]] == 1.0
    assert sorted(decision["eligible_actions"]) == actions
    assert decision["treatment_reason"] == "no_registered_treatment_comparison"


def test_active_model_is_not_withheld_from_half_of_leads_outside_a_trial(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {"active": True, "goal": "meeting_held"},
            now=service.clock(),
        )
    monkeypatch.setattr(service, "model_source_current", lambda *args: True)
    with service.db.transaction(False) as s:
        config = service.optimization_config(s, p)
        held = sum(service.serving_model(s, p, config, f"lead-{i}") is None for i in range(2000)) / 2000
    assert abs(held - 0.10) < 0.025, f"{held:.0%} of leads were held out from the promoted model"


def test_trials_use_the_declared_allocation_and_production_keeps_its_holdout(workspace, app, clock):
    from app.db import uid
    from app.optimization import learning

    assert learning.TRIAL_CONTROL_FRACTION == 0.5 and learning.PRODUCTION_HOLDOUT_FRACTION == 0.10
    assert not hasattr(learning, "CONTROL_FRACTION")
    ids = [f"lead-{i}" for i in range(4000)]
    assert abs(sum(map(learning.stable_control, ids)) / 4000 - 0.10) < 0.02
    trial = [learning.stable_control(i, learning.TRIAL_CONTROL_FRACTION) for i in ids]
    assert abs(sum(trial) / 4000 - 0.5) < 0.03
    assert all(t for i, t in zip(ids, trial, strict=True) if learning.stable_control(i)), "Holdout nests"
    rows = [
        {"id": "control", "arm": "control", "p_human": 0.2, "score": 0.001},
        {"id": "candidate", "arm": "candidate", "p_human": 0.9, "score": 0.1},
    ]
    choice = learning.choose_cohort_candidate(rows, now=clock.now)
    assert choice["lead_selection_probability"] in {0.1, 0.9}
    assert choice == learning.choose_cohort_candidate(rows, now=clock.now), "Hash-based and reproducible"
    declared = learning.choose_cohort_candidate(
        rows, now=clock.now, control_fraction=learning.TRIAL_CONTROL_FRACTION
    )
    assert declared["lead_selection_probability"] == 0.5
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        epoch = service.ensure_experiment_cohort(s, p)
        production = [
            r.data for r in service.scoped(s, "experiment_assignments", p) if r.data["epoch"] == epoch
        ]
    assert production
    for row in production:
        assert row["arm"] == ("control" if learning.stable_control(row["lead_id"]) else "candidate")
        assert row["propensity"] == (0.1 if row["arm"] == "control" else 0.9)
        assert row["allocation"] == {"control": 0.1, "candidate": 0.9}
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            campaign,
            optimization_epoch=uid(),
            optimization_trial={
                "model_id": "fixture",
                "ends_at": (clock.now + timedelta(days=7)).isoformat(),
                "max_leads": 100,
                "control_fraction": learning.TRIAL_CONTROL_FRACTION,
            },
        )
        epoch = service.ensure_experiment_cohort(s, p)
        rows = [r.data for r in service.scoped(s, "experiment_assignments", p) if r.data["epoch"] == epoch]
    assert rows and all(r["propensity"] == 0.5 and r["allocation"]["control"] == 0.5 for r in rows)


def test_operator_capacity_override_uses_the_settings_bound(workspace, app):
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    with pytest.raises(HTTPException) as error:
        service.proposal(p, workspace["campaign_id"], capacity=5000)
    assert error.value.status_code == 422
    assert service.proposal(p, workspace["campaign_id"], capacity=1000)["projection"]


# Groups 6 and 7: one owner for policies/defaults; proportionate training.


def test_health_policy_and_defaults_have_one_owner(workspace, app, monkeypatch):
    import inspect

    import app.number_operations as operations
    from app.optimization import api, numbering, workflows
    from app.scheduling import models

    assert api.HealthPolicy is numbering.HealthPolicy
    assert workflows.DEFAULTS is models.OPTIMIZATION_DEFAULTS
    signature = inspect.signature(numbering.capacity_forecast).parameters
    for key in ("horizon_hours", "rep_minutes", "handling_seconds", "daily_capacity_per_number"):
        assert signature[key].default == models.OPTIMIZATION_DEFAULTS[key]
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            campaign,
            optimization={**campaign.data.get("optimization", {}), "health_policy": {"min_comparable": 50}},
        )
    seen, assess = [], operations.assess_number_health

    def captured(observed, baseline, external, policy):
        seen.append(policy)
        return assess(observed, baseline, external, policy)

    monkeypatch.setattr(operations, "assess_number_health", captured)
    with service.db.transaction(False) as s:
        number = service.scoped(s, "number_inventory", p)[0]
        service.number_readiness_in(s, p, number)
    assert seen and seen[-1]["min_comparable"] == 50


def test_training_child_is_not_started_below_the_review_support(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    # 120 classified rows but only three decision times: no calibration/evaluation partition
    # can hold ten examples of each class, so the bounded child would only report "pooled".
    data = [
        {
            "lead_id": str(i),
            "at": (at + timedelta(days=i % 3)).isoformat(),
            "outcome_available_at": (at + timedelta(days=i % 3, minutes=1)).isoformat(),
            "human": i % 2,
            "features": {"local_hour": 9},
        }
        for i in range(120)
    ]
    monkeypatch.setattr(
        service,
        "save_operational_dataset",
        lambda p: {
            "id": "tiny",
            "rows": data,
            "goal": "meeting_held",
            "checksum": "c",
            "baseline": {},
            "window_days": 14,
        },
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("A training child was queued for unreviewable data")

    monkeypatch.setattr(service.jobs, "enqueue", forbidden)
    result = service.train_operational(p)
    assert result["status"] == "insufficient_data"
    assert result["reason"].startswith("No funnel stage")


def test_test_only_learning_helpers_are_not_application_code():
    from app.ml import training
    from app.optimization import experiments, learning
    from app.scheduling import policy

    for module, name in (
        (learning, "drift_report"),
        (learning, "trial_report"),
        (learning, "learn_distribution"),
        (training, "independent_partitions"),
        (training, "predict_challenger"),
        (policy, "choose_action"),
    ):
        assert not hasattr(module, name), name
    # One IPW weighting definition serves projections and comparison rewards.
    reward = {"window_status": "matured", "conversion": 1, "action": "a", "propensity": 0.25}
    assert experiments.reward_values(reward) == learning.reward_counts([reward])
