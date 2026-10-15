import hashlib
from pathlib import Path

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss

FEATURE_ORDER = ["local_hour", "attempt_count", "number_health"]


def train_challenger(rows, output_dir):
    if len(rows) < 100:
        raise ValueError("At least 100 outcome-labeled rows required for a challenger")
    ordered = sorted(rows, key=lambda r: r["at"])
    cut = int(len(ordered) * 0.6)
    calibration_cut = int(len(ordered) * 0.8)
    train = ordered[:cut]
    calibration = ordered[cut:calibration_cut]
    validation = ordered[calibration_cut:]
    # Remove repeated entities across chronological partitions to prevent endpoint/lead leakage.
    train_ids = {r["endpoint_id"] for r in train} | {r["lead_id"] for r in train}
    calibration = [
        r for r in calibration if r["endpoint_id"] not in train_ids and r["lead_id"] not in train_ids
    ]
    prior_ids = train_ids | {r["endpoint_id"] for r in calibration} | {r["lead_id"] for r in calibration}
    validation = [
        r for r in validation if r["endpoint_id"] not in prior_ids and r["lead_id"] not in prior_ids
    ]
    if not calibration or not validation:
        raise ValueError("Insufficient independent chronological calibration/validation rows")

    def xy(part):
        return np.array([[float(r["features"].get(k, 0)) for k in FEATURE_ORDER] for r in part]), np.array(
            [int(r["human"]) for r in part]
        )

    x, y = xy(train)
    model = HistGradientBoostingClassifier(max_iter=40, max_leaf_nodes=7, random_state=42)
    model.fit(x, y)
    from sklearn.frozen import FrozenEstimator

    calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
    calibrated.fit(*xy(calibration))
    xv, yv = xy(validation)
    probabilities = calibrated.predict_proba(xv)[:, 1]
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "challenger.joblib"
    joblib.dump(calibrated, path)
    baseline = (int(y.sum()) + 2) / (len(y) + 4)
    return {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "train_through": train[-1]["at"],
        "validation_from": validation[0]["at"],
        "calibration_samples": len(calibration),
        "validation_samples": len(validation),
        "feature_order": FEATURE_ORDER,
        "metrics": {
            "brier": float(brier_score_loss(yv, probabilities)),
            "log_loss": float(log_loss(yv, probabilities)),
            "baseline_brier": float(brier_score_loss(yv, [baseline] * len(yv))),
        },
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
        and model.get("metrics", {}).get("brier", 1) < model.get("metrics", {}).get("baseline_brier", 0)
    )
