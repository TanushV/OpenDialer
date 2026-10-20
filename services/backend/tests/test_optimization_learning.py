from datetime import timedelta

import pytest
from app.optimization.learning import (
    conversion_score,
    derive_labels,
    freeze_features,
    training_rows,
)
from comparison_oracle import drift_report, learn_distribution, trial_report


@pytest.mark.parametrize(
    "outcome,human,right,qualified",
    [
        ("wrong_party", True, False, None),
        ("gatekeeper", True, False, None),
        ("screening", False, None, None),
        ("ivr", False, None, None),
        ("voicemail", False, None, None),
        ("no_answer", False, None, None),
        ("opt_out", True, None, None),
        ("not_interested", True, None, None),
        ("right_party", True, True, None),
        ("qualified_conversation", True, True, True),
        ("meeting_held", True, None, None),
        ("sale_won", True, None, None),
    ],
)
def test_labels_keep_the_funnel_distinct(outcome, human, right, qualified):
    # 20.2: a rep-confirmed source establishes the right party; other goals stay pending (None).
    result = derive_labels({"outcome": outcome, "outcome_source": "rep_reported"})
    assert (result["human"], result["right_party"], result["qualified"]) == (human, right, qualified)


@pytest.mark.parametrize(
    "outcome,human,right",
    [
        (None, None, None),
        ("temporary_failure", None, None),
        ("invalid_endpoint", None, None),
        ("callback_requested", True, None),
        ("opt_out", True, None),
        ("not_interested", True, None),
        ("no_answer", False, None),
        ("busy", False, None),
        ("voicemail", False, None),
        ("screening", False, None),
        ("gatekeeper", True, False),
        ("right_party", True, True),
    ],
)
def test_nullable_labels_and_pooled_counts(outcome, human, right):
    """C030: an unobserved conditional result is not a negative example."""
    labels = derive_labels({"outcome": outcome, "outcome_source": "rep_reported"})
    assert labels["human"] is human
    assert labels["right_party"] is right


def test_sparse_prior_excludes_cell_and_uses_circular_hour():
    from app.scheduling.models import pooled_predictions

    empty = pooled_predictions([], segment="a", hour=0)
    assert empty["p_human"] == pytest.approx(0.2)
    assert empty["expected_rep_seconds"] == 120
    rows = [
        {"outcome": "right_party", "segment": "a", "local_hour": 23, "duration_seconds": 80},
        {"outcome": "no_answer", "segment": "b", "local_hour": 10, "duration_seconds": 40},
        {"outcome": "temporary_failure", "segment": "a", "local_hour": 0},
    ]
    predicted = pooled_predictions(rows, segment="a", hour=0)
    # Exclude the matching cell from the pooled prior: outside 0/1 -> (0+2)/(1+10).
    assert predicted["p_human"] == pytest.approx((1 + 10 * (2 / 11)) / 11)
    assert predicted["observations"] == 1
    assert predicted["expected_rep_seconds"] == pytest.approx((80 + 5 * 120) / 6)


def test_missing_historical_time_falls_back_to_the_known_segment():
    from app.scheduling.models import pooled_predictions

    rows = [{"segment": "a", "outcome": "right_party"}] * 3 + [{"segment": "b", "outcome": "no_answer"}] * 5
    value = pooled_predictions(
        rows, features={"segment": "a", "local_hour": 9, "local_weekday": 1, "attempt_count": 0}
    )
    assert value["estimate_level"] == "segment"
    assert value["stage_support"]["human"]["cell_known"] == 3
    assert value["p_human"] == pytest.approx((3 + 10 * (2 / 15)) / 13)


def test_finite_large_prior_parameters_cannot_overflow_a_prediction():
    import math

    from app.scheduling.models import EstimatePolicy, pooled_predictions

    policy = EstimatePolicy(
        human_prior=(1e308, 1e308),
        right_prior=(1e308, 1e308),
        goal_prior=(1e308, 1e308),
        work_prior=1e308,
        shrinkage=1e308,
    )
    value = pooled_predictions([], estimate_policy=policy)
    for key in ("p_human", "p_right_party_given_human", "p_conversion_given_right_party"):
        assert math.isfinite(value[key]) and value[key] == pytest.approx(0.5)
    assert value["expected_rep_seconds"] == pytest.approx(120)


