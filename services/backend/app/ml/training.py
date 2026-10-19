"""Bounded CPU funnel challengers with independent chronological calibration."""

import hashlib
import io
import pickle
import threading
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import brier_score_loss, log_loss

from app.optimization.learning import funnel_predictions
from app.optimization.numbering import timestamp
from app.scheduling.models import EstimatePolicy, prediction_key

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


REVIEW_POLICY = {
    "version": "rolling-review-v2",
    "minimum_rows": 100,
    "minimum_calibration_validation_class": 10,
    "minimum_bootstrap_groups": 30,
    "material_brier_regression": 0.01,
    "material_calibration_regression": 0.02,
    "bootstrap_resamples": 1000,
    "bootstrap_seed": 20261018,
    "objective": "conversion_given_right_party_when_all_folds_supported_else_human",
}
STAGES = {
    "human": "human",
    "right_party_given_human": "right_party",
    "conversion_given_right_party": "conversion",
}


def fitted_vocabulary(rows):
    segments, concepts = Counter(), Counter()
    for row in rows:
        features = row["features"]
        segment = str(features.get("segment") or "unknown")
        if segment != "unknown":
            segments[segment] += 1
        if not features.get("conversation_evidence_missing", True):
            concepts.update(set(features.get("conversation_concepts", [])))

    def top(counts, limit):
        return sorted(counts, key=lambda v: (-counts[v], v))[:limit]

    return {"segments": top(segments, 32), "concepts": top(concepts, 24)}


def feature_order(vocabulary, *, conversation):
    return [
        *FEATURE_ORDER[:10],
        "local_time_uncertain",
        *["segment:" + value for value in vocabulary["segments"]],
        "segment:unknown",
        *(
            [
                name
                for value in vocabulary["concepts"]
                for name in ("concept:" + value, "concept:" + value + ":missing")
            ]
            if conversation
            else []
        ),
    ]


def vector_v2(features, order, vocabulary):
    segment = str(features.get("segment") or "unknown")
    segment = segment if segment in vocabulary["segments"] else "unknown"
    concepts = (
        set(features.get("conversation_concepts", []))
        if not features.get("conversation_evidence_missing", True)
        else set()
    )
    values = []
    for name in order:
        if name.startswith("segment:"):
            value = float(name[8:] == segment)
        elif name.startswith("concept:"):
            missing = name.endswith(":missing")
            known = name[8:-8] in concepts if missing else name[8:] in concepts
            value = float(not known if missing else known)
        elif name == "number_health_missing":
            value = float(features.get(name, features.get("number_health") is None))
        elif name == "local_time_uncertain":
            value = float(features.get(name, features.get("local_hour") is None))
        else:
            value = features.get(name)
            value = float(value) if type(value) in (int, float, bool) else float("nan")
            if not np.isfinite(value):
                value = float("nan")
        values.append(value)
    return values


def label_time(row, label=None):
    at = timestamp(row["at"])
    observed = timestamp(row.get("outcome_available_at"))
    if not observed:
        return datetime.max.replace(tzinfo=UTC)
    if label == "conversion" or label is None and "conversion" in row:
        available, end = timestamp(row.get("label_available_at")), timestamp(row.get("window_ends_at"))
        return max(at, observed, available, end) if available and end else datetime.max.replace(tzinfo=UTC)
    return max(at, observed)


def linked_entities(rows):
    """Union transitive lead/endpoint/company links before any partition filtering."""
    parents, seen = list(range(len(rows))), {}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, row in enumerate(rows):
        for field in ("lead_id", "endpoint_id", "company_id"):
            value = row.get(field)
            if value in (None, "", "unknown"):
                continue
            key = field, value
            if key in seen:
                first, second = root(i), root(seen[key])
                parents[max(first, second)] = min(first, second)
            else:
                seen[key] = i
    return [root(i) for i in range(len(rows))]


def partition_at(rows, label, cutoffs):
    first, second, end = cutoffs
    groups = linked_entities(rows)
    earliest, result = {}, [[], [], []]
    dropped = {"late_or_unknown_labels": 0, "linked_later_rows": 0, "outside_horizon": 0}
    for row, group in zip(rows, groups, strict=True):
        at = timestamp(row["at"])
        slot = 0 if at < first else 1 if at < second else 2
        earliest[group] = min(slot, earliest.get(group, slot))
    for row, group in zip(rows, groups, strict=True):
        at = timestamp(row["at"])
        slot = 0 if at < first else 1 if at < second else 2
        if at >= end:
            dropped["outside_horizon"] += 1
        elif earliest[group] < slot:
            dropped["linked_later_rows"] += 1
        elif label_time(row, label) >= cutoffs[slot]:
            dropped["late_or_unknown_labels"] += 1
        else:
            result[slot].append(row)
    return result, dropped


