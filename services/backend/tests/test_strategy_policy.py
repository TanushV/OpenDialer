"""The finite strategy policy keeps the reviewed default until another approach earns the lead."""

import pytest
from app.optimization.learning import distribution_from_counts

ACTIONS = ["control", "candidate"]


def counts(**arms):
    """Matured rewards per action at propensity 0.5 (weight 2), as reward_counts records them."""
    values = {}
    for action, (n, successes) in arms.items():
        values.update(
            {
                f"reward:{action}:all_n": n,
                f"reward:{action}:all_success": successes,
                f"reward:{action}:n": n,
                f"reward:{action}:weight": 2 * n,
                f"reward:{action}:weighted_success": 2 * successes,
            }
        )
    return values


def test_no_support_keeps_the_reviewed_default():
    policy = distribution_from_counts(ACTIONS, counts(control=(3, 0), candidate=(4, 2)))
    assert policy["best"] == "control"
    assert policy["probabilities"] == pytest.approx({"control": 0.95, "candidate": 0.05})


def test_reaching_support_first_is_not_enough_to_replace_the_default():
    # A 50/50 draw can give one approach ten outcomes first; with no conversions it has not won.
    policy = distribution_from_counts(ACTIONS, counts(control=(6, 0), candidate=(10, 0)))
    assert policy["observations"] == {"control": 6, "candidate": 10}
    assert policy["best"] == "control"


def test_a_supported_approach_with_a_better_estimate_takes_the_lead():
    policy = distribution_from_counts(ACTIONS, counts(control=(12, 1), candidate=(12, 9)))
    assert policy["estimates"]["candidate"] > policy["estimates"]["control"]
    assert policy["best"] == "candidate"
    assert policy["probabilities"] == pytest.approx({"control": 0.05, "candidate": 0.95})


def test_a_supported_but_worse_approach_never_takes_the_lead():
    policy = distribution_from_counts(ACTIONS, counts(control=(20, 8), candidate=(20, 1)))
    assert policy["best"] == "control"