@pytest.mark.parametrize("outcome", ["callback_requested", "meeting_booked", "meeting_held", "sale_won"])
def test_ambiguous_import_does_not_invent_target_contact(outcome):
    labels = derive_labels({"outcome": outcome, "outcome_source": "imported", "contact_role": "unknown"})
    assert labels["human"] is True and labels["right_party"] is None
    confirmed = derive_labels(
        {"outcome": outcome, "outcome_source": "rep_reported", "contact_role": "right_party"}
    )
    assert confirmed["right_party"] is True
    provisional = derive_labels({"outcome": outcome, "outcome_source": "llm_provisional"})
    assert provisional["human"] is None and provisional["right_party"] is None


def test_one_goal_evidence_cannot_prove_another_goal():
    # Other goals remain pending until their own evidence or the window closes.
    assert derive_labels("sale_won")["meeting_held"] is None
    assert derive_labels("meeting_booked")["qualified"] is None
    assert derive_labels("meeting_held")["qualified"] is None


def test_conditional_estimates_use_only_known_conditioning_labels_and_no_double_prior():
    from app.optimization.learning import funnel_predictions

    rows = [
        {
            "segment": "a",
            "local_hour": 23,
            "local_weekday": 1,
            "human": True,
            "right_party": True,
            "conversion": 1,
            "window_status": "resolved",
        },
        {
            "segment": "a",
            "local_hour": 0,
            "local_weekday": 2,
            "human": True,
            "right_party": None,
            "conversion": None,
        },
        {
            "segment": "b",
            "local_hour": 10,
            "local_weekday": 2,
            "human": True,
            "right_party": False,
            "conversion": None,
        },
        {
            "segment": "b",
            "local_hour": 10,
            "local_weekday": 2,
            "human": False,
            "right_party": None,
            "conversion": None,
        },
        {
            "segment": "a",
            "local_hour": 0,
            "local_weekday": 2,
            "human": None,
            "right_party": None,
            "conversion": None,
        },
    ]
    result = funnel_predictions(rows, {"segment": "a", "local_hour": 0, "local_weekday": 2})
    assert result["p_human"] == pytest.approx((2 + 10 * (3 / 12)) / 12)
    assert result["p_right_party_given_human"] == pytest.approx((1 + 10 * (1 / 4)) / 11)
    assert result["p_conversion_given_right_party"] == pytest.approx((1 + 10 * (1 / 4)) / 11)
    assert result["stage_support"]["human"]["cell_known"] == 2
    assert result["stage_support"]["right_party"]["cell_known"] == 1
    assert result["stage_support"]["conversion"]["cell_known"] == 1
    assert result["stage_support"]["human"]["cell_unknown"] == 1
    assert result["expected_attempt_cost_usd"] is None


def test_prior_intervals_stay_unknown_and_custom_work_prior_is_visible():
    from app.optimization.learning import funnel_predictions

    value = funnel_predictions(
        [],
        {"segment": "unknown", "local_hour": None},
        handling_seconds=180,
        estimate_policy={"human_prior": [1, 1], "work_prior": 2},
    )
    assert value["p_human"] == 0.5 and value["expected_rep_seconds"] == 180
    assert value["estimate_level"] == "pooled"
    assert value["stage_support"]["human"]["observed_interval"] == [0, 1]
    assert value["prior_only"] is True
    assert value["estimate_policy_version"]


def test_attempt_work_unions_phases_and_excludes_detached_playback(clock):
    from app.scheduling.models import attempt_work_seconds

    at = clock.now

    def stamp(sec):
        return (at + timedelta(seconds=sec)).isoformat()

    call = {
        "submitted_at": stamp(0),
        "ended_at": stamp(120),
        "detached": True,
        "rep_released_at": stamp(40),
        "rep_work_intervals": [
            {"started_at": stamp(35), "ended_at": stamp(50)},
            {"started_at": stamp(45), "ended_at": stamp(55)},
        ],
    }
    assert attempt_work_seconds(call) == 55
    assert attempt_work_seconds({"detached": True, "duration_seconds": 120}) is None
    assert attempt_work_seconds({"duration_seconds": float("nan")}) is None


def test_estimate_policy_is_validated_and_returned_by_the_single_settings_owner(workspace):
    c, h = workspace["client"], workspace["headers"]
    value = c.get("/v1/optimization", headers=h).json()["settings"]
    assert value["estimate_policy"]["human_prior"] == [2, 8]
    assert (
        c.put("/v1/admin/optimization", headers=h, json={"estimate_policy": {"work_prior": 3}}).status_code
        == 200
    )
    value = c.get("/v1/optimization", headers=h).json()["settings"]
    assert value["estimate_policy"]["work_prior"] == 3
    assert (
        c.put(
            "/v1/admin/optimization", headers=h, json={"estimate_policy": {"human_prior": [0, 8]}}
        ).status_code
        == 422
    )