def independent_partitions(rows, *, label=None):
    ordered = sorted(rows, key=lambda r: timestamp(r["at"]))
    if len(ordered) < 3:
        return [], [], []
    cut, calibration_cut = int(len(ordered) * 0.6), int(len(ordered) * 0.8)
    calibration_at = timestamp(ordered[cut]["at"])
    validation_at = timestamp(ordered[calibration_cut]["at"])
    # Legacy split reader has no evaluation as-of; new rolling fits always supply one.
    return tuple(
        partition_at(ordered, label, (calibration_at, validation_at, datetime.max.replace(tzinfo=UTC)))[0]
    )


def calibration_diagnostics(rows, probabilities, *, train, label="human"):
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
                    "observed_rate": sum(row[label] for row, _ in selected) / len(selected),
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
                "brier": sum((row[label] - p) ** 2 for row, p in selected) / len(selected),
                "seen_during_training": value in seen,
                "observed_rate": sum(row[label] for row, _ in selected) / len(selected),
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


def train_challenger(rows, output_dir, *, baseline=None):
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return _train_challenger(rows, output_dir, baseline=baseline)


def paired_bootstrap(candidate_losses, reference_losses, groups):
    """Paired attempt loss, resampling complete linked entities in bounded waves."""
    totals = defaultdict(lambda: [0.0, 0])
    for candidate, reference, group in zip(candidate_losses, reference_losses, groups, strict=True):
        totals[group][0] += float(candidate) - float(reference)
        totals[group][1] += 1
    values = np.asarray(list(totals.values()), dtype=float)
    result = {
        "unit": "linked_entity",
        "resamples": REVIEW_POLICY["bootstrap_resamples"],
        "seed": REVIEW_POLICY["bootstrap_seed"],
        "groups": len(totals),
        "samples": len(groups),
        "mean_difference": None,
        "interval": None,
    }
    if not totals:
        return result
    rng = np.random.default_rng(REVIEW_POLICY["bootstrap_seed"])
    means = []
    for start in range(0, result["resamples"], 25):
        indices = rng.integers(0, len(values), size=(min(25, result["resamples"] - start), len(values)))
        means.extend(values[indices, 0].sum(axis=1) / values[indices, 1].sum(axis=1))
    result.update(
        mean_difference=float(values[:, 0].sum() / values[:, 1].sum()),
        interval=[float(v) for v in np.quantile(means, [0.025, 0.975])],
    )
    return result


def stage_partitions(rows, label, cutoffs):
    parts, dropped = partition_at(rows, label, cutoffs)

    def eligible(row):
        if row.get(label) not in (0, 1) or row.get("human") not in (0, 1):
            return False
        if label != "human" and not row["human"]:
            return False
        return label != "conversion" or (
            row.get("right_party") == 1
            and (row.get("window_closed") or row.get("window_status") in {"matured", "resolved"})
        )

    selected = [[r for r in part if eligible(r)] for part in parts]
    dropped["unknown_or_outside_condition"] = sum(map(len, parts)) - sum(map(len, selected))
    return selected, dropped


