"""As-of outcomes, delayed-window embargo and fitted-stage independence."""

from datetime import UTC, datetime, timedelta

from app.ml.training import independent_partitions, train_challenger
from app.optimization.learning import freeze_features, training_rows
from conftest import start
from test_remediation_rep_workflow import p


def timeline(count=150, gap_days=7):
    at = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        {
            "lead_id": str(i),
            "endpoint_id": "e" + str(i),
            "at": (at + timedelta(days=i)).isoformat(),
            "outcome_available_at": (at + timedelta(days=i, minutes=1)).isoformat(),
            "label_available_at": (at + timedelta(days=i + gap_days)).isoformat(),
            "window_ends_at": (at + timedelta(days=i + gap_days)).isoformat(),
            "window_closed": True,
            "human": int(i % 3 != 0),
            "right_party": int(i % 3 != 0),
            "conversion": i % 2,
            "features": {"local_hour": 10 + i % 4},
        }
        for i in range(count)
    ]


def test_each_fitting_partition_purges_labels_crossing_its_next_boundary():
    source = timeline()
    # A very late observation must be purged even though its outcome window closed.
    source[0]["label_available_at"] = source[-1]["at"]
    train, calibration, validation = independent_partitions(source)
    assert train and calibration and validation
    assert all(max(r["label_available_at"], r["window_ends_at"]) < calibration[0]["at"] for r in train)
    assert all(max(r["label_available_at"], r["window_ends_at"]) < validation[0]["at"] for r in calibration)
    assert source[0] not in train


def test_answer_model_can_learn_before_conversion_windows_mature():
    source = timeline(gap_days=400)
    train, calibration, validation = independent_partitions(source, label="human")
    assert len(train) == 90 and len(calibration) == 30 and len(validation) == 30
    conversion = independent_partitions(source, label="conversion")
    assert not conversion[0] and not conversion[1]


def test_timestamp_ties_are_not_split_between_training_and_calibration():
    source = timeline()
    for row in source[85:95]:
        row["at"] = source[85]["at"]
        row["outcome_available_at"] = row["at"]
    train, calibration, validation = independent_partitions(source, label="human")
    assert max(r["at"] for r in train) < min(r["at"] for r in calibration)
    assert max(r["at"] for r in calibration) < min(r["at"] for r in validation)


def test_duplicate_entities_cannot_cross_fitting_partitions():
    source = timeline()
    source[95]["endpoint_id"] = source[1]["endpoint_id"]
    source[125]["lead_id"] = source[96]["lead_id"]
    partitions = independent_partitions(source)
    for field in ("lead_id", "endpoint_id"):
        sets = [{r[field] for r in part} for part in partitions]
        assert not sets[0] & sets[1] and not sets[0] & sets[2] and not sets[1] & sets[2]


def test_challenger_persists_auditable_per_stage_cutoffs(tmp_path):
    model = train_challenger(timeline(400), tmp_path)
    for stage in ("human", "right_party_given_human", "conversion_given_right_party"):
        report = model["funnel_metrics"][stage]
        if stage == "right_party_given_human":
            # This fixture has only positive right-party labels conditional on human.
            assert report["status"] == "insufficient_independent_matured_data"
            continue
        assert report["train_labels_through"] < report["calibration_from"]
        assert report["calibration_labels_through"] < report["validation_from"]
        assert report["partition_policy"] == "as-of-grouped-embargo-v1"


def outcome_fixture(clock):
    at = clock.now - timedelta(days=20)
    call = {
        "id": "call",
        "lead_id": "lead",
        "endpoint_id": "endpoint",
        "feature_snapshot_id": "snapshot",
        "started_at": at.isoformat(),
        "ended_at": (at + timedelta(minutes=1)).isoformat(),
        "outcome_available_at": (at + timedelta(minutes=2)).isoformat(),
        "outcome": "right_party",
    }
    snapshot = {"id": "snapshot", "as_of": at.isoformat(), "features": {"local_hour": 10}}
    event = {
        "id": "event",
        "lead_id": "lead",
        "call_id": "call",
        "kind": "meeting_held",
        "occurred_at": (at + timedelta(days=2)).isoformat(),
        "recorded_at": (clock.now + timedelta(days=1)).isoformat(),
    }
    return call, snapshot, event


