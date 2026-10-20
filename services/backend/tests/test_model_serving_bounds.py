"""A33/A50: bounded serving history, exact verified bytes, and safe probabilities."""

import hashlib
import json
import pickle
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from app.db import Database, get, put, update
from app.ml import serving, training
from conftest import ready
from model_artifacts import column_stage, constant_stage, write
from sqlalchemy import inspect
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


def test_saved_conditional_estimators_retain_their_actual_stage_names(tmp_path):
    descriptor = write(
        tmp_path / "conditional.json",
        {name: column_stage(["requested_contact"]) for name in training.STAGES},
    )
    result = training.predict_funnel_batch(descriptor, [{"requested_contact": 1}], tmp_path)
    assert result == [{"p_human": 1, "p_right_party_given_human": 1, "p_conversion_given_right_party": 1}]


ROWS = [
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


def test_v2_vocabulary_scores_its_exported_feature_order(tmp_path):
    record = write(
        tmp_path / "v2.json",
        {
            "human": column_stage(
                ["segment:enterprise", "concept:approval", "concept:approval:missing"],
                vocabulary={"segments": ["enterprise"], "concepts": ["approval"]},
            )
        },
    )
    values = training.predict_funnel_batch(record, ROWS, tmp_path)
    assert values == [{"p_human": 1.0}, {"p_human": 0.0}]
    assert training.predict_funnel_batch(record, ROWS, tmp_path) == values
    # C034: a different record schema never reinterprets the stored stages.
    assert training.predict_funnel_batch({**record, "feature_schema_version": 1}, ROWS, tmp_path) == [{}, {}]


@pytest.mark.parametrize("version", [1, 2])
def test_retained_pickled_artifacts_are_retired_not_reinterpreted(tmp_path, version):
    """Artifacts from before pure serving stay on disk but are never opened."""
    path = tmp_path / f"v{version}.joblib"
    path.write_bytes(pickle.dumps({"human": "legacy estimator"}))
    record = {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_schema_version": version,
        "feature_order": ["requested_contact"],
    }
    assert training.predict_funnel_batch(record, ROWS, tmp_path) == [{}, {}]
    assert training.retired_reason(record) == serving.RETIRED


def artifact(tmp_path, probability=0.5):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    path = root / "candidate.json"
    descriptor = write(path, {"human": constant_stage(probability)})
    training._MODELS.clear()
    return root, path, descriptor


@pytest.mark.parametrize("action", ["activate", "rollback", "serve"])
@pytest.mark.parametrize("change", [None, "work", "prior", "window", "goal", "source", "artifact"])
def test_v2_model_controls_recheck_frozen_definition_and_owned_source(workspace, app, clock, action, change):
    service, p = app.state.service, principal(workspace)
    root, path, descriptor = artifact(service.settings.data_dir)
    assert root == service.settings.data_dir / "models"
    with service.db.transaction() as s:
        config = service.optimization_config(s, p)
        baseline = {k: config[k] for k in ("estimate_policy", "handling_seconds")}
        source = put(
            s,
            "learning_datasets",
            p["tenant_id"],
            p["seller_id"],
            {
                "goal": config["conversion_goal"],
                "window_days": config["window_days"],
                "baseline": baseline,
            },
            now=clock(),
        )
        model = put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {
                **descriptor,
                "feature_schema_version": 2,
                "source_dataset_id": source.id,
                "goal": config["conversion_goal"],
                "window_days": config["window_days"],
                "baseline": baseline,
                "active": action == "serve",
                "ever_activated": True,
                "reviewable": True,
                "manual_approved": True,
                "online_trial_passed": True,
                "coverage_delta": 0,
                "policy_violations": 0,
            },
            now=clock(),
        )
        model_id = model.id
        campaign = get(s, "campaigns", workspace["campaign_id"])
        values = dict(campaign.data.get("optimization", {}))
        if change == "work":
            values["handling_seconds"] = 300
        elif change == "prior":
            values["estimate_policy"] = {**config["estimate_policy"], "human_prior": [1, 1]}
        elif change == "window":
            values["window_days"] = config["window_days"] + 1
        elif change == "goal":
            values["conversion_goal"] = "sale_won"
        elif change == "source":
            update(model, source_dataset_id="missing")
        elif change == "artifact":
            update(model, checksum="0" * 64)
        if change in {"work", "prior", "window", "goal"}:
            update(campaign, optimization=values)
    if action == "serve":
        with service.db.transaction(False) as s:
            found = service.serving_model(s, p, service.optimization_config(s, p))
            # Artifact verification belongs to batched serving, not pointer lookup.
            if change == "artifact":
                assert training.predict_funnel_batch(found.data, [{}], root) == [{}]
            else:
                assert (found.id if found else None) == (None if change else model_id)
    else:
        response = workspace["client"].post(
            f"/v1/admin/models/{model_id}/{action}",
            headers=workspace["headers"],
            json={"approved": True, "reason": "Reviewed model control"},
        )
        assert response.status_code == (409 if change else 200), response.text


def test_baseline_rollback_names_the_current_baseline(workspace):
    response = workspace["client"].post(
        "/v1/admin/models/baseline/rollback",
        headers=workspace["headers"],
        json={"reason": "Restore current sparse estimator"},
    )
    assert response.status_code == 200
    assert response.json()["active"] == "pooled-funnel-v3"


