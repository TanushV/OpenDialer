"""Promoted funnel models are scored by a pure-Python evaluator, never by unpickling.

The oracle is scikit-learn itself: the trainer exports each calibrated histogram gradient
boosting stage as plain JSON, and the authority's evaluator must reproduce predict_proba.
"""

import hashlib
import json
import math
import pickle
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from app.ml import serving, training

TOLERANCE = 1e-9


def synthetic_rows(seed, count=360, *, missing=0.0, conversation=False):
    rng = random.Random(seed)
    segments = ["enterprise", "smb", "public", "unknown", "agency"]
    concepts = ["approval", "budget", "timing", "pricing"]
    at = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for i in range(count):
        health = None if rng.random() < missing else rng.random()
        hour = None if rng.random() < missing / 2 else rng.randrange(7, 20)
        segment = rng.choice(segments)
        mentioned = [c for c in concepts if rng.random() < 0.3]
        score = (
            (0.9 if segment == "enterprise" else -0.4 if segment == "public" else 0.0)
            + (1.2 * health if health is not None else -0.8)
            + (0.15 * (hour - 12) if hour is not None else 0.3)
            + (0.9 if conversation and "approval" in mentioned else 0.0)
            + rng.gauss(0, 0.7)
        )
        rows.append(
            {
                "lead_id": f"lead-{i}",
                "endpoint_id": f"endpoint-{i}",
                "at": (at + timedelta(hours=i)).isoformat(),
                "outcome_available_at": (at + timedelta(hours=i, minutes=1)).isoformat(),
                "human": int(score > 0.4),
                "features": {
                    "local_hour": hour,
                    "local_weekday": i % 7,
                    "attempt_count": rng.randrange(0, 5),
                    "number_health": health,
                    "source_age_days": rng.uniform(0, 400),
                    "elapsed_hours": rng.choice([0.5, 3, 30, 200.25]),
                    "requested_contact": int(rng.random() < 0.1),
                    "segment": segment,
                    "conversation_concepts": mentioned,
                    "conversation_evidence_missing": not conversation or rng.random() < 0.2,
                },
            }
        )
    return rows


def thirds(rows):
    size = len(rows) // 3
    return [rows[:size], rows[size : 2 * size], rows[2 * size :]]


def evaluator(document):
    # Round-trip through the stored bytes, exactly as the authority reads them.
    return serving.parse(json.dumps({"format": serving.FORMAT, "stages": {"human": document}}).encode())[
        "human"
    ]


@pytest.mark.parametrize(
    "case",
    [
        {"seed": 1, "missing": 0.0, "conversation": False},
        {"seed": 2, "missing": 0.3, "conversation": False},
        {"seed": 3, "missing": 0.15, "conversation": True},
    ],
)
def test_production_stage_export_equals_sklearn_predict_proba(case):
    """The trainer's own fit path: chronological thirds, one-hot segments and concepts, NaNs."""
    rows = synthetic_rows(case["seed"], missing=case["missing"], conversation=case["conversation"])
    packed, report, _ = training.fit_stage(thirds(rows), "human", conversation=case["conversation"])
    assert report["status"] == "fitted", report
    order, vocabulary = packed["feature_order"], packed["vocabulary"]
    assert any(name.startswith("segment:") for name in order)
    if case["conversation"]:
        assert any(name.startswith("concept:") for name in order)
    probe = [*rows, *synthetic_rows(case["seed"] + 100, count=200, missing=0.5, conversation=True)]
    vectors = [training.vector_v2(row["features"], order, vocabulary) for row in probe]
    if case["missing"]:
        assert any(math.isnan(value) for vector in vectors for value in vector)
    expected = packed["estimator"].predict_proba(np.asarray(vectors))[:, 1]
    actual = evaluator(packed["export"]).probabilities(vectors)
    assert max(abs(a - e) for a, e in zip(actual, expected, strict=True)) <= TOLERANCE


def stress_data(seed, rows=600, columns=6):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(rows, columns))
    x[rng.random((rows, columns)) < 0.2] = np.nan
    # Column 0 carries signal only through missingness: a split on NaN against every value.
    x[:, 0] = np.where(rng.random(rows) < 0.5, np.nan, 1.0)
    signal = np.isnan(x[:, 0]) * 1.5 + np.nan_to_num(x[:, 1]) - 0.5 * np.nan_to_num(x[:, 2])
    y = (signal + rng.normal(scale=0.6, size=rows) > 0.6).astype(int)
    return x, y


