"""Pure-Python scoring of exported funnel models; no NumPy, scikit-learn, joblib or pickle.

The trainer exports each calibrated stage as plain JSON: per calibration member, the histogram
gradient boosting baseline (raw-score constant), its trees as parallel node arrays, and the sigmoid
calibration parameters. Scoring mirrors scikit-learn exactly: a node sends NaN to its learned
missing side and otherwise goes left when ``value <= threshold``; the raw score is the baseline
plus the leaf of every tree in fitting order; the member probability is
``expit(-(a * raw + b))``; and the stage probability is the mean over members.

Parsing validates everything before a model can score: the declared format, stage names, finite
numbers, consistent array lengths, in-range features, and child indices strictly after their
parent (so every walk terminates), within fixed tree and node bounds.
"""

import json
import math

FORMAT = "opendialer.funnel-trees.v1"
STAGE_NAMES = ("human", "right_party_given_human", "conversion_given_right_party")
MAX_MEMBERS = 16
MAX_TREES = 2000
MAX_NODES_PER_TREE = 8191
MAX_TOTAL_NODES = 2_000_000
MAX_FEATURES = 2048
MAX_VOCABULARY = 256
NODE_FIELDS = ("feature", "threshold", "missing_left", "left", "right", "leaf", "value")
INFINITE = {"inf": math.inf, "-inf": -math.inf}


class ArtifactError(ValueError):
    """The bytes are not a valid exported funnel model; the caller falls back to pooled."""


def encode_threshold(value):
    """JSON has no infinities; scikit-learn uses +inf for a split on missing values alone."""
    value = float(value)
    if math.isnan(value):
        raise ArtifactError("NaN split threshold")
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return value


def expit(z):
    """scipy.special.expit's formula, 1 / (1 + exp(-z)), with its limit for very negative z."""
    try:
        return 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0


class Stage:
    __slots__ = ("feature_order", "vocabulary", "members", "width")

    def __init__(self, feature_order, vocabulary, members):
        self.feature_order, self.vocabulary, self.members = feature_order, vocabulary, members
        self.width = len(feature_order)

    def probabilities(self, vectors):
        result = []
        for vector in vectors:
            if len(vector) != self.width:
                raise ArtifactError("Feature vector width differs from the model")
            total = 0.0
            for baseline, a, b, trees in self.members:
                raw = baseline
                for feature, threshold, missing_left, left, right, leaf, value in trees:
                    node = 0
                    while not leaf[node]:
                        x = vector[feature[node]]
                        if x != x:  # NaN
                            node = left[node] if missing_left[node] else right[node]
                        elif x <= threshold[node]:
                            node = left[node]
                        else:
                            node = right[node]
                    raw += value[node]
                total += expit(-(a * raw + b))
            result.append(total / len(self.members))
        return result


def _reject_constant(name):
    raise ArtifactError(f"Non-finite constant {name}")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ArtifactError("Expected a finite number")
    return float(value)


def _index(value, limit):
    if type(value) is not int or not 0 <= value < limit:
        raise ArtifactError("Index out of range")
    return value


def _flag(value):
    if type(value) is not bool:
        raise ArtifactError("Expected a boolean")
    return value


def _strings(values, limit):
    if not isinstance(values, list) or len(values) > limit or not all(type(v) is str for v in values):
        raise ArtifactError("Expected a bounded list of strings")
    return list(values)


def _tree(document, width, budget):
    if not isinstance(document, dict) or set(document) != set(NODE_FIELDS):
        raise ArtifactError("Tree fields differ from the format")
    columns = [document[name] for name in NODE_FIELDS]
    if not all(isinstance(column, list) for column in columns):
        raise ArtifactError("Tree node arrays must be lists")
    size = len(columns[0])
    if not 1 <= size <= MAX_NODES_PER_TREE or any(len(column) != size for column in columns):
        raise ArtifactError("Tree node arrays are empty, ragged or oversized")
    budget[0] -= size
    if budget[0] < 0:
        raise ArtifactError("Model node budget exceeded")
    feature, threshold, missing_left, left, right, leaf, value = ([0] * size for _ in NODE_FIELDS)
    for i in range(size):
        leaf[i] = _flag(document["leaf"][i])
        if leaf[i]:
            value[i] = _number(document["value"][i])
            continue
        feature[i] = _index(document["feature"][i], width)
        raw = document["threshold"][i]
        threshold[i] = INFINITE[raw] if type(raw) is str and raw in INFINITE else _number(raw)
        missing_left[i] = _flag(document["missing_left"][i])
        # Children strictly after their parent: no cycles, every walk ends at a leaf.
        left[i] = _index(document["left"][i], size)
        right[i] = _index(document["right"][i], size)
        if left[i] <= i or right[i] <= i:
            raise ArtifactError("Child index does not follow its parent")
    return tuple(tuple(column) for column in (feature, threshold, missing_left, left, right, leaf, value))


def stage_from_document(document, budget=None):
    budget = budget if budget is not None else [MAX_TOTAL_NODES]
    if not isinstance(document, dict):
        raise ArtifactError("Stage must be an object")
    if document.get("link") != "logistic" or document.get("calibration") != "sigmoid":
        raise ArtifactError("Unsupported link or calibration")
    order = _strings(document.get("feature_order"), MAX_FEATURES)
    vocabulary = document.get("vocabulary")
    if not isinstance(vocabulary, dict) or set(vocabulary) != {"segments", "concepts"}:
        raise ArtifactError("Vocabulary must name segments and concepts")
    vocabulary = {k: _strings(vocabulary[k], MAX_VOCABULARY) for k in ("segments", "concepts")}
    members = document.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        raise ArtifactError("Stage needs a bounded, non-empty member list")
    parsed = []
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("sigmoid"), dict):
            raise ArtifactError("Member must carry its sigmoid calibration")
        trees = member.get("trees")
        if not isinstance(trees, list) or len(trees) > MAX_TREES:
            raise ArtifactError("Member tree list is missing or oversized")
        parsed.append(
            (
                _number(member.get("baseline")),
                _number(member["sigmoid"].get("a")),
                _number(member["sigmoid"].get("b")),
                tuple(_tree(tree, len(order), budget) for tree in trees),
            )
        )
    return Stage(order, vocabulary, tuple(parsed))


def parse(contents):
    """Validated stages from artifact bytes; raises ArtifactError for anything else."""
    try:
        document = json.loads(contents, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ArtifactError("Artifact is not JSON") from error
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ArtifactError("Artifact is not an exported funnel model")
    stages = document.get("stages")
    if not isinstance(stages, dict) or not set(stages) <= set(STAGE_NAMES):
        raise ArtifactError("Artifact stages are not funnel stages")
    budget = [MAX_TOTAL_NODES]
    return {name: stage_from_document(stage, budget) for name, stage in stages.items()}