def dataset(call, snapshot, events, now):
    return training_rows([call], [snapshot], events, now=now, goal="meeting_held", window_days=14)


def test_late_observed_conversion_is_not_available_at_its_occurrence(clock):
    call, snapshot, event = outcome_fixture(clock)
    before = dataset(call, snapshot, [event], clock.now)[0]
    assert before["conversion"] == 0 and before["conversion_event_ids"] == []
    after = dataset(call, snapshot, [event], clock.now + timedelta(days=2))[0]
    assert after["conversion"] == 1
    assert after["label_available_at"] == event["recorded_at"]


def test_retraction_changes_only_datasets_after_it_is_known(clock):
    call, snapshot, event = outcome_fixture(clock)
    event.update(
        recorded_at=event["occurred_at"],
        retracted=True,
        retracted_at=(clock.now + timedelta(days=1)).isoformat(),
    )
    assert dataset(call, snapshot, [event], clock.now)[0]["conversion"] == 1
    after = dataset(call, snapshot, [event], clock.now + timedelta(days=2))[0]
    assert after["conversion"] == 0 and after["label_available_at"] == event["retracted_at"]


def test_future_disposition_is_excluded_and_provisional_labels_are_not_training_truth(clock):
    call, snapshot, _ = outcome_fixture(clock)
    call["outcome_available_at"] = (clock.now + timedelta(minutes=1)).isoformat()
    assert dataset(call, snapshot, [], clock.now) == []
    call.update(outcome_available_at=clock.now.isoformat(), outcome_source="llm_provisional")
    assert dataset(call, snapshot, [], clock.now) == []


def test_legacy_unknown_label_availability_is_not_backdated(clock):
    call, snapshot, event = outcome_fixture(clock)
    del event["recorded_at"]
    row = dataset(call, snapshot, [event], clock.now)[0]
    assert row["label_available_at"] == clock.now.isoformat()
    assert row["label_timing_quality"] == "observation_time_unknown"


def test_future_callback_does_not_change_frozen_pre_call_features(clock):
    calls = [
        {
            "lead_id": "lead",
            "started_at": (clock.now + timedelta(days=1)).isoformat(),
            "outcome": "callback_requested",
        }
    ]
    features = freeze_features({"id": "lead"}, health={"score": None}, now=clock.now, calls=calls)
    assert features["callback_history"] == 0


def test_disposition_corrections_retain_as_of_history_without_transcripts(workspace, app, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    path = f"/v1/calls/{call['id']}/disposition"
    first_at = clock.now
    assert c.post(path, headers=h, json={"outcome": "right_party"}).status_code == 200
    clock.now += timedelta(days=1)
    # The session remains valid for this test's one-day boundary only after login.
    login = c.post(
        "/v1/auth/session",
        headers={"X-Local-Token": "test-local-secret"},
        json={"email": "operator@example.test", "password": "a-test-only-password-123"},
    )
    h = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert c.post(path, headers=h, json={"outcome": "no_answer"}).status_code == 200
    service = app.state.service
    with service.db.transaction(False) as s:
        stored = dict(service.required(s, "call_attempts", call["id"], p(workspace)).data) | {
            "id": call["id"]
        }
        snapshot = dict(
            service.required(s, "feature_snapshots", stored["feature_snapshot_id"], p(workspace)).data
        )
        snapshot["id"] = stored["feature_snapshot_id"]
    assert stored["outcome_history"][0]["outcome"] == "right_party"
    assert dataset(stored, snapshot, [], first_at)[0]["human"] is True
    assert dataset(stored, snapshot, [], clock.now)[0]["human"] is False
