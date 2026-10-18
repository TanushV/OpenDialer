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
from app.optimization.numbering import timestamp

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


def label_time(row, label=None):
    at = timestamp(row["at"])
    if label == "conversion" or label is None and "conversion" in row:
        return max(
            at,
            timestamp(row.get("outcome_available_at")) or at,
            timestamp(row.get("label_available_at")) or at,
            timestamp(row.get("window_ends_at")) or at,
        )
    return max(at, timestamp(row.get("outcome_available_at")) or at)


def independent_partitions(rows, *, label=None):
    ordered = sorted(rows, key=lambda r: timestamp(r["at"]))
    if len(ordered) < 3:
        return [], [], []
    cut, calibration_cut = int(len(ordered) * 0.6), int(len(ordered) * 0.8)
    calibration_at = timestamp(ordered[cut]["at"])
    validation_at = timestamp(ordered[calibration_cut]["at"])
    train = [
        r for r in ordered if timestamp(r["at"]) < calibration_at and label_time(r, label) < calibration_at
    ]
    calibration = [
        r
        for r in ordered
        if calibration_at <= timestamp(r["at"]) < validation_at and label_time(r, label) < validation_at
    ]
    validation = [r for r in ordered if timestamp(r["at"]) >= validation_at]

    def entities(partition):
        return {(field, r[field]) for r in partition for field in ("endpoint_id", "lead_id")}

    train_ids = entities(train)
    calibration = [r for r in calibration if not entities([r]) & train_ids]
    prior = train_ids | entities(calibration)
    validation = [r for r in validation if not entities([r]) & prior]
    return train, calibration, validation


def calibration_diagnostics(rows, probabilities, *, train):
    pairs = list(zip(rows, probabilities, strict=True))
    bins = []
    for index in range(10):
        selected = [
            (row, probability) for row, probability in pairs if min(9, int(probability * 10)) == index
        ]
        if selected:
            bins.append(
                {
                    "lower": index / 10,
                    "upper": (index + 1) / 10,
                    "samples": len(selected),
                    "mean_prediction": sum(p for _, p in selected) / len(selected),
                    "observed_rate": sum(row["human"] for row, _ in selected) / len(selected),
                }
            )
    subgroups = {}
    for field in ("segment", "rep_id", "number_id"):

        def group(row, field=field):
            return str(
                row.get("features", {}).get(field, "unknown")
                if field == "segment"
                else row.get(field, "unknown")
            )

        seen = {group(row) for row in train}
        groups = sorted({group(row) for row in rows})
        subgroups[field] = {}
        for value in groups[:100]:
            selected = [(row, p) for row, p in pairs if group(row) == value]
            subgroups[field][value] = {
                "samples": len(selected),
                "brier": sum((row["human"] - p) ** 2 for row, p in selected) / len(selected),
                "seen_during_training": value in seen,
                "observed_rate": sum(row["human"] for row, _ in selected) / len(selected),
            }
    return {
        "bins": bins,
        "subgroups": subgroups,
        "expected_calibration_error": sum(
            b["samples"] * abs(b["mean_prediction"] - b["observed_rate"]) for b in bins
        )
        / len(rows)
        if rows
        else None,
        "evidence_scope": "Out-of-time held-out groups; small groups are descriptive and do not establish generalization",
    }


def train_challenger(rows, output_dir):
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return _train_challenger(rows, output_dir)


def _train_challenger(rows, output_dir):
    if len(rows) < 100:
        raise ValueError("At least 100 outcome-labeled rows required for a challenger")
    train, calibration, validation = independent_partitions(rows, label="human")
    if not calibration or not validation:
        raise ValueError("Insufficient independent chronological calibration/validation rows")

    def fit_stage(partitions, label):
        if any(len(part) < 5 or len({int(r[label]) for r in part}) < 2 for part in partitions):
            return None

        def xy(part):
            return np.array([vector(r["features"], FEATURE_ORDER) for r in part]), np.array(
                [int(r[label]) for r in part]
            )

        model = HistGradientBoostingClassifier(
            max_iter=40, max_leaf_nodes=7, random_state=42, early_stopping=False
        )
        model.fit(*xy(partitions[0]))
        calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
        calibrated.fit(*xy(partitions[1]))
        xv, yv = xy(partitions[2])
        probabilities = calibrated.predict_proba(xv)[:, 1]
        return calibrated, {
            "brier": float(brier_score_loss(yv, probabilities)),
            "log_loss": float(log_loss(yv, probabilities, labels=[0, 1])),
            "validation_samples": len(yv),
            "partition_policy": "as-of-grouped-embargo-v1",
            "train_labels_through": max(label_time(r, label) for r in partitions[0]).isoformat(),
            "calibration_from": min(timestamp(r["at"]) for r in partitions[1]).isoformat(),
            "calibration_labels_through": max(label_time(r, label) for r in partitions[1]).isoformat(),
            "validation_from": min(timestamp(r["at"]) for r in partitions[2]).isoformat(),
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
        partitions = tuple(
            [r for r in part if predicate(r)] for part in independent_partitions(rows, label=label)
        )
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
    probabilities = human[0].predict_proba(
        np.array([vector(r["features"], FEATURE_ORDER) for r in validation])
    )[:, 1]
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
        "calibration": calibration_diagnostics(validation, probabilities, train=train),
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


def predict_funnel_batch(model, features, artifact_root):
    try:
        path = Path(model["artifact"]).resolve()
        if not path.is_relative_to(Path(artifact_root).resolve()) or not path.is_file():
            return [{} for _ in features]
        stat = path.stat()
        key = (str(path), model["checksum"], stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if key not in _MODELS:
            if hashlib.sha256(path.read_bytes()).hexdigest() != model["checksum"]:
                return [{} for _ in features]
            if len(_MODELS) >= 4:
                _MODELS.clear()
            _MODELS[key] = joblib.load(path)
        models = _MODELS[key]
        if not isinstance(models, dict):
            models = {"human": models}
        if not features:
            return []
        values = [vector(row, model["feature_order"]) for row in features]
        output = [{} for _ in features]
        for name, estimator in models.items():
            probabilities = estimator.predict_proba(values)[:, 1]
            for row, probability in zip(output, probabilities, strict=True):
                row["p_" + name] = float(probability)
        return output
    except (OSError, ValueError, KeyError, TypeError):
        return [{} for _ in features]


def predict_funnel(model, features, artifact_root):
    return predict_funnel_batch(model, [features], artifact_root)[0]


def predict_challenger(model, features, artifact_root):
    return predict_funnel(model, features, artifact_root).get("p_human")


_MODELS = {}