def test_checksum_verified_bytes_are_the_bytes_deserialized(tmp_path, monkeypatch):
    root, path, model = artifact(tmp_path)
    replacement = json.dumps({"format": serving.FORMAT, "stages": {"human": constant_stage(0.9)}}).encode()
    parse = serving.parse

    def replace_before_parsing(contents):
        path.write_bytes(replacement)
        return parse(contents)

    monkeypatch.setattr(serving, "parse", replace_before_parsing)
    assert training.predict_funnel_batch(model, [{"local_hour": 12}], root) == [{"p_human": 0.5}]
    # A subsequent read notices replacement and cannot reuse a stale verified entry.
    assert training.predict_funnel_batch(model, [{}], root) == [{}]


def test_concurrent_first_predictions_verify_and_load_once(tmp_path, monkeypatch):
    root, _, model = artifact(tmp_path)
    parse, loads = serving.parse, []

    def slow_parse(*args, **kwargs):
        loads.append(1)
        time.sleep(0.05)
        return parse(*args, **kwargs)

    monkeypatch.setattr(serving, "parse", slow_parse)
    with ThreadPoolExecutor(max_workers=6) as executor:
        predictions = list(executor.map(lambda _: training.predict_funnel_batch(model, [{}], root), range(6)))
    assert predictions == [[{"p_human": 0.5}]] * 6
    assert len(loads) == 1


@pytest.mark.parametrize(
    "contents",
    [b'{"format": "opendialer.funnel-trees.v1", "stages": {"human": ', b"NaN", b"\x80\x04K\x01.", b"{}"],
)
def test_invalid_model_content_falls_back_without_poisoning_the_queue(tmp_path, contents):
    root, path, model = artifact(tmp_path)
    path.write_bytes(contents)
    model["checksum"] = hashlib.sha256(contents).hexdigest()
    assert training.predict_funnel_batch(model, [{}, {}], root) == [{}, {}]


def test_oversized_artifact_is_rejected_before_reading_or_loading(tmp_path, monkeypatch):
    root, path, model = artifact(tmp_path)
    with path.open("wb") as handle:
        handle.truncate(65 * 1024 * 1024)

    def forbidden(*args, **kwargs):
        raise AssertionError("Serving read an oversized model")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(serving, "parse", forbidden)
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


def retired_model(service, p, clock, **fields):
    """A current, reviewed model whose artifact predates pure serving (a pickle on disk)."""
    path = service.settings.data_dir / "models" / "legacy" / "challenger.joblib"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps({"feature_schema_version": 2, "stages": {}}))
    with service.db.transaction() as s:
        config = service.optimization_config(s, p)
        baseline = {k: config[k] for k in ("estimate_policy", "handling_seconds")}
        definition = {"goal": config["conversion_goal"], "window_days": config["window_days"]}
        source = put(
            s,
            "learning_datasets",
            p["tenant_id"],
            p["seller_id"],
            {**definition, "baseline": baseline},
            now=clock(),
        )
        model = put(
            s,
            "model_versions",
            p["tenant_id"],
            p["seller_id"],
            {
                "artifact": str(path),
                "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "feature_order": ["local_hour"],
                "feature_schema_version": 2,
                "source_dataset_id": source.id,
                **definition,
                "baseline": baseline,
                "reviewable": True,
                "online_trial_passed": True,
                "coverage_delta": 0,
                "policy_violations": 0,
                **fields,
            },
            now=clock(),
        )
        return model.id


def test_retired_active_model_serves_pooled_estimates_with_a_visible_reason(
    workspace, app, clock, monkeypatch
):
    import app.optimization.learning as learning

    service, p = app.state.service, principal(workspace)
    model_id = retired_model(service, p, clock, active=True, ever_activated=True)
    monkeypatch.setattr(learning, "stable_control", lambda *args, **kwargs: False)  # No holdout lead.
    ready(workspace)
    plan = service._selection(p, workspace["campaign_id"])
    assert plan["prediction"]["model_version"] == "pooled-funnel-v3"
    assert plan["prediction"]["model_fallback"] == {"model_id": model_id, "reason": serving.RETIRED}
    page = workspace["client"].get("/v1/workspace/models", headers=workspace["headers"]).json()
    shown = next(item for item in page["items"] if item["id"] == model_id)
    assert shown["serving_block"] == serving.RETIRED


@pytest.mark.parametrize("action", ["activate", "rollback", "start-trial"])
def test_retired_model_controls_name_the_retirement(workspace, app, clock, action):
    service, p = app.state.service, principal(workspace)
    model_id = retired_model(service, p, clock, ever_activated=action == "rollback")
    body = (
        {"review_reference": "Reviewed controlled pilot", "max_leads": 100, "duration_days": 7}
        if action == "start-trial"
        else {"approved": True, "reason": "Reviewed model control"}
    )
    response = workspace["client"].post(
        f"/v1/admin/models/{model_id}/{action}", headers=workspace["headers"], json=body
    )
    assert response.status_code == 409, response.text
    assert serving.RETIRED in response.json()["detail"]