def test_sparse_cells_keep_weekend_attempt_and_missing_labels_distinct():
    from app.optimization.learning import funnel_predictions

    rows = [
        {"segment": "a", "local_hour": 23, "local_weekday": 5, "attempt_count": 1, "outcome": "right_party"},
        {"segment": "a", "local_hour": 0, "local_weekday": 1, "attempt_count": 1, "outcome": "no_answer"},
        {"segment": "a", "local_hour": 0, "local_weekday": 6, "attempt_count": 2, "outcome": "no_answer"},
        {
            "segment": "a",
            "local_hour": 0,
            "local_weekday": 6,
            "attempt_count": 1,
            "outcome": "right_party",
            "outcome_source": "llm_provisional",
            "human": True,
        },
    ]
    result = funnel_predictions(
        rows, {"segment": "a", "local_hour": 0, "local_weekday": 6, "attempt_count": 1}
    )
    assert result["p_human"] == pytest.approx((1 + 10 * (2 / 12)) / 11)
    assert result["stage_support"]["human"]["cell_unknown"] == 1
    assert result["estimate_level"] == "segment_time_attempt"


def test_pending_conversion_and_unsettled_cost_are_never_observed_rewards():
    from app.optimization.learning import funnel_predictions

    result = funnel_predictions(
        [
            {
                "human": True,
                "right_party": True,
                "conversion": 0,
                "window_status": "pending",
                "observed_cost_usd": 0.5,
                "cost_status": "uncertain",
            },
            {
                "human": True,
                "right_party": True,
                "conversion": 1,
                "window_status": "resolved",
                "observed_cost_usd": 0.2,
                "cost_status": "settled",
            },
        ],
        {},
    )
    assert result["conversion_observations"] == 1
    assert result["expected_attempt_cost_usd"] == 0.2
    assert result["cost_unknown_observations"] == 1


def test_attempt_work_preserves_asof_contact_role_and_missing_time(clock):
    at = clock.now - timedelta(days=20)
    call = {
        "id": "c",
        "lead_id": "l",
        "started_at": at.isoformat(),
        "feature_snapshot_id": "f",
        "outcome": "callback_requested",
        "contact_role": "right_party",
        "outcome_source": "rep_reported",
        "outcome_available_at": (at + timedelta(minutes=2)).isoformat(),
    }
    snap = {
        "id": "f",
        "as_of": at.isoformat(),
        "features": {"local_hour": 0, "local_weekday": 6, "attempt_count": 1},
    }
    row = training_rows([call], [snap], [], now=clock.now, goal="meeting_held", window_days=14)[0]
    assert row["right_party"] is True
    assert row["rep_work_seconds"] is None
    assert row["local_weekday"] == 6 and row["attempt_count"] == 1


def test_live_decision_uses_saved_cold_assumptions(workspace, app):
    from conftest import start

    c, h = workspace["client"], workspace["headers"]
    assert (
        c.put(
            "/v1/admin/optimization",
            headers=h,
            json={"handling_seconds": 180, "estimate_policy": {"human_prior": [1, 1]}},
        ).status_code
        == 200
    )
    # A subsequent partial policy edit preserves the explicitly saved prior.
    assert (
        c.put("/v1/admin/optimization", headers=h, json={"estimate_policy": {"work_prior": 3}}).status_code
        == 200
    )
    call = start(workspace)
    from app.db import rows

    with app.state.db.transaction(False) as s:
        frozen = next(r.data for r in rows(s, "feature_snapshots") if r.id == call["feature_snapshot_id"])
    assert frozen["predictions"]["p_human"] == 0.5
    assert frozen["predictions"]["expected_rep_seconds"] == 180


