"""A33/A50: bounded serving history, exact verified bytes, and safe probabilities."""

import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import joblib
import numpy as np
import pytest
from app.db import Database, get, put, update
from app.ml import training
from conftest import ready
from sklearn.dummy import DummyClassifier
from sqlalchemy import inspect
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


class FeatureColumnClassifier:
    def predict_proba(self, values):
        # Independent oracle: a named training-vocabulary column is probability.
        probabilities = np.asarray(values)[:, 0]
        return np.column_stack((1 - probabilities, probabilities))


def test_saved_conditional_estimators_retain_their_actual_stage_names(tmp_path):
    path = tmp_path / "conditional.joblib"
    joblib.dump(
        {
            name: FeatureColumnClassifier()
            for name in ("human", "right_party_given_human", "conversion_given_right_party")
        },
        path,
    )
    descriptor = {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_order": ["requested_contact"],
    }
    result = training.predict_funnel_batch(descriptor, [{"requested_contact": 1}], tmp_path)
    assert result == [{"p_human": 1, "p_right_party_given_human": 1, "p_conversion_given_right_party": 1}]


@pytest.mark.parametrize("version", [1, 2])
def test_v1_v2_model_compatibility(tmp_path, version):
    """C034: a v2 vocabulary must not reinterpret a retained v1 artifact."""
    path = tmp_path / f"v{version}.joblib"
    artifact = {"human": FeatureColumnClassifier()}
    if version == 2:
        artifact.update(
            feature_schema_version=2, vocabulary={"segments": ["enterprise"], "concepts": ["approval"]}
        )
    joblib.dump(artifact, path)
    record = {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_schema_version": version,
        "feature_order": ["requested_contact"]
        if version == 1
        else ["segment:enterprise", "concept:approval", "concept:approval:missing"],
    }
    rows = [
        {
            "requested_contact": 1,
            "segment": "enterprise",
            "conversation_concepts": ["approval"],
            "conversation_evidence_missing": False,
        },
        {
            "requested_contact": 0,
            "segment": "unknown",
            "conversation_concepts": [],
            "conversation_evidence_missing": True,
        },
    ]
    values = training.predict_funnel_batch(record, rows, tmp_path)
    assert values == [{"p_human": 1.0}, {"p_human": 0.0}]
    assert training.predict_funnel_batch(record, rows, tmp_path) == values


def artifact(tmp_path, probability=None):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    path = root / "candidate.joblib"
    model = DummyClassifier(strategy="prior").fit([[0], [1]], [0, 1])
    joblib.dump({"human": model if probability is None else ProbabilityEstimator(probability)}, path)
    training._MODELS.clear()
    return (
        root,
        path,
        {
            "artifact": str(path),
            "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
            "feature_order": ["local_hour"],
        },
    )


class ProbabilityEstimator:
    def __init__(self, probability):
        self.probability = probability

    def predict_proba(self, values):
        if self.probability == "short":
            return np.array([[1.0]])
        return np.array([[1 - self.probability, self.probability] for _ in values])


def test_checksum_verified_bytes_are_the_bytes_deserialized(tmp_path, monkeypatch):
    root, path, model = artifact(tmp_path)
    replacement = io.BytesIO()
    joblib.dump(
        {"human": DummyClassifier(strategy="constant", constant=1).fit([[0], [1]], [0, 1])}, replacement
    )
    load = joblib.load

    def replace_before_deserialization(source, *args, **kwargs):
        path.write_bytes(replacement.getvalue())
        return load(source, *args, **kwargs)

    monkeypatch.setattr(joblib, "load", replace_before_deserialization)
    assert training.predict_funnel_batch(model, [{"local_hour": 12}], root) == [{"p_human": 0.5}]
    # A subsequent read notices replacement and cannot reuse a stale verified entry.
    assert training.predict_funnel_batch(model, [{}], root) == [{}]


def test_concurrent_first_predictions_verify_and_load_once(tmp_path, monkeypatch):
    root, _, model = artifact(tmp_path)
    load, loads = joblib.load, []

    def slow_load(*args, **kwargs):
        loads.append(1)
        time.sleep(0.05)
        return load(*args, **kwargs)

    monkeypatch.setattr(joblib, "load", slow_load)
    with ThreadPoolExecutor(max_workers=6) as executor:
        predictions = list(executor.map(lambda _: training.predict_funnel_batch(model, [{}], root), range(6)))
    assert predictions == [[{"p_human": 0.5}]] * 6
    assert len(loads) == 1


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -0.1, 1.1, "short"])
def test_invalid_model_probabilities_fall_back_without_poisoning_the_queue(tmp_path, probability):
    root, _, model = artifact(tmp_path, probability)
    assert training.predict_funnel_batch(model, [{}, {}], root) == [{}, {}]


def test_oversized_artifact_is_rejected_before_reading_or_loading(tmp_path, monkeypatch):
    root, path, model = artifact(tmp_path)
    with path.open("wb") as handle:
        handle.truncate(65 * 1024 * 1024)

    def forbidden(*args, **kwargs):
        raise AssertionError("Serving read an oversized model")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(joblib, "load", forbidden)
    assert training.predict_funnel_batch(model, [{}], root) == [{}]