def fit_stage(partitions, label, *, conversation):
    support = [Counter(int(r[label]) for r in part) for part in partitions]
    vocabulary = fitted_vocabulary(partitions[0])
    order = feature_order(vocabulary, conversation=conversation)
    report = {
        "status": "insufficient_independent_matured_data",
        "samples": sum(map(len, partitions)),
        "support": {
            name: {"positive": count[1], "negative": count[0]}
            for name, count in zip(("train", "calibration", "validation"), support, strict=True)
        },
        "calibration_min_class": min(support[1][0], support[1][1]),
        "validation_min_class": min(support[2][0], support[2][1]),
        "entity_overlap_count": 0,
        "vocabulary": vocabulary,
        "partition_policy": "as-of-grouped-embargo-v2",
    }
    if (
        report["samples"] < REVIEW_POLICY["minimum_rows"]
        or not all(support[0][v] for v in (0, 1))
        or min(report["calibration_min_class"], report["validation_min_class"])
        < REVIEW_POLICY["minimum_calibration_validation_class"]
    ):
        return None, report, []

    def xy(part):
        return np.asarray([vector_v2(r["features"], order, vocabulary) for r in part]), np.asarray(
            [int(r[label]) for r in part]
        )

    observed = np.isfinite(xy(partitions[0])[0]).any(axis=0)
    report["unobserved_columns"] = [name for name, keep in zip(order, observed, strict=True) if not keep]
    order = [name for name, keep in zip(order, observed, strict=True) if keep]
    estimator = HistGradientBoostingClassifier(
        max_iter=40, max_leaf_nodes=7, random_state=42, early_stopping=False
    )
    estimator.fit(*xy(partitions[0]))
    calibrated = CalibratedClassifierCV(FrozenEstimator(estimator), method="sigmoid")
    calibrated.fit(*xy(partitions[1]))
    xv, yv = xy(partitions[2])
    probabilities = calibrated.predict_proba(xv)[:, 1]
    report.update(
        status="fitted",
        brier=float(brier_score_loss(yv, probabilities)),
        log_loss=float(log_loss(yv, probabilities, labels=[0, 1])),
        validation_samples=len(yv),
        train_labels_through=max(label_time(r, label) for r in partitions[0]).isoformat(),
        calibration_from=min(timestamp(r["at"]) for r in partitions[1]).isoformat(),
        calibration_labels_through=max(label_time(r, label) for r in partitions[1]).isoformat(),
        validation_from=min(timestamp(r["at"]) for r in partitions[2]).isoformat(),
        validation_labels_through=max(label_time(r, label) for r in partitions[2]).isoformat(),
        calibration=calibration_diagnostics(partitions[2], probabilities, train=partitions[0], label=label),
    )
    return {"estimator": calibrated, "vocabulary": vocabulary, "feature_order": order}, report, probabilities