def test_dataset_cost_waits_for_complete_attributed_receipts(workspace, app):
    from app.db import put, rows, update
    from conftest import disposition_body, start

    c, h = workspace["client"], workspace["headers"]
    call = start(workspace)
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="right_party"),
    )
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    service = app.state.service
    with app.state.db.transaction() as s:
        put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "kind": "voice",
                "status": "settled",
                "cost_usd": 0.2,
                "credited_usd": 0.05,
                "provider_record_id": "synthetic-cost",
            },
        )
    with app.state.db.transaction(False) as s:
        incomplete = service.operational_dataset(s, p)[0]
    assert incomplete["observed_cost_usd"] is None and incomplete["cost_status"] == "uncertain"
    with app.state.db.transaction() as s:
        hold = next(r for r in rows(s, "usage_ledger") if r.external_key == "call:" + call["id"])
        update(
            hold,
            status="settled",
            cost_usd=0,
            reserved_usd=0,
            billing_verified=True,
            reconciled_to_provider_records=True,
        )
    with app.state.db.transaction(False) as s:
        complete = service.operational_dataset(s, p)[0]
    assert complete["observed_cost_usd"] == pytest.approx(0.15)
    assert complete["cost_status"] == "settled"


def test_forecast_shares_weekday_and_attempt_estimates_with_selection(workspace, app):
    from app.db import update
    from app.scheduling.models import EstimatePolicy

    service = app.state.service
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    with app.state.db.transaction() as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        leads = service.scoped(s, "leads", p)
        for lead in leads:
            update(lead, segment="fixture", attempt_count=0)
        base = {
            "p_human": 0.2,
            "p_right_party_given_human": 0.5,
            "p_conversion_given_right_party": 0.5,
            "expected_rep_seconds": 180,
        }
        update(
            campaign,
            selection_profile={
                "goal": "meeting_held",
                "window_days": 14,
                "estimate_policy_version": EstimatePolicy().version,
                "work_assumption_seconds": 120,
                "pooled": base,
                "predictions": {
                    '["fixture",10,false,0]': {**base, "p_human": 0.8},
                    '["fixture",10,true,0]': {**base, "p_human": 0.4},
                },
            },
        )
    with app.state.db.transaction(False) as s:
        inputs, _ = service.forecast_input(s, p)
    for lead in inputs["leads"]:
        assert lead["slot_estimates"]["0:10"] == pytest.approx(0.2)
        assert lead["slot_estimates"]["6:10"] == pytest.approx(0.1)
        assert lead["predicted_work_seconds"] == 180


def test_snapshot_only_has_information_known_before_the_call(clock):
    lead = {
        "segment": "roofing",
        "attempt_count": 2,
        "timezone": "America/Los_Angeles",
        "created_at": (clock.now - timedelta(days=5)).isoformat(),
        "emotion": "happy",
        "registry_listed": False,
        "outcome": "sale_won",
        "transcript": "future information",
    }
    features = freeze_features(lead, health={"score": None, "samples": 0}, now=clock.now)
    assert features["local_hour"] == 11 and features["local_weekday"] == 2
    assert features["number_health_missing"] == 1
    assert features["source_age_days"] == 5
    assert not {"emotion", "registry_listed", "outcome", "transcript"} & features.keys()


def test_dataset_uses_immutable_snapshot_and_censors_delayed_conversions(clock):
    at = clock.now - timedelta(days=2)
    calls = [
        {
            "id": "c1",
            "lead_id": "l1",
            "endpoint_id": "e1",
            "started_at": at.isoformat(),
            "state": "FINALIZED",
            "outcome": "right_party",
            "outcome_available_at": at.isoformat(),
            "feature_snapshot_id": "f1",
        }
    ]
    snapshots = [{"id": "f1", "as_of": at.isoformat(), "features": {"local_hour": 10, "attempt_count": 0}}]
    rows = training_rows(calls, snapshots, [], now=clock.now, goal="meeting_held", window_days=14)
    assert rows[0]["features"]["local_hour"] == 10
    assert rows[0]["conversion"] is None and rows[0]["window_status"] == "pending"
    mature = training_rows(
        calls, snapshots, [], now=clock.now + timedelta(days=15), goal="meeting_held", window_days=14
    )
    assert mature[0]["conversion"] == 0 and mature[0]["window_status"] == "matured"
    events = [
        {
            "id": "event1",
            "lead_id": "l1",
            "call_id": "c1",
            "kind": "meeting_held",
            "occurred_at": clock.now.isoformat(),
            "recorded_at": clock.now.isoformat(),
        }
    ]
    positive = training_rows(calls, snapshots, events, now=clock.now, goal="meeting_held", window_days=14)
    assert positive[0]["conversion"] == 1 and positive[0]["conversion_event_ids"] == ["event1"]