@pytest.mark.parametrize("expired_trial", [False, True])
def test_retired_model_history_does_not_enter_reservation_reads(
    workspace, app, clock, monkeypatch, expired_trial
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for _ in range(600):
            put(
                s,
                "model_versions",
                p["tenant_id"],
                p["seller_id"],
                {
                    "active": False,
                    "trial_active": expired_trial,
                    "trial_ends_at": (clock.now - timedelta(days=1)).isoformat(),
                    "goal": "meeting_held",
                },
                now=clock.now,
            )
    ready(workspace)
    decrypted = count_decrypted(app, monkeypatch)
    response = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert response.status_code == 200, response.text
    assert len(decrypted) <= 180, len(decrypted)


def test_serving_pointer_is_loaded_once_per_transaction_without_unrelated_enrollment(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        active = put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {"active": True, "goal": "meeting_held"},
            now=clock.now,
        )
        model_id = active.id
        for i in range(200):
            put(
                s,
                "experiment_assignments",
                p["tenant_id"],
                p["seller_id"],
                {"lead_id": f"prior-{i}", "epoch": "prior-cohort"},
                now=clock.now,
            )
    decrypted = count_decrypted(app, monkeypatch)
    with service.db.transaction(False) as s:
        config = service.optimization_config(s, p)
        for _ in range(20):
            assert service.serving_model(s, p, config).id == model_id
    assert len(decrypted) <= 6, len(decrypted)
    with service.db.transaction() as s:
        update(get(s, "model_versions", model_id), active=False)
    with service.db.transaction(False) as s:
        assert service.serving_model(s, p, service.optimization_config(s, p)) is None


def test_serving_index_backfill_keeps_source_identity_and_versions(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {
                "active": False,
                "trial_active": True,
                "goal": "meeting_held",
                "trial_ends_at": (clock.now + timedelta(days=1)).isoformat(),
            },
            now=clock.now,
        )
        saved = (model.id, model.version, model.data)
    columns = {c["name"] for c in inspect(service.db.engine).get_columns("model_versions")}
    assert {"ctx_key", "ctx_status", "ctx_expires_at", "ctx_index_version"} <= columns
    with service.db.transaction() as s:
        row = get(s, "model_versions", saved[0])
        row.ctx_index_version = None
        row.ctx_key = None
    reopened = Database(service.settings.database_url, service.settings.secret_key)
    try:
        with reopened.transaction(False) as s:
            row = get(s, "model_versions", saved[0])
            assert (row.id, row.version, row.data) == saved
            assert row.ctx_key == "trial"
            assert row.ctx_status == "meeting_held"
    finally:
        reopened.engine.dispose()


def test_queue_scores_one_vector_batch_for_each_selected_model(workspace, app, clock, monkeypatch):
    from app.db import doc, uid
    from app.optimization.learning import stable_control

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        template = service.scoped(s, "leads", p)[0]
        for _ in range(18):
            identifier = uid()
            while stable_control(identifier):
                identifier = uid()
            put(
                s,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                dict(template.data),
                object_id=identifier,
                now=clock.now,
            )
        model = put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {"active": True, "goal": "meeting_held"},
            now=clock.now,
        )
        expected_id = model.id
    calls = []

    def predict(model, features, root):
        calls.append(features)
        return [{"p_human": 0.5} for _ in features]

    monkeypatch.setattr(training, "predict_funnel_batch", predict)
    ready(workspace)
    service._selection(p, workspace["campaign_id"])
    assert len(calls) == 1
    assert len(calls[0]) >= 18
    with service.db.transaction(False) as s:
        assert doc(service.serving_model(s, p, service.optimization_config(s, p)))["id"] == expected_id


def test_trial_pointer_cannot_cross_its_reviewed_epoch(workspace, app, clock):
    from app.db import uid
    from app.optimization.learning import stable_control

    service, p = app.state.service, principal(workspace)
    lead_id = uid()
    while stable_control(lead_id):
        lead_id = uid()
    with service.db.transaction() as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        put(
            s,
            "experiment_assignments",
            p["tenant_id"],
            p["seller_id"],
            {"lead_id": lead_id, "epoch": epoch, "family": "optimization"},
            now=clock.now,
        )
        put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {
                "active": False,
                "trial_active": True,
                "trial_epoch": "different-reviewed-epoch",
                "trial_ends_at": (clock.now + timedelta(days=1)).isoformat(),
                "goal": "meeting_held",
            },
            now=clock.now,
        )
    with service.db.transaction(False) as s:
        assert service.serving_model(s, p, service.optimization_config(s, p), lead_id) is None


@pytest.mark.parametrize("matching", [True, False])
def test_trial_enrollment_reads_only_the_original_model_assignment(
    workspace, app, clock, monkeypatch, matching
):
    from app.db import uid
    from app.optimization.learning import stable_control

    service, p = app.state.service, principal(workspace)
    lead_id = uid()
    while stable_control(lead_id):
        lead_id = uid()
    with service.db.transaction() as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        model = put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {
                "active": False,
                "trial_active": True,
                "trial_epoch": epoch,
                "trial_ends_at": (clock.now + timedelta(days=1)).isoformat(),
                "goal": "meeting_held",
            },
            now=clock.now,
        )
        model_id = model.id
        for i in range(600):
            put(
                s,
                "experiment_assignments",
                p["tenant_id"],
                p["seller_id"],
                {"lead_id": f"other-{i}", "epoch": epoch, "family": "optimization", "arm": "candidate"},
                key=epoch + f":other-{i}",
                now=clock.now,
            )
        put(
            s,
            "experiment_assignments",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead_id,
                "epoch": epoch,
                "family": "optimization",
                "arm": "candidate",
                "trial_model_id": model_id if matching else "different-reviewed-model",
            },
            key=epoch + ":" + lead_id,
            now=clock.now,
        )
    decrypted = count_decrypted(app, monkeypatch)
    with service.db.transaction(False) as s:
        config = service.optimization_config(s, p)
        for _ in range(20):
            selected = service.serving_model(s, p, config, lead_id)
            assert (selected.id if selected else None) == (model_id if matching else None)
    assert len(decrypted) <= 8, len(decrypted)
