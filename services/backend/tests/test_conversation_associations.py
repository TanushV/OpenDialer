"""Conversation learning: honest, later-validated associations between observed approaches and outcomes."""

from datetime import timedelta

import pytest


def cohort(clock, count=80, *, later=False, inverted=False):
    rows = []
    for i in range(count):
        exposure = i % 2 == 0
        at = clock.now - timedelta(days=25 if later else 60, minutes=count - i)
        rows.append(
            {
                "call_id": f"{'later' if later else 'early'}-{i}",
                "lead_id": f"{'later' if later else 'early'}-{i}",
                "at": at.isoformat(),
                "label_available_at": (at + timedelta(days=14)).isoformat(),
                "window_ends_at": (at + timedelta(days=14)).isoformat(),
                "window_closed": True,
                "conversion": int(exposure != inverted),
                "segment": "small-business",
                "features": {"requested_contact": 0, "attempt_count": 0},
                "observations": [
                    {
                        "concept": "Procurement review",
                        "approach": "Clarify approval steps" if exposure else "Describe features",
                        "observed_at": (at + timedelta(minutes=10)).isoformat(),
                        "status": "active",
                        "review_status": "model_reviewed",
                    }
                ],
            }
        )
    return rows


def report(rows, clock, **kwargs):
    from app.optimization.conversations import conversation_associations

    return conversation_associations(rows, as_of=clock.now, min_support=10, **kwargs)


def pattern(result, approach="Clarify approval steps"):
    return next(x for x in result["patterns"] if x["approach"] == approach)


def test_association_needs_later_independent_support_not_only_a_good_discovery_fit(clock):
    early = cohort(clock)
    result = report(early + cohort(clock, later=True), clock)
    found = pattern(result)
    assert found["status"] == "supported_correlation"
    assert found["samples"] == 80
    assert found["validation"]["exposed"] == 40
    assert found["validation"]["reference"] == 40
    assert found["validation"]["difference_low"] > 0
    assert found["automatic_policy_change"] is False
    assert result["multiple_comparison"] == "bonferroni_simultaneous_intervals"
    assert result["unit"] == "one_first_eligible_conversation_per_lead_or_endpoint"
    # Labels for this single time cohort are not available before its own later calls.
    assert pattern(report(early, clock))["status"] != "supported_correlation"
    reversed_result = report(early + cohort(clock, later=True, inverted=True), clock)
    assert pattern(reversed_result)["status"] != "supported_correlation"


def test_repeated_calls_or_shared_endpoint_do_not_inflate_support(clock):
    rows = cohort(clock, count=8)
    repeated = [dict(row, call_id=f"{row['call_id']}-{i}") for row in rows for i in range(20)]
    assert report(repeated, clock)["patterns"] == []
    rows = cohort(clock)
    for row in rows:
        row["endpoint_id"] = "shared-switchboard"
    assert report(rows, clock)["patterns"] == []


@pytest.mark.parametrize(
    "bad", ["future_observation", "unreviewed", "invalidated", "pending", "future_label", "after_label"]
)
def test_unavailable_or_unreliable_observations_cannot_become_supported_evidence(clock, bad):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows:
        observation = row["observations"][0]
        if bad == "future_observation":
            observation["observed_at"] = (clock.now + timedelta(days=1)).isoformat()
        elif bad == "unreviewed":
            observation["review_status"] = "unreviewed"
        elif bad == "invalidated":
            observation["status"] = "invalidated"
        elif bad == "pending":
            row.update(window_closed=False, conversion=None)
        elif bad == "future_label":
            row["label_available_at"] = (clock.now + timedelta(days=1)).isoformat()
        else:
            observation["observed_at"] = clock.now.isoformat()
    assert report(rows, clock)["patterns"] == []


def test_comparable_mix_is_required_and_sparse_support_is_visible(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows:
        row["segment"] = "warm" if row["conversion"] else "cold"
    found = pattern(report(rows, clock))
    assert found["status"] == "descriptive_only"
    assert found["validation"]["comparable_strata"] == 0
    assert found["reason"]


def test_vocabulary_normalizes_surface_forms_but_never_erases_negation(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for i, row in enumerate(rows):
        row["observations"][0]["concept"] = "PROCUREMENT  REVIEW!" if i % 3 else "Procurement review"
    result = report(rows, clock)
    assert len({x["concept_key"] for x in result["patterns"]}) == 1
    for row in rows[:40]:
        row["observations"][0]["concept"] = "No procurement review"
    result = report(rows, clock)
    assert len({x["concept_key"] for x in result["patterns"]}) == 2


def test_human_reviewed_aliases_are_versioned_and_do_not_create_new_observations(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows[:80]:
        row["observations"][0]["concept"] = "Purchasing signoff"
    vocabulary = {
        "version": 3,
        "aliases": [{"field": "concept", "source": "Purchasing signoff", "target": "Procurement review"}],
    }
    result = report(rows, clock, vocabulary=vocabulary)
    assert result["vocabulary_version"] == 3
    assert pattern(result)["status"] == "supported_correlation"
    assert len({x["concept_key"] for x in result["patterns"]}) == 1
    assert report([], clock, vocabulary=vocabulary)["patterns"] == []
