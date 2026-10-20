"""Hand-built exported funnel artifacts: exact probabilities without fitting or unpickling."""

import hashlib
import json
import math

from app.ml import serving


def stage(trees, order, *, baseline=0.0, a=-1.0, b=0.0, vocabulary=None):
    return {
        "feature_order": list(order),
        "vocabulary": vocabulary or {"segments": [], "concepts": []},
        "link": "logistic",
        "calibration": "sigmoid",
        "members": [{"baseline": baseline, "sigmoid": {"a": a, "b": b}, "trees": trees}],
    }


def constant_stage(probability, order=("local_hour",), **kwargs):
    """No trees; a = -1 makes the probability expit(-b)."""
    return stage([], order, b=-math.log(probability / (1 - probability)), **kwargs)


def column_stage(order, **kwargs):
    """The first ordered column (0 or 1) is the probability; a missing value scores 0."""
    tree = {
        "feature": [0, 0, 0],
        "threshold": [0.5, 0.0, 0.0],
        "missing_left": [True, False, False],
        "left": [1, 0, 0],
        "right": [2, 0, 0],
        "leaf": [False, True, True],
        "value": [0.0, -1000.0, 1000.0],
    }
    return stage([tree], order, **kwargs)


def write(path, stages):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": serving.FORMAT, "feature_schema_version": 2, "stages": stages}))
    return {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "artifact_format": serving.FORMAT,
        "feature_schema_version": 2,
        "feature_order": next(iter(stages.values()))["feature_order"] if stages else [],
    }