def test_future_features_and_events_are_rejected_from_training(clock):
    call = {
        "id": "c",
        "lead_id": "l",
        "endpoint_id": "e",
        "started_at": clock.now.isoformat(),
        "outcome": "right_party",
        "feature_snapshot_id": "f",
    }
    future = {"id": "f", "as_of": (clock.now + timedelta(days=1)).isoformat(), "features": {"local_hour": 23}}
    assert training_rows([call], [future], [], now=clock.now, goal="meeting_held", window_days=14) == []


def test_conversion_score_prefers_useful_outcomes_over_easy_low_value_pickups():
    easy = {
        "p_human": 0.9,
        "p_right_party_given_human": 0.2,
        "p_conversion_given_right_party": 0.02,
        "expected_rep_seconds": 120,
    }
    useful = {
        "p_human": 0.4,
        "p_right_party_given_human": 0.8,
        "p_conversion_given_right_party": 0.3,
        "expected_rep_seconds": 120,
    }
    assert conversion_score(useful) > conversion_score(easy)


def test_treatment_learning_changes_the_best_approved_action_and_logs_exact_distribution():
    # Rewards carry their logged propensity; a missing one is rejected, not read as certainty.
    rows = [
        {"action": "a", "conversion": 0, "window_status": "matured", "segment": "roofing", "propensity": 0.5}
        for _ in range(30)
    ]
    rows += [
        {"action": "b", "conversion": 1, "window_status": "matured", "segment": "roofing", "propensity": 0.5}
        for _ in range(30)
    ]
    result = learn_distribution(["a", "b"], rows, segment="roofing", exploration=0.1)
    assert result["best"] == "b" and result["probabilities"]["b"] == pytest.approx(0.95)
    assert sum(result["probabilities"].values()) == pytest.approx(1)
    assert set(result["probabilities"]) == {"a", "b"}
    for flag in ["control", "urgent"]:
        safe = learn_distribution(["a", "b"], rows, **{flag: True})
        assert safe["probabilities"] == {"a": 1.0, "b": 0.0}


def test_pending_outcomes_cannot_promote_a_treatment():
    rows = [{"action": "b", "conversion": 1, "window_status": "pending"} for _ in range(100)]
    assert learn_distribution(["a", "b"], rows)["best"] == "a"


def test_forecast_respects_timezone_retry_callback_and_window(clock):
    # Per-lead suggestions now come from the one whole-list schedule (no alternatives path).
    from app.optimization.numbering import capacity_forecast

    callback = clock.now + timedelta(hours=2)
    lead = {
        "id": "l",
        "status": "eligible",
        "timezone": "America/Los_Angeles",
        "segment": "roofing",
        "attempt_count": 1,
        "callback_due_at": callback.isoformat(),
        "next_attempt_at": (clock.now + timedelta(hours=1)).isoformat(),
    }
    window = {
        "start_at": clock.now.isoformat(),
        "end_at": (clock.now + timedelta(hours=8)).isoformat(),
        "reps": 1,
    }
    policy = {"local_start_hour": 9, "local_end_hour": 17, "min_retry_seconds": 3600, "max_attempts": 4}
    inventory = [{"id": "n", "region": "general", "lifecycle": "healthy", "daily_limit": 5, "capacity": 1}]
    rows = capacity_forecast([lead], inventory, now=clock.now, policy=policy, staffed_windows=[window])[
        "existing_schedule"
    ]
    assert rows[0]["at"] == callback.isoformat() and rows[0]["callback"]
    later = capacity_forecast(
        [{**lead, "callback_due_at": None}], inventory, now=clock.now, policy=policy, staffed_windows=[window]
    )["existing_schedule"]
    assert later and later[0]["at"] >= lead["next_attempt_at"]


def test_no_timezone_and_exhausted_leads_are_not_scheduled(clock):
    from app.optimization.numbering import capacity_forecast

    result = capacity_forecast(
        [{"id": "l", "timezone": None}, {"id": "x", "timezone": "UTC", "attempt_count": 4}],
        [],
        now=clock.now,
        policy={"max_attempts": 4},
    )
    assert result["known_next_attempts"] == 0 and result["window_unknown_leads"] == 1


