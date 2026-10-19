"""Frozen evaluation data and blinded review; these do not certify model quality."""

import json
from collections import Counter
from pathlib import Path

from app.coaching.replay import evaluate, validate_cases

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "ordinary",
    "unfamiliar_objection",
    "negation",
    "false_premise",
    "missing_fact",
    "numeric",
    "changed_answer",
    "partial_stt",
    "source_correction",
    "opt_out",
    "wrong_party",
    "short_noop",
}


def cases():
    return [
        json.loads(line)
        for line in (ROOT / "packages/evaluation-fixtures/coach.jsonl").read_text().splitlines()
    ]


def test_frozen_semantic_matrix_has_120_distinct_contexts_and_complete_review_contract():
    values = cases()
    validate_cases(values)
    assert len(values) == 120
    assert len({json.dumps(v["recent_dialogue"], sort_keys=True) for v in values}) == 120
    counts = Counter(tag for v in values for tag in v["categories"])
    assert all(counts[tag] >= 10 for tag in CATEGORIES)
    assert all(v["review_criteria"] and v["synthetic"] is True for v in values)
    spec = json.loads((ROOT / "packages/evaluation-fixtures/review-contract.json").read_text())
    assert len(set(spec["overlap_case_ids"])) == 30
    assert set(spec["overlap_case_ids"]) <= {v["id"] for v in values}
    assert spec["required_independent_reviewers"] == 2
    assert spec["quality_status"] == "not_reviewed"
    assert set(spec["dimensions"]) == {
        "groundedness",
        "usefulness",
        "distraction",
        "correct_silence",
        "timing",
    }


async def test_all_review_targets_are_withheld_for_the_entire_matrix():
    seen = []

    class Observer:
        async def suggest(self, request, deadline_ms):
            assert not {
                "categories",
                "synthetic",
                "review_criteria",
                "expected_action",
                "reviewed_answer",
            }.intersection(request)
            seen.append(request["id"])
            return {"action": "NO_ACTION"}

    result = await evaluate(cases(), Observer(), model="synthetic-contract")
    assert result["attempted_samples"] == 120
    assert len(set(seen)) == 120
    assert result["semantic_quality"] == "not_reviewed"
    assert result["valid_samples"] == 120
