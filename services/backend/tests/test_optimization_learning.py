from datetime import timedelta

import pytest
from app.optimization.learning import (
    conversion_score,
    derive_labels,
    drift_report,
    freeze_features,
    future_slots,
    learn_distribution,
    training_rows,
    trial_report,
)


@pytest.mark.parametrize(
    "outcome,human,right,qualified",
    [
        ("wrong_party", True, False, False),
        ("gatekeeper", True, False, False),
        ("screening", False, False, False),
        ("ivr", False, False, False),
        ("voicemail", False, False, False),
        ("no_answer", False, False, False),
        ("opt_out", True, False, False),
        ("not_interested", True, False, False),
        ("right_party", True, True, False),
        ("qualified_conversation", True, True, True),
        ("meeting_held", True, True, True),
        ("sale_won", True, True, True),
    ],
)
def test_labels_keep_the_funnel_distinct(outcome, human, right, qualified):
    result = derive_labels(outcome)
    assert (result["human"], result["right_party"], result["qualified"]) == (human, right, qualified)


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
    rows = [
        {"action": "a", "conversion": 0, "window_status": "matured", "segment": "roofing"} for _ in range(30)
    ]
    rows += [
        {"action": "b", "conversion": 1, "window_status": "matured", "segment": "roofing"} for _ in range(30)
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


def test_future_slots_respect_timezone_retry_callback_and_coverage(clock):
    callback = clock.now + timedelta(hours=2)
    lead = {
        "id": "l",
        "timezone": "America/Los_Angeles",
        "segment": "roofing",
        "attempt_count": 1,
        "callback_due_at": callback.isoformat(),
        "next_attempt_at": (clock.now + timedelta(hours=1)).isoformat(),
        "coverage_due_at": (clock.now + timedelta(days=1)).isoformat(),
    }
    slots = future_slots(
        lead,
        [],
        now=clock.now,
        policy={"local_start_hour": 9, "local_end_hour": 17, "min_retry_seconds": 3600, "max_attempts": 4},
    )
    assert slots[0]["at"] == callback.isoformat() and slots[0]["reason"] == "requested_callback"
    assert all(s["local_hour"] in range(9, 17) for s in slots)
    assert all(s["at"] >= lead["next_attempt_at"] for s in slots)


def test_no_timezone_and_exhausted_leads_are_not_scheduled(clock):
    assert future_slots({"id": "l", "timezone": None}, [], now=clock.now, policy={}) == []
    assert (
        future_slots(
            {"id": "l", "timezone": "UTC", "attempt_count": 4}, [], now=clock.now, policy={"max_attempts": 4}
        )
        == []
    )


def test_a_possible_coverage_deadline_is_not_replaced_with_a_later_slot(clock):
    deadline = clock.now + timedelta(hours=2)
    slots = future_slots(
        {"id": "covered", "timezone": "America/Los_Angeles", "coverage_due_at": deadline.isoformat()},
        [],
        now=clock.now,
        policy={"local_start_hour": 9, "local_end_hour": 20},
    )
    assert slots and slots[0]["at"] <= deadline.isoformat()


def test_future_slots_can_use_the_verified_active_model_instead_of_only_the_pooled_baseline(clock):
    slots = future_slots(
        {"id": "active-model", "timezone": "America/Los_Angeles"},
        [],
        now=clock.now,
        policy={"local_start_hour": 9, "local_end_hour": 17},
        predictor=lambda hour, weekday: {
            "p_human": 0.9 if hour == 16 else 0.1,
            "p_right_party_given_human": 0.8,
            "p_conversion_given_right_party": 0.4,
            "expected_rep_seconds": 60,
        },
    )
    assert slots[0]["local_hour"] == 16


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
    events = [
        {"lead_id": "2", "kind": "meeting_held", "occurred_at": clock.now.isoformat()},
        {"lead_id": "2", "kind": "meeting_held", "occurred_at": clock.now.isoformat()},
    ]
    report = trial_report(assigned, events, now=clock.now, goal="meeting_held", window_days=30)
    assert report["arms"]["control"]["leads"] == 2 and report["arms"]["candidate"]["conversions"] == 1
    assert not report["sufficient_evidence"]
    assert report["pending_leads"] > 0 and report["required_per_arm"] > 2
    assert report["evidence_scope"] == "observed controlled cohort; no automatic causal guarantee"


def test_drift_report_exposes_changed_features_without_automatic_activation():
    reference = [{"features": {"local_hour": 10}, "human": 1, "p_human": 0.8} for _ in range(40)]
    recent = [{"features": {"local_hour": 23}, "human": 0, "p_human": 0.8} for _ in range(40)]
    report = drift_report(reference, recent)
    assert report["drift_detected"] and report["recent_brier"] > report["reference_brier"]
    assert report["action"] == "review_or_restore_baseline"