@pytest.mark.parametrize("ensemble", [False, True])
def test_deep_ensembles_with_nan_splits_equal_sklearn(ensemble):
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.frozen import FrozenEstimator

    x, y = stress_data(7 if ensemble else 8)
    model = HistGradientBoostingClassifier(
        max_iter=150, max_leaf_nodes=31, random_state=0, early_stopping=False
    )
    if ensemble:
        calibrated = CalibratedClassifierCV(model, method="sigmoid", cv=3).fit(x, y)
        assert len(calibrated.calibrated_classifiers_) == 3
    else:
        model.fit(x[:300], y[:300])
        calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid").fit(x[300:], y[300:])
    order = [f"column_{i}" for i in range(x.shape[1])]
    document = training.export_stage(calibrated, order, {"segments": [], "concepts": []})
    thresholds = [t for m in document["members"] for tree in m["trees"] for t in tree["threshold"]]
    assert "inf" in thresholds, "The fixture must exercise sklearn's split-on-NaN threshold"
    probe, _ = stress_data(99, rows=400)
    expected = calibrated.predict_proba(probe)[:, 1]
    actual = evaluator(document).probabilities(probe.tolist())
    assert max(abs(a - e) for a, e in zip(actual, expected, strict=True)) <= TOLERANCE


def test_trained_challenger_is_json_and_serves_without_the_numeric_stack(tmp_path):
    rows = synthetic_rows(11, count=420, missing=0.2)
    result = training.train_challenger(rows, tmp_path / "models" / "candidate")
    path = Path(result["artifact"])
    assert path.name == "challenger.json"
    assert result["artifact_format"] == serving.FORMAT
    document = json.loads(path.read_bytes())
    assert document["format"] == serving.FORMAT and document["stages"]
    assert result["checksum"] == hashlib.sha256(path.read_bytes()).hexdigest()
    batch = [row["features"] for row in rows[:40]]
    local = training.predict_funnel_batch(result, batch, tmp_path / "models")
    assert local and all("p_human" in row for row in local)
    blocked = ["numpy", "scipy", "sklearn", "joblib", "threadpoolctl"]
    script = (
        "import json, sys\n"
        f"for name in {blocked!r}: sys.modules[name] = None\n"
        "from app.ml import training\n"
        "model, batch, root = json.loads(sys.stdin.read())\n"
        "print(json.dumps(training.predict_funnel_batch(model, batch, root)))\n"
        f"assert not any(sys.modules.get(n) for n in {blocked!r})\n"
    )
    served = subprocess.run(  # noqa: S603 - current interpreter, fixed probe script
        [sys.executable, "-c", script],
        input=json.dumps([result, batch, str(tmp_path / "models")]),
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(training.__file__).resolve().parents[2],
        timeout=120,
    )
    assert json.loads(served.stdout) == local


class Detonator:
    def __init__(self, sentinel):
        self.sentinel = sentinel

    def __reduce__(self):
        return (open, (self.sentinel, "w"))


@pytest.mark.parametrize("claims_current_format", [False, True])
def test_legacy_pickle_artifact_is_retired_and_never_unpickled(tmp_path, monkeypatch, claims_current_format):
    root = tmp_path / "models"
    root.mkdir()
    sentinel = tmp_path / "unpickled"
    path = root / "challenger.joblib"
    path.write_bytes(pickle.dumps({"feature_schema_version": 2, "stages": Detonator(str(sentinel))}))

    def forbidden(*args, **kwargs):
        raise AssertionError("Serving attempted to unpickle a model artifact")

    monkeypatch.setattr(pickle, "loads", forbidden)
    monkeypatch.setattr(pickle, "load", forbidden)
    model = {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_schema_version": 2,
        "feature_order": ["local_hour"],
        **({"artifact_format": serving.FORMAT} if claims_current_format else {}),
    }
    training._MODELS.clear()
    assert training.predict_funnel_batch(model, [{"local_hour": 9}, {}], root) == [{}, {}]
    assert not sentinel.exists()
    expected = None if claims_current_format else serving.RETIRED
    assert training.retired_reason(model) == expected
    assert serving.RETIRED == "model_format_retired: retrain"


