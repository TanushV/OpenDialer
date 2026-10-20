"""As-of outcomes, delayed-window embargo and fitted-stage independence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.ml.training import train_challenger
from app.optimization.learning import freeze_features, training_rows
from comparison_oracle import independent_partitions
from conftest import disposition_body, start
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


def test_rolling_asof_entity_embargo(tmp_path):
    """C031: model evidence exposes three honest expanding as-of evaluations."""
    rows = timeline(500, gap_days=2)
    for i, row in enumerate(rows):
        row["company_id"] = f"company-{i // 2}"
        row["features"].update(segment=f"segment-{i % 40}", feature_schema_version=2)
        row["right_party"] = int(i % 4 != 0) if row["human"] else None
    rows[0]["label_available_at"] = rows[-1]["at"]
    model = train_challenger(rows, tmp_path)
    folds = model.get("rolling_evaluations", [])
    assert len(folds) == 3, "One chronological split does not establish rolling validation"
    for fold in folds:
        assert fold["training_cutoff"] < fold["calibration_cutoff"] < fold["evaluation_cutoff"]
        for stage in fold["stages"].values():
            if stage["status"] == "fitted":
                assert stage["train_labels_through"] < stage["calibration_from"]
                assert stage["calibration_labels_through"] < stage["validation_from"]
                assert stage["entity_overlap_count"] == 0
                assert stage["calibration_min_class"] >= 10 and stage["validation_min_class"] >= 10
    assert model["feature_schema_version"] == 2
    assert model["bootstrap"]["resamples"] == 1000
    assert model["bootstrap"]["unit"] == "linked_entity"


def test_feature_vocabulary_is_fitted_only_in_each_training_partition(tmp_path):
    rows = timeline(500, gap_days=2)
    for i, row in enumerate(rows):
        row["features"].update(
            segment="training-segment" if i < 350 else "future-only-segment",
            conversation_concepts=["early-concept"] if i < 350 else ["future-only-concept"],
            conversation_evidence_missing=False,
        )
    model = train_challenger(rows, tmp_path)
    assert model.get("feature_schema_version") == 2
    vocabularies = [fold["vocabulary"] for fold in model["rolling_evaluations"]] + [model["vocabulary"]]
    for vocabulary in vocabularies:
        assert len(vocabulary["segments"]) <= 32 and len(vocabulary["concepts"]) <= 24
        assert "future-only-segment" not in vocabulary["segments"]
        assert "future-only-concept" not in vocabulary["concepts"]
    ablation = model["conversation_ablation"]
    assert ablation["comparison"] == "same_folds_without_conversation_features"
    assert ablation["eligible_for_serving"] is False, (
        "Constant prior-call concepts add no predictive evidence"
    )


def test_linked_company_and_endpoint_chains_cannot_cross_partitions():
    rows = timeline(150, gap_days=2)
    # The link crosses through a second identity, not a shared literal in train/test.
    rows[0]["company_id"] = "shared"
    rows[99]["company_id"] = "shared"
    rows[99]["endpoint_id"] = "bridge"
    rows[130]["endpoint_id"] = "bridge"
    train, calibration, validation = independent_partitions(rows, label="human")
    assert rows[0] in train
    assert rows[99] not in calibration and rows[130] not in validation


def test_unknown_classification_and_unavailable_times_cannot_fit_a_stage(tmp_path):
    rows = timeline(500, gap_days=2)
    for r in rows:
        r["human"] = None
    model = train_challenger(rows, tmp_path)
    assert model["funnel_metrics"]["human"]["status"] != "fitted"
    assert model["reviewable"] is False
    assert model["pooled_stages"] == ["human", "right_party_given_human", "conversion_given_right_party"]


def test_absent_concept_is_unknown_even_when_another_concept_was_observed():
    values = __import__("app.ml.training", fromlist=["vector_v2"]).vector_v2(
        {"segment": "unseen", "conversation_concepts": ["another"], "conversation_evidence_missing": False},
        ["segment:known", "segment:unknown", "concept:approval", "concept:approval:missing"],
        {"segments": ["known"], "concepts": ["approval"]},
    )
    assert values == [0, 1, 0, 1]


def test_missing_label_availability_is_never_assigned_to_the_decision_time(tmp_path):
    rows = timeline(500, gap_days=2)
    for row in rows:
        del row["outcome_available_at"]
    result = train_challenger(rows, tmp_path)
    assert result.get("pooled_stages") == ["human", "right_party_given_human", "conversion_given_right_party"]
    assert result.get("reviewable") is False


def test_evaluation_does_not_use_a_label_received_after_its_asof_cutoff(tmp_path):
    rows = timeline(500, gap_days=2)
    for row in rows[300:]:
        row["outcome_available_at"] = "2040-01-01T00:00:00+00:00"
    result = train_challenger(rows, tmp_path)
    assert result.get("reviewable") is False
    for fold in result.get("rolling_evaluations", []):
        for stage in fold["stages"].values():
            if stage["status"] == "fitted":
                assert stage["validation_labels_through"] < fold["evaluation_cutoff"]


def test_paired_bootstrap_preserves_linked_entities_and_zero_effect():
    from app.ml.training import paired_bootstrap

    # Four attempts, two linked businesses. Every attempt within a business is
    # resampled together; identical alternatives have exactly zero difference.
    result = paired_bootstrap([0.1, 0.7, 0.3, 0.4], [0.1, 0.7, 0.3, 0.4], ["a", "a", "b", "b"])
    assert result["groups"] == 2 and result["resamples"] == 1000
    assert result["mean_difference"] == 0 and result["interval"] == [0, 0]
    improved = paired_bootstrap([0.1] * 4, [0.6] * 4, ["a", "a", "b", "b"])
    assert improved["interval"] == [-0.5, -0.5]
    assert improved == paired_bootstrap([0.1] * 4, [0.6] * 4, ["a", "a", "b", "b"])


def test_unreviewable_v2_candidate_cannot_activate_with_legacy_brier_fields():
    from app.ml.training import promotion_allowed

    assert not promotion_allowed(
        {
            "feature_schema_version": 2,
            "reviewable": False,
            "manual_approved": True,
            "online_trial_passed": True,
            "coverage_delta": 0,
            "policy_violations": 0,
            "metrics": {"brier": 0.01, "pooled_baseline_brier": 0.3},
        }
    )


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


def test_conversion_stage_waits_for_its_conditioning_right_party_label():
    source = timeline()
    source[0]["outcome_available_at"] = source[-1]["at"]
    assert source[0] not in independent_partitions(source, label="conversion")[0]


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
        assert report["partition_policy"] == "as-of-grouped-embargo-v2"


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
    # Unknown availability is excluded entirely; it is neither backdated nor set to the cutoff.
    call, snapshot, event = outcome_fixture(clock)
    del event["recorded_at"]
    row = dataset(call, snapshot, [event], clock.now)[0]
    assert row["conversion_event_ids"] == [] and row["conversion"] == 0
    assert row["label_timing_quality"] == "observed"
    del call["outcome_available_at"]
    assert dataset(call, snapshot, [event], clock.now) == []


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


def test_same_timestamp_correction_uses_the_latest_recorded_revision(clock):
    call, snapshot, _ = outcome_fixture(clock)
    call["outcome_history"] = [
        {
            "outcome": "no_answer",
            "outcome_available_at": call["outcome_available_at"],
            "outcome_source": "rep_reported",
        }
    ]
    assert dataset(call, snapshot, [], clock.now)[0]["human"] is True


def test_repeated_retraction_does_not_move_the_original_availability(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    body = {
        "event_id": "late-meeting",
        "kind": "meeting_held",
        "call_id": call["id"],
        "occurred_at": clock.now.isoformat(),
        "source_reference": "Rep verified attendance",
    }
    event = c.post(f"/v1/leads/{call['lead_id']}/conversions", headers=h, json=body).json()
    path = f"/v1/leads/{call['lead_id']}/conversions/{event['id']}/retract"
    first = c.post(path, headers=h, json={"reason": "Correction"}).json()
    clock.now += timedelta(minutes=5)
    repeated = c.post(path, headers=h, json={"reason": "Correction"}).json()
    assert repeated["retracted_at"] == first["retracted_at"]
    assert repeated["version"] == first["version"]


def test_disposition_corrections_retain_as_of_history_without_transcripts(workspace, app, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    path = f"/v1/calls/{call['id']}/disposition"
    first_at = clock.now
    assert (
        c.post(path, headers=h, json=disposition_body(c, h, call["id"], outcome="right_party")).status_code
        == 200
    )
    clock.now += timedelta(days=1)
    # The session remains valid for this test's one-day boundary only after login.
    login = c.post(
        "/v1/auth/session",
        headers={"X-Local-Token": "test-local-secret"},
        json={"email": "operator@example.test", "password": "a-test-only-password-123"},
    )
    h = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert (
        c.post(path, headers=h, json=disposition_body(c, h, call["id"], outcome="no_answer")).status_code
        == 200
    )
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


def test_revised_meeting_reports_keep_observation_and_retraction_history(workspace, app, clock):
    from app.optimization.learning import conversion_at

    service = app.state.service
    c, h = workspace["client"], workspace["headers"]
    lead_id = c.get("/v1/workspace", headers=h).json()["leads"][0]["id"]
    times = []
    for status in ("held", "no_show", "held"):
        packet = c.get(f"/v1/leads/{lead_id}/packet", headers=h).json()
        result = c.post(
            f"/v1/leads/{lead_id}/meeting",
            headers=h,
            json={
                "request_id": str(uuid4()),
                "expected_version": packet["lead"]["version"],
                "status": status,
                **({"next_action": "keep_hold"} if status == "no_show" else {}),
            },
        )
        assert result.status_code == 200, result.text
        times.append(clock.now)
        clock.now += timedelta(hours=1)
    with service.db.transaction(False) as s:
        row = service._by_key(s, p(workspace), "conversion_events", "rep-meeting:" + lead_id)
        event = {"id": row.id, **row.data}
    first = conversion_at(event, times[0])
    retracted = conversion_at(event, times[1])
    latest = conversion_at(event, times[2])
    assert first and first["retracted"] is False
    assert first["available"] == times[0]
    assert retracted and retracted["retracted"] is True
    assert latest and latest["retracted"] is False and latest["available"] == times[2]
