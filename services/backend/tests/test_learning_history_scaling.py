"""A32/A50: preserve label/attribution semantics without Cartesian history joins."""

from datetime import timedelta

from app.optimization.learning import training_rows, trial_report


class CountedIdentity(str):
    comparisons = 0
    __hash__ = str.__hash__

    def __eq__(self, other):
        type(self).comparisons += 1
        return super().__eq__(other)


def histories(clock, count):
    start = (clock.now - timedelta(days=20)).isoformat()
    observed = (clock.now - timedelta(days=19)).isoformat()
    calls, snapshots, events, assignments = [], [], [], []
    for i in range(count):
        lead = CountedIdentity(f"lead-{i}")
        calls.append(
            {
                "id": f"call-{i}",
                "lead_id": lead,
                "started_at": start,
                "outcome": "right_party",
                "outcome_available_at": start,
                "feature_snapshot_id": f"snapshot-{i}",
            }
        )
        snapshots.append({"id": f"snapshot-{i}", "as_of": start, "features": {"local_hour": 10}})
        events.append(
            {
                "id": f"event-{i}",
                "lead_id": lead,
                "call_id": f"call-{i}",
                "kind": "meeting_held",
                "occurred_at": observed,
                "recorded_at": observed,
                "retracted": bool(i % 2),
            }
        )
        assignments.append(
            {
                "lead_id": lead,
                "assigned_at": start,
                "arm": "candidate" if i % 2 else "control",
                "covered": True,
                "policy_unknown": 0,
            }
        )
    return calls, snapshots, events, assignments


def test_training_does_not_compare_each_call_with_unrelated_conversion_events(clock):
    calls, snapshots, events, _ = histories(clock, 800)
    CountedIdentity.comparisons = 0
    rows = training_rows(calls, snapshots, events, now=clock.now, goal="meeting_held", window_days=14)
    assert len(rows) == 800 and sum(row["conversion"] for row in rows) == 400
    assert CountedIdentity.comparisons <= 800 * 8, CountedIdentity.comparisons


def test_cohort_report_does_not_compare_each_assignment_with_unrelated_events(clock):
    _, _, events, assignments = histories(clock, 800)
    CountedIdentity.comparisons = 0
    result = trial_report(assignments, events, now=clock.now, goal="meeting_held")
    assert result["arms"]["control"]["conversions"] == 400
    assert result["arms"]["candidate"]["conversions"] == 0
    assert result["pending_leads"] == 0
    assert CountedIdentity.comparisons <= 800 * 8, CountedIdentity.comparisons


def test_training_keeps_call_specific_and_unassigned_events_separate(clock):
    calls, snapshots, events, _ = histories(clock, 2)
    calls[1]["lead_id"] = calls[0]["lead_id"]
    events = [{**events[0], "call_id": "call-1"}]
    rows = training_rows(calls, snapshots, events, now=clock.now, goal="meeting_held", window_days=14)
    assert [row["conversion"] for row in rows] == [0, 1]
    events.append({**events[0], "id": "unassigned", "call_id": None})
    rows = training_rows(calls, snapshots, events, now=clock.now, goal="meeting_held", window_days=14)
    assert [row["conversion_event_ids"] for row in rows] == [["unassigned"], ["event-0", "unassigned"]]


def test_cohort_keeps_first_enrollment_and_only_events_inside_its_window(clock):
    _, _, events, assignments = histories(clock, 3)
    events[1]["retracted"] = False
    events[1]["occurred_at"] = (clock.now - timedelta(days=21)).isoformat()
    events[2]["kind"] = "sale_won"
    assignments.append({**assignments[0], "arm": "candidate", "assigned_at": clock.now.isoformat()})
    result = trial_report(assignments, events, now=clock.now, goal="meeting_held")
    assert result["arms"]["control"]["leads"] == 2
    assert result["arms"]["candidate"]["leads"] == 1
    assert result["arms"]["control"]["conversions"] == 1
    assert result["arms"]["candidate"]["conversions"] == 0
