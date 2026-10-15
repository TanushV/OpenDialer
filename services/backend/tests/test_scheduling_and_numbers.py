from datetime import timedelta

import pytest
from app.ml.training import promotion_allowed, train_challenger
from app.scheduling.policy import choose_action, delayed_reward, eligible_features, rank_candidates
from conftest import start


def test_callback_and_coverage_beat_propensity(clock):
    candidates = [
        {"id": "easy", "propensity": 0.9, "coverage_due_at": clock.now + timedelta(days=2)},
        {"id": "overdue", "propensity": 0.01, "coverage_due_at": clock.now - timedelta(days=1)},
        {
            "id": "callback",
            "propensity": 0.1,
            "callback_due_at": clock.now - timedelta(minutes=1),
            "coverage_due_at": clock.now + timedelta(days=3),
        },
    ]
    assert [x["id"] for x in rank_candidates(candidates, clock.now)] == ["callback", "overdue", "easy"]


def test_exploration_logs_actual_propensity_and_never_invents_actions():
    actions = ["approved-a", "approved-b", "approved-c"]
    selected = choose_action(actions, best="approved-a", exploration=0.1, random_value=0.95)
    assert selected["action"] in actions
    assert selected["propensity"] == pytest.approx(0.9 + 0.1 / 3)
    selected = choose_action(
        actions, best="approved-a", exploration=0.1, random_value=0.05, exploration_index=1
    )
    assert selected["action"] == "approved-b"
    assert selected["propensity"] == pytest.approx(0.1 / 3)


def test_pre_call_features_strip_protected_future_and_registry_data():
    features = eligible_features(
        {
            "segment": "roofing",
            "attempt_count": 1,
            "local_hour": 11,
            "registry_listed": False,
            "dnc": False,
            "emotion": "happy",
            "duration": 45,
            "future_outcome": "won",
            "transcript": "hello",
            "number_health": 0.8,
        }
    )
    assert set(features) == {"segment", "attempt_count", "local_hour", "number_health"}


def test_pending_outcome_window_is_censored(clock):
    assert (
        delayed_reward(clock.now, clock.now - timedelta(days=2), window_days=14, meeting_held=False) is None
    )
    assert delayed_reward(clock.now, clock.now - timedelta(days=15), window_days=14, meeting_held=False) == 0
    assert delayed_reward(clock.now, clock.now - timedelta(days=15), window_days=14, meeting_held=True) == 1


def test_challenger_evaluates_chronologically_and_requires_approval(tmp_path):
    rows = [
        {
            "lead_id": str(i),
            "endpoint_id": str(i),
            "at": f"2026-09-{1 + i // 10:02d}T12:00:00Z",
            "features": {"local_hour": 9 + i % 8, "attempt_count": i % 3, "number_health": 0.8},
            "human": i % 3 == 0,
        }
        for i in range(120)
    ]
    result = train_challenger(rows, tmp_path)
    assert result["train_through"] < result["validation_from"]
    assert result["calibration_samples"] > 0
    assert "brier" in result["metrics"]
    assert not promotion_allowed({**result, "manual_approved": False, "online_trial_passed": False})
    assert not promotion_allowed(
        {
            **result,
            "manual_approved": True,
            "online_trial_passed": True,
            "coverage_delta": -0.1,
            "policy_violations": 0,
        }
    )


def test_number_selection_stability_health_and_regional_demand(workspace):
    c, h = workspace["client"], workspace["headers"]
    call = start(workspace)
    number_id, lead_id = call["number_id"], call["lead_id"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    packet = c.get(f"/v1/leads/{lead_id}/packet", headers=h).json()
    assert packet["bound_number_id"] == number_id
    r = c.patch(
        f"/v1/admin/numbers/{number_id}",
        headers=h,
        json={"lifecycle": "paused_for_outbound", "reason": "Investigate route failures"},
    )
    assert r.status_code == 200
    proposal = c.post(
        "/v1/admin/numbers/proposal",
        headers=h,
        json={"campaign_id": workspace["campaign_id"], "daily_capacity_per_number": 20},
    )
    assert proposal.status_code == 200
    assert "regions" in proposal.json()
    assert proposal.json()["purchased"] is False


def test_purchase_cannot_exceed_operator_approved_budget(workspace, carrier):
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        "/v1/admin/numbers/purchase",
        headers=h,
        json={
            "phone_numbers": ["+14155550199"],
            "approved_budget_usd": 0,
            "idempotency_key": "purchase-test",
        },
    )
    assert r.status_code == 403
    assert not carrier.purchases


def test_warmup_requires_identity_and_callback_evidence(workspace):
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        "/v1/admin/numbers",
        headers=h,
        json={"phone": "+12125550199", "region": "212", "lifecycle": "purchased", "daily_limit": 5},
    )
    assert r.status_code == 200
    number = r.json()
    assert (
        c.patch(
            f"/v1/admin/numbers/{number['id']}",
            headers=h,
            json={"lifecycle": "healthy", "reason": "Skip warmup"},
        ).status_code
        == 409
    )


def test_metrics_keep_original_cohort_denominator(workspace):
    c, h = workspace["client"], workspace["headers"]
    before = c.get("/v1/metrics", headers=h).json()
    call = start(workspace)
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(f"/v1/calls/{call['id']}/disposition", headers=h, json={"outcome": "voicemail"})
    after = c.get("/v1/metrics", headers=h).json()
    assert before["original_cohort"] == after["original_cohort"]
    assert after["right_party_unique"] == 0
    assert after["attempts"] == 1
    assert after["held_meetings_per_original_lead"] == 0
