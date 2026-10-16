"""Bounded CPU funnel challengers with independent chronological calibration."""

import hashlib
from pathlib import Path

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import brier_score_loss, log_loss

from app.optimization.learning import funnel_predictions

FEATURE_ORDER = [
    "local_hour",
    "local_weekday",
    "attempt_count",
    "number_health",
    "number_health_missing",
    "source_age_days",
    "elapsed_hours",
    "endpoint_evidence",
    "callback_history",
    "requested_contact",
    *[f"segment_bucket_{i}" for i in range(8)],
]


def vector(features, order):
    bucket = int(hashlib.sha256(str(features.get("segment", "unknown")).encode()).hexdigest()[:8], 16) % 8
    return [
        float(int(name.rsplit("_", 1)[1]) == bucket)
        if name.startswith("segment_bucket_")
        else float(features.get(name) or 0)
        for name in order
    ]


def independent_partitions(rows):
    ordered = sorted(rows, key=lambda r: r["at"])
    cut, calibration_cut = int(len(ordered) * 0.6), int(len(ordered) * 0.8)
    train, calibration, validation = ordered[:cut], ordered[cut:calibration_cut], ordered[calibration_cut:]
    train_ids = {r["endpoint_id"] for r in train} | {r["lead_id"] for r in train}
    calibration = [
        r for r in calibration if r["endpoint_id"] not in train_ids and r["lead_id"] not in train_ids
    ]
    prior = train_ids | {r["endpoint_id"] for r in calibration} | {r["lead_id"] for r in calibration}
    validation = [r for r in validation if r["endpoint_id"] not in prior and r["lead_id"] not in prior]
    return train, calibration, validation


def train_challenger(rows, output_dir):
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return _train_challenger(rows, output_dir)


def _train_challenger(rows, output_dir):
    if len(rows) < 100:
        raise ValueError("At least 100 outcome-labeled rows required for a challenger")
    train, calibration, validation = independent_partitions(rows)
    if not calibration or not validation:
        raise ValueError("Insufficient independent chronological calibration/validation rows")

    def fit_stage(partitions, label):
        if any(len(part) < 5 or len({int(r[label]) for r in part}) < 2 for part in partitions):
            return None

        def xy(part):
            return np.array([vector(r["features"], FEATURE_ORDER) for r in part]), np.array(
                [int(r[label]) for r in part]
            )

        model = HistGradientBoostingClassifier(max_iter=40, max_leaf_nodes=7, random_state=42)
        model.fit(*xy(partitions[0]))
        calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
        calibrated.fit(*xy(partitions[1]))
        xv, yv = xy(partitions[2])
        probabilities = calibrated.predict_proba(xv)[:, 1]
        return calibrated, {
            "brier": float(brier_score_loss(yv, probabilities)),
            "log_loss": float(log_loss(yv, probabilities, labels=[0, 1])),
            "validation_samples": len(yv),
        }

    human = fit_stage((train, calibration, validation), "human")
    if human is None:
        raise ValueError("Independent human-answer classes are required in every partition")
    models, metrics = {"human": human[0]}, {"human": human[1]}
    for name, label, predicate in [
        (
            "right_party_given_human",
            "right_party",
            lambda r: r.get("human") and r.get("right_party") is not None,
        ),
        (
            "conversion_given_right_party",
            "conversion",
            lambda r: r.get("right_party") and r.get("window_closed") and r.get("conversion") is not None,
        ),
    ]:
        partitions = tuple([r for r in part if predicate(r)] for part in (train, calibration, validation))
        result = fit_stage(partitions, label) if sum(map(len, partitions)) >= 100 else None
        if result:
            models[name], metrics[name] = result
        else:
            metrics[name] = {
                "status": "insufficient_independent_matured_data",
                "samples": sum(map(len, partitions)),
            }
    history = [
        {
            **r,
            "segment": r["features"].get("segment", "unknown"),
            "local_hour": r["features"].get("local_hour", 12),
            "outcome": r.get("outcome", "right_party" if r["human"] else "no_answer"),
        }
        for r in train
    ]
    baseline = [funnel_predictions(history, r["features"])["p_human"] for r in validation]
    baseline_brier = float(brier_score_loss([r["human"] for r in validation], baseline))
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "challenger.joblib"
    joblib.dump(models, path)
    return {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "train_through": train[-1]["at"],
        "validation_from": validation[0]["at"],
        "calibration_samples": len(calibration),
        "validation_samples": len(validation),
        "feature_order": FEATURE_ORDER,
        "funnel_metrics": metrics,
        "metrics": {**human[1], "baseline_brier": baseline_brier, "pooled_baseline_brier": baseline_brier},
        "manual_approved": False,
        "online_trial_passed": False,
        "coverage_delta": 0,
        "policy_violations": 0,
    }


def promotion_allowed(model):
    return bool(
        model.get("manual_approved")
        and model.get("online_trial_passed")
        and model.get("coverage_delta", -1) >= 0
        and model.get("policy_violations", 1) == 0
        and model.get("metrics", {}).get("brier", 1)
        < model.get("metrics", {}).get(
            "pooled_baseline_brier", model.get("metrics", {}).get("baseline_brier", 0)
        )
    )


def predict_funnel(model, features, artifact_root):
    try:
        path = Path(model["artifact"]).resolve()
        if not path.is_relative_to(Path(artifact_root).resolve()) or not path.is_file():
            return {}
        if hashlib.sha256(path.read_bytes()).hexdigest() != model["checksum"]:
            return {}
        key = (str(path), model["checksum"])
        if key not in _MODELS:
            _MODELS.clear()
            _MODELS[key] = joblib.load(path)
        models = _MODELS[key]
        if not isinstance(models, dict):
            models = {"human": models}  # Existing 0.1.x artifacts stay readable.
        values = [vector(features, model["feature_order"])]
        return {
            "p_" + name: float(estimator.predict_proba(values)[0][1]) for name, estimator in models.items()
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def predict_challenger(model, features, artifact_root):
    return predict_funnel(model, features, artifact_root).get("p_human")


_MODELS = {}