def rolling_cutoffs(rows):
    times = sorted({timestamp(r["at"]) for r in rows})
    if len(times) < 3:
        return []
    end = times[-1] + timedelta(microseconds=1)
    boundaries = []
    for fraction in (5, 6, 7):
        cutoffs = tuple(
            times[len(times) * index // 10] if index < 10 else end
            for index in (fraction, fraction + 1, fraction + 3)
        )
        if cutoffs[0] < cutoffs[1] < cutoffs[2] and cutoffs not in boundaries:
            boundaries.append(cutoffs)
    return boundaries


def baseline_probabilities(train, validation, stage, baseline):
    # The baseline sees only the earlier stage-eligible labels. Cache identical
    # feature cells within an offline fold; never read operational DBs here.
    cache, result = {}, []
    for row in validation:
        key = prediction_key(row["features"])
        if key not in cache:
            cache[key] = funnel_predictions(train, row["features"], **baseline)["p_" + stage]
        result.append(cache[key])
    return result


def _train_challenger(rows, output_dir, *, baseline=None):
    if len(rows) < REVIEW_POLICY["minimum_rows"]:
        raise ValueError("At least 100 outcome-labeled rows required for a challenger")
    baseline = baseline or {}
    baseline = {
        "estimate_policy": EstimatePolicy.model_validate(baseline.get("estimate_policy", {})).model_dump(
            mode="json"
        ),
        "handling_seconds": baseline.get("handling_seconds", 120),
    }
    funnel_predictions([], {}, **baseline)  # Validate even when every stage remains pooled.
    groups = linked_entities(rows)
    ordered = sorted(
        ({**r, "_row": i, "_entity": groups[i]} for i, r in enumerate(rows)), key=lambda r: timestamp(r["at"])
    )
    folds, final, evidence = [], {}, {name: {} for name in STAGES}
    for cutoffs in rolling_cutoffs(ordered):
        fold = dict(
            zip(
                ("training_cutoff", "calibration_cutoff", "evaluation_cutoff"),
                (v.isoformat() for v in cutoffs),
                strict=True,
            )
        )
        fold["stages"] = {}
        for stage, label in STAGES.items():
            partitions, dropped = stage_partitions(ordered, label, cutoffs)
            base_model, base_report, base_predictions = fit_stage(partitions, label, conversation=False)
            if base_model and base_model["vocabulary"]["concepts"]:
                full_model, full_report, full_predictions = fit_stage(partitions, label, conversation=True)
            else:
                full_model, full_report, full_predictions = base_model, dict(base_report), base_predictions
            report = {**full_report, "dropped": dropped, "without_conversation": base_report}
            if base_model:
                reference = baseline_probabilities(partitions[0], partitions[2], stage, baseline)
                reference_calibration = calibration_diagnostics(
                    partitions[2], reference, train=partitions[0], label=label
                )
                report["baseline_calibration_error"] = reference_calibration["expected_calibration_error"]
                report["baseline_brier"] = float(
                    brier_score_loss([r[label] for r in partitions[2]], reference)
                )
                for row, simple, rich, pooled in zip(
                    partitions[2], base_predictions, full_predictions, reference, strict=True
                ):
                    y = row[label]
                    # Overlapping rolling windows contribute each decision once:
                    # keep its latest valid out-of-time prediction.
                    evidence[stage][row["_row"]] = (
                        row["_entity"],
                        (y - rich) ** 2,
                        (y - simple) ** 2,
                        (y - pooled) ** 2,
                    )
            fold["stages"][stage] = report
            final[stage] = (base_model, full_model, report, partitions)
        fold["vocabulary"] = fold["stages"]["human"]["vocabulary"]
        folds.append(fold)

    models, metrics, comparisons, ablations = {}, {}, {}, {}
    for stage in STAGES:
        records = list(evidence[stage].values())
        groups = [r[0] for r in records]
        ablation = paired_bootstrap([r[1] for r in records], [r[2] for r in records], groups)
        calibration_ok = all(
            f["stages"][stage]["calibration"]["expected_calibration_error"]
            <= f["stages"][stage]["without_conversation"]["calibration"]["expected_calibration_error"]
            + REVIEW_POLICY["material_calibration_regression"]
            for f in folds
            if f["stages"][stage]["status"] == "fitted"
        )
        use_conversation = bool(
            ablation["interval"]
            and ablation["interval"][1] < 0
            and len(set(groups)) >= REVIEW_POLICY["minimum_bootstrap_groups"]
            and calibration_ok
        )
        ablations[stage] = {**ablation, "eligible_for_serving": use_conversation}
        comparison = paired_bootstrap(
            [r[1 if use_conversation else 2] for r in records], [r[3] for r in records], groups
        )
        comparisons[stage] = comparison
        if stage not in final:
            metrics[stage] = {
                "status": "insufficient_independent_matured_data",
                "reason": "distinct_decision_times",
                "samples": 0,
            }
            continue
        simple, rich, report, partitions = final[stage]
        metrics[stage] = {
            **(report if use_conversation else report["without_conversation"]),
            "dropped": report["dropped"],
            "baseline_brier": report.get("baseline_brier"),
            "baseline_calibration_error": report.get("baseline_calibration_error"),
            "uses_conversation": use_conversation,
            "bootstrap": comparison,
        }
        if simple:
            models[stage] = rich if use_conversation else simple

    objective = (
        "conversion_given_right_party"
        if folds and all(f["stages"]["conversion_given_right_party"]["status"] == "fitted" for f in folds)
        else "human"
    )
    objective_comparison = comparisons[objective]
    # These are declared engineering review tolerances, not a claim of lift.
    no_regression = all(
        c["interval"][1] <= REVIEW_POLICY["material_brier_regression"]
        for stage, c in comparisons.items()
        if stage in models and c["interval"]
    )
    calibration_ok = all(
        (r if ablations[stage]["eligible_for_serving"] else r["without_conversation"])["calibration"][
            "expected_calibration_error"
        ]
        <= r["baseline_calibration_error"] + REVIEW_POLICY["material_calibration_regression"]
        for f in folds
        for stage, r in f["stages"].items()
        if stage in models and r["status"] == "fitted"
    )
    complete = len(folds) == 3 and all(f["stages"][objective]["status"] == "fitted" for f in folds)
    reviewable = bool(
        complete
        and objective in models
        and objective_comparison["interval"]
        and objective_comparison["groups"] >= REVIEW_POLICY["minimum_bootstrap_groups"]
        and objective_comparison["interval"][1] < 0
        and no_regression
        and calibration_ok
    )
    vocabulary = final["human"][2]["vocabulary"] if final else {"segments": [], "concepts": []}
    artifact = {"feature_schema_version": 2, "stages": models, "review_policy": REVIEW_POLICY}
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "challenger.joblib"
    joblib.dump(artifact, path)
    human = metrics["human"]
    human_parts = final["human"][3] if final else ([], [], [])
    return {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_schema_version": 2,
        "baseline": baseline,
        "feature_order": models.get("human", {}).get("feature_order", []),
        "vocabulary": vocabulary,
        "rolling_evaluations": folds,
        "funnel_metrics": metrics,
        "train_through": human_parts[0][-1]["at"] if human_parts[0] else None,
        "validation_from": human_parts[2][0]["at"] if human_parts[2] else None,
        "calibration_samples": len(human_parts[1]),
        "validation_samples": len(human_parts[2]),
        "calibration": human.get("calibration", {}),
        "metrics": {
            "brier": human.get("brier"),
            "log_loss": human.get("log_loss"),
            "baseline_brier": human.get("baseline_brier"),
            "pooled_baseline_brier": human.get("baseline_brier"),
        },
        "review_policy": dict(REVIEW_POLICY),
        "reviewable": reviewable,
        "objective_stage": objective,
        "bootstrap": {**objective_comparison, "by_stage": comparisons},
        "pooled_stages": [name for name in STAGES if name not in models],
        "conversation_ablation": {
            "comparison": "same_folds_without_conversation_features",
            "eligible_for_serving": any(v["eligible_for_serving"] for v in ablations.values()),
            "stages": ablations,
        },
        "manual_approved": False,
        "online_trial_passed": False,
        "coverage_delta": 0,
        "policy_violations": 0,
        "evidence_scope": "Out-of-time predictive evaluation; no causal conversion-lift evidence",
    }


def promotion_allowed(model):
    if model.get("feature_schema_version", 1) == 2:
        return bool(
            model.get("reviewable")
            and model.get("manual_approved")
            and model.get("online_trial_passed")
            and model.get("coverage_delta", -1) >= 0
            and model.get("policy_violations", 1) == 0
        )
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
    if not features:
        return []
    try:
        path = Path(model["artifact"]).resolve()
        if not path.is_relative_to(Path(artifact_root).resolve()) or not path.is_file():
            return [{} for _ in features]
        # Only load owned artifacts produced by this authority. Verify and deserialize
        # the same bounded bytes; a second path open could observe a replacement.
        with _MODEL_LOCK:
            stat = path.stat()
            if stat.st_size > MAX_MODEL_BYTES:
                return [{} for _ in features]
            key = (
                str(path),
                model["checksum"],
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
                stat.st_ino,
            )
            if key not in _MODELS:
                with path.open("rb") as handle:
                    contents = handle.read(MAX_MODEL_BYTES + 1)
                if (
                    len(contents) > MAX_MODEL_BYTES
                    or hashlib.sha256(contents).hexdigest() != model["checksum"]
                ):
                    return [{} for _ in features]
                loaded = joblib.load(io.BytesIO(contents))
                if len(_MODELS) >= 4:
                    _MODELS.pop(next(iter(_MODELS)))
                _MODELS[key] = loaded
            models = _MODELS[key]
        if not isinstance(models, dict):
            models = {"human": models}
        schema = models.get("feature_schema_version", 1)
        if schema != model.get("feature_schema_version", 1) or schema not in (1, 2):
            return [{} for _ in features]
        estimators = models.get("stages", models)
        output = [{} for _ in features]
        for name, estimator in estimators.items():
            if schema == 2 and name in {"feature_schema_version", "vocabulary", "feature_order"}:
                continue
            if name not in {*STAGES, "right_party", "conversion"}:
                return [{} for _ in features]
            if schema == 2:
                packed = (
                    estimator
                    if "stages" in models
                    else {
                        "estimator": estimator,
                        "vocabulary": models["vocabulary"],
                        "feature_order": models.get("feature_order", model["feature_order"]),
                    }
                )
                values = [vector_v2(row, packed["feature_order"], packed["vocabulary"]) for row in features]
                estimator = packed["estimator"]
            else:
                values = [vector(row, model["feature_order"]) for row in features]
            matrix = np.asarray(estimator.predict_proba(values))
            if matrix.shape != (len(features), 2):
                return [{} for _ in features]
            probabilities = matrix[:, 1]
            if not np.all(np.isfinite(probabilities) & (probabilities >= 0) & (probabilities <= 1)):
                return [{} for _ in features]
            for row, probability in zip(output, probabilities, strict=True):
                row["p_" + name] = float(probability)
        return output
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        EOFError,
        pickle.UnpicklingError,
        ImportError,
        AttributeError,
    ):
        return [{} for _ in features]


def predict_funnel(model, features, artifact_root):
    return predict_funnel_batch(model, [features], artifact_root)[0]


def predict_challenger(model, features, artifact_root):
    return predict_funnel(model, features, artifact_root).get("p_human")


_MODELS = {}
_MODEL_LOCK = threading.Lock()
MAX_MODEL_BYTES = 64 * 1024 * 1024