def test_trial_reports_use_unique_matured_leads_and_do_not_claim_lift_from_tiny_samples(clock):
    assigned = [
        {
            "lead_id": str(i),
            "arm": "control" if i < 2 else "candidate",
            "assigned_at": (clock.now - timedelta(days=20)).isoformat(),
        }
        for i in range(4)
    ]
    assigned += [assigned[0]]
    known = {"recorded_at": clock.now.isoformat()}
    events = [
        {"lead_id": "2", "kind": "meeting_held", "occurred_at": clock.now.isoformat(), **known},
        {"lead_id": "2", "kind": "meeting_held", "occurred_at": clock.now.isoformat(), **known},
    ]
    report = trial_report(assigned, events, now=clock.now, goal="meeting_held", window_days=30)
    assert report["arms"]["control"]["leads"] == 2 and report["arms"]["candidate"]["conversions"] == 1
    assert not report["sufficient_evidence"]
    assert report["pending_leads"] > 0 and report["required_per_arm"] > 2
    assert report["evidence_scope"] == "observed controlled cohort; no automatic causal guarantee"


def test_drift_report_exposes_changed_features_without_automatic_activation():
    reference = [
        {"features": {"local_hour": 10}, "human": 1, "p_human": 0.8, "model_version": "m"} for _ in range(40)
    ]
    recent = [
        {"features": {"local_hour": 23}, "human": 0, "p_human": 0.8, "model_version": "m"} for _ in range(40)
    ]
    report = drift_report(reference, recent)
    assert report["drift_detected"] and report["recent_brier"] > report["reference_brier"]
    assert report["action"] == "review_or_restore_baseline"


def test_drift_separates_missingness_from_values_and_requires_comparable_model_support():
    base = {
        "model_version": "m1",
        "goal": "meeting_held",
        "human": 1,
        "p_human": 0.8,
        "right_party": 1,
        "p_right_party_given_human": 0.8,
        "conversion": 0,
        "p_conversion_given_right_party": 0.1,
        "window_closed": True,
    }
    reference = [{**base, "features": {"local_hour": 10}} for _ in range(40)]
    recent = [{**base, "features": {"local_hour": None}} for _ in range(40)]
    result = drift_report(reference, recent)
    assert result["features"]["local_hour"]["recent_mean"] is None
    assert result["features"]["local_hour"]["recent_missing"] == 40
    assert result["missingness_shift"] is True
    assert result["stage_support"]["human"]["recent"] == 40
    assert result["definition_version"] and result["model_versions"] == ["m1"]
    changed = drift_report(reference, [{**r, "model_version": "m2", "human": 0} for r in recent])
    assert changed["action"] == "insufficient_comparable_data"
    assert not changed["drift_detected"]
    sparse = drift_report(
        reference, [{**r, "human": None, "right_party": None, "conversion": None} for r in recent]
    )
    assert sparse["stage_support"]["human"]["recent"] == 0
    assert sparse["recent_brier"] is None
    assert sparse["action"] != "review_or_restore_baseline"


def test_training_rows_retain_each_actual_frozen_stage_prediction(clock):
    at = (clock() - timedelta(days=20)).isoformat()
    predictions = {
        "p_human": 0.4,
        "p_right_party_given_human": 0.3,
        "p_conversion_given_right_party": 0.2,
        "model_version": "owned-model",
    }
    rows = training_rows(
        [
            {
                "id": "call",
                "lead_id": "lead",
                "started_at": at,
                "feature_snapshot_id": "snapshot",
                "outcome": "right_party",
                "outcome_available_at": at,
            }
        ],
        [
            {
                "id": "snapshot",
                "as_of": at,
                "features": {},
                "predictions": predictions,
                "model_version": "owned-model",
            }
        ],
        [],
        now=clock(),
        goal="meeting_held",
        window_days=14,
    )
    for key, value in predictions.items():
        assert rows[0].get(key) == value


@pytest.mark.parametrize("window", [None, 30])
def test_saved_sparse_profile_requires_the_same_outcome_window(window):
    from app.scheduling.models import EstimatePolicy, pooled_predictions, saved_prediction

    policy = EstimatePolicy()
    config = {
        "conversion_goal": "meeting_held",
        "window_days": 14,
        "handling_seconds": 120,
        "estimate_policy": policy.model_dump(),
    }
    historical = {**pooled_predictions([], features={}), "p_human": 0.99}
    profile = {
        "goal": "meeting_held",
        "window_days": window,
        "estimate_policy_version": policy.version,
        "work_assumption_seconds": 120,
        "pooled": historical,
        "as_of": "2026-10-01T00:00:00Z",
    }
    value = saved_prediction(profile, {}, config)
    assert value["p_human"] == 0.2
    assert value.get("as_of") is None
