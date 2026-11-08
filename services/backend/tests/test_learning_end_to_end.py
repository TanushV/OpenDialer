"""R8.9: a pattern planted in 600 synthetic calls is learned, promoted, used, and rolled back when it stops working."""

import random
from datetime import UTC, datetime, timedelta

import pytest
from app.ml import training
from app.optimization.learning import arm_comparison, rollback_due

START = datetime(2026, 6, 1, tzinfo=UTC)


def planted(count=600, seed=7, *, pattern=True):
    """People reached after a hiring trigger pick up far more often; everything else is noise. Without the pattern every
    call has the same chance."""
    rng = random.Random(seed)
    rows = []
    for i in range(count):
        trigger = rng.choice(["hiring_sdrs", "new_funding", "none", "expansion"])
        chance = (0.75 if trigger == "hiring_sdrs" else 0.2) if pattern else 0.34
        rows.append(
            {
                "lead_id": f"lead-{i}",
                "endpoint_id": f"endpoint-{i}",
                "at": (START + timedelta(hours=i)).isoformat(),
                "outcome_available_at": (START + timedelta(hours=i, minutes=1)).isoformat(),
                "human": int(rng.random() < chance),
                "features": {
                    "feature_schema_version": 3,
                    "local_hour": rng.randrange(8, 18),
                    "local_weekday": i % 5,
                    "attempt_count": rng.randrange(0, 4),
                    "number_health": rng.random(),
                    "source_age_days": rng.uniform(0, 200),
                    "elapsed_hours": rng.choice([1, 24, 200]),
                    "requested_contact": 0,
                    "segment": "general",
                    "trigger_type": trigger,
                    "title_seniority": rng.choice(["vp", "director", "manager"]),
                },
            }
        )
    return rows


@pytest.mark.slow
def test_the_review_finds_a_real_pattern_and_rarely_promotes_noise(tmp_path):
    found = [
        training.train_challenger(planted(seed=s), tmp_path / f"p{s}")["reviewable"] for s in range(1, 9)
    ]
    noise = [
        training.train_challenger(planted(seed=s, pattern=False), tmp_path / f"n{s}")["reviewable"]
        for s in range(1, 9)
    ]
    assert sum(found) >= 7, found
    assert sum(noise) <= 1, noise


@pytest.mark.slow
def test_a_planted_pattern_is_learned_promoted_used_and_rolled_back(tmp_path):
    root = tmp_path / "models"
    result = training.train_challenger(planted(), root / "candidate")
    # Learned and promoted: the out-of-time review passes on its own (R8.5).
    assert result["reviewable"], result.get("review")
    assert training.promotion_allowed(result)
    # Used: the served model ranks a hiring-trigger lead above the same lead without it.
    base = planted(1)[0]["features"]
    hiring, other = {**base, "trigger_type": "hiring_sdrs"}, {**base, "trigger_type": "none"}
    scored = training.predict_funnel_batch(result, [hiring, other], root)
    assert scored[0]["p_human"] > scored[1]["p_human"] + 0.2
    # Rolled back: after promotion the pattern stops, and the comparison group does better (R8.6).
    promoted = START + timedelta(days=60)
    later = [
        {
            "experiment_arm": arm,
            "at": (promoted + timedelta(hours=i)).isoformat(),
            "conversion": int(i % 10 < rate),
        }
        for arm, rate in (("candidate", 1), ("control", 2))
        for i in range(120)
    ]
    assert rollback_due(arm_comparison(later, promoted))