def valid_document():
    return {
        "format": serving.FORMAT,
        "feature_schema_version": 2,
        "stages": {
            "human": {
                "feature_order": ["local_hour", "attempt_count"],
                "vocabulary": {"segments": [], "concepts": []},
                "link": "logistic",
                "calibration": "sigmoid",
                "members": [
                    {
                        "baseline": 0.25,
                        "sigmoid": {"a": -1.0, "b": 0.0},
                        "trees": [
                            {
                                "feature": [0, 0, 0],
                                "threshold": [12.5, 0.0, 0.0],
                                "missing_left": [True, False, False],
                                "left": [1, 0, 0],
                                "right": [2, 0, 0],
                                "leaf": [False, True, True],
                                "value": [0.0, -0.5, 0.75],
                            }
                        ],
                    }
                ],
            }
        },
    }


def test_valid_document_scores_by_hand():
    stage = serving.parse(json.dumps(valid_document()).encode())["human"]
    expit = lambda z: 1 / (1 + math.exp(-z))  # noqa: E731 - the oracle formula
    assert stage.probabilities([[9.0, 0.0], [13.0, 1.0], [float("nan"), 2.0]]) == [
        expit(0.25 - 0.5),
        expit(0.25 + 0.75),
        expit(0.25 - 0.5),
    ]


def mutate(change):
    document = valid_document()
    stage = document["stages"]["human"]
    tree = stage["members"][0]["trees"][0]
    if change == "format":
        document["format"] = "joblib"
    elif change == "stage_name":
        document["stages"]["right_party"] = document["stages"].pop("human")
    elif change == "cycle":
        tree["left"][0] = 0
    elif change == "child_out_of_range":
        tree["right"][0] = 3
    elif change == "feature_out_of_range":
        tree["feature"][0] = 2
    elif change == "ragged":
        tree["value"].append(1.0)
    elif change == "boolean_index":
        tree["left"][0] = True
    elif change == "nan_threshold":
        tree["threshold"][0] = "nan"
    elif change == "string_value":
        tree["value"][1] = "0.5"
    elif change == "no_members":
        stage["members"] = []
    elif change == "link":
        stage["link"] = "identity"
    elif change == "too_many_trees":
        stage["members"][0]["trees"] = [tree] * (serving.MAX_TREES + 1)
    elif change == "too_many_nodes":
        size = serving.MAX_NODES_PER_TREE + 1
        tree.update(
            feature=[0] * size,
            threshold=[0.0] * size,
            missing_left=[False] * size,
            left=[0] * size,
            right=[0] * size,
            leaf=[True] * size,
            value=[0.0] * size,
        )
    elif change == "not_a_document":
        return b"[]"
    text = json.dumps(document)
    if change == "nan_constant":
        text = text.replace("0.75", "NaN")
    elif change == "infinite_constant":
        text = text.replace("0.75", "Infinity")
    return text.encode()


@pytest.mark.parametrize(
    "change",
    [
        "format",
        "stage_name",
        "cycle",
        "child_out_of_range",
        "feature_out_of_range",
        "ragged",
        "boolean_index",
        "nan_threshold",
        "string_value",
        "no_members",
        "link",
        "too_many_trees",
        "too_many_nodes",
        "not_a_document",
        "nan_constant",
        "infinite_constant",
    ],
)
def test_malformed_artifacts_are_rejected_and_fall_back(tmp_path, change):
    contents = mutate(change)
    with pytest.raises(serving.ArtifactError):
        serving.parse(contents)
    root = tmp_path / "models"
    root.mkdir()
    path = root / "challenger.json"
    path.write_bytes(contents)
    model = {
        "artifact": str(path),
        "checksum": hashlib.sha256(contents).hexdigest(),
        "artifact_format": serving.FORMAT,
        "feature_schema_version": 2,
    }
    training._MODELS.clear()
    assert training.predict_funnel_batch(model, [{"local_hour": 9}], root) == [{}]


def test_serving_bounds_are_unchanged():
    assert training.MAX_MODEL_BYTES == 64 * 1024 * 1024
    assert training.MODEL_CACHE_ENTRIES == 4
