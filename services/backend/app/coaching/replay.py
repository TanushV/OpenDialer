"""Reproducible inference-only replay; real-call usefulness needs a reviewed reference set."""

import asyncio
import copy
import hashlib
import json
import math
import time

from app.coaching.context import (
    MeaningReviewRequired,
    ground_guidance,
    guidance_review_request,
    guidance_sources,
)
from app.coaching.contracts import card_view
from app.grounding import accept_review

CONTEXT_FIELDS = {
    "id",
    "state_version",
    "state",
    "recent_dialogue",
    "automatic_tips_muted",
    "manual_help",
    "private_question",
    "approved_cards",
    "reviewed_examples",
    "locally_retrieved_card_ids",
    "approved_claims",
    "prohibited_claims",
    "facts",
    "seller",
    "prepared_script",
    "lead_context",
    "prior_commitments",
    "prior_call_context",
    "objective",
    "conversation_learning",
}


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def validate_cases(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 200 or len(encoded(cases)) > 524288:
        raise ValueError("Replay needs 1–200 cases within 512 KiB")
    identities = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or not 0 < len(case["id"]) <= 128
            or case["id"] in identities
            or case.get("expected_action") not in ("SHOW_CARD", "NO_ACTION")
            or type(case.get("state_version")) is not int
            or case["state_version"] < 0
            or len(encoded(case)) > 65536
        ):
            raise ValueError("Replay cases need unique IDs, valid expectations and at most 64 KiB each")
        identities.add(case["id"])
        for field in (
            "expected_card_ids",
            "approved_claims",
            "prohibited_claims",
            "locally_retrieved_card_ids",
        ):
            values = case.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > 128
                or not all(isinstance(v, str) for v in values)
            ):
                raise ValueError(f"{field} must be a bounded list of text")
        for field in (
            "recent_dialogue",
            "approved_cards",
            "reviewed_examples",
            "facts",
            "prior_commitments",
            "prior_call_context",
        ):
            values = case.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > 128
                or not all(isinstance(v, dict) for v in values)
            ):
                raise ValueError(f"{field} must be a bounded list of objects")
        for field in ("state", "seller", "prepared_script", "lead_context", "conversation_learning"):
            if field in case and not isinstance(case[field], dict):
                raise ValueError(f"{field} must be an object")
        for field, identity in (("recent_dialogue", "segment_id"), ("approved_cards", "id")):
            if any(
                not isinstance(v.get(identity), str) or not isinstance(v.get("text"), str)
                for v in case.get(field, [])
            ):
                raise ValueError(f"{field} needs text and source identities")
        if not isinstance(case.get("seller", {}).get("knowledge", ""), str):
            raise ValueError("Seller knowledge must be text")


async def evaluate(cases, provider, *, model, deadline_ms=1200, max_duration_ms=60000):
    validate_cases(cases)
    if not 0 < max_duration_ms <= 60000 or not 0 < deadline_ms <= 8000:
        raise ValueError("Replay deadlines exceed the bounded evaluation window")
    results = []
    expires = time.perf_counter() + max_duration_ms / 1000
    for case in cases:
        started = time.perf_counter()
        request = {key: value for key, value in case.items() if key in CONTEXT_FIELDS}
        request["context_fingerprint"] = hashlib.sha256(encoded(request)).hexdigest()
        request["evidence_catalog"] = guidance_sources(request)
        valid, matched, failure, expected_card = False, False, None, None
        review_metadata = None
        budget = min(deadline_ms / 1000, expires - started)
        attempted, output_hash = budget > 0, None
        if attempted:
            try:
                result = await asyncio.wait_for(
                    provider.suggest(copy.deepcopy(request), max(1, int(budget * 1000))), budget
                )
                output_hash = hashlib.sha256(encoded(result)).hexdigest()
                # Cooperative cancellation can return late; that is never useful-in-time evidence.
                if time.perf_counter() >= started + budget:
                    raise TimeoutError("Replay response exceeded its deadline")
                result = card_view(result)
                try:
                    ground_guidance(result, request, cached=bool(getattr(provider, "seller_approved", False)))
                except MeaningReviewRequired:
                    if not hasattr(provider, "review"):
                        raise
                    review = guidance_review_request(result, request)
                    generation = {
                        name: getattr(provider, name, None)
                        for name in ("last_usage", "last_request_id", "last_provider", "last_model")
                    }
                    try:
                        left = started + budget - time.perf_counter()
                        if left <= 0:
                            raise TimeoutError("No remaining review budget")
                        checked = await asyncio.wait_for(
                            provider.review(copy.deepcopy(review), max(1, int(left * 1000))), left
                        )
                        if time.perf_counter() >= started + budget:
                            raise TimeoutError("Review exceeded the original replay deadline")
                        receipt = accept_review(review, checked, getattr(provider, "last_request_id", None))
                        ground_guidance(result, request, review=receipt)
                        review_metadata = receipt.metadata()
                    finally:
                        for name, value in generation.items():
                            setattr(provider, name, value)
                valid = True
                matched = result["action"] == case["expected_action"]
                if "expected_card_ids" in case:
                    actual = set(result.get("playbook_card_ids", []))
                    expected = set(case["expected_card_ids"])
                    expected_card = (
                        bool(actual) and actual.issubset(expected)
                        if expected
                        else result["action"] == "NO_ACTION"
                    )
            except Exception as exc:
                failure = type(exc).__name__
        else:
            failure = "EvaluationDeadline"
        results.append(
            {
                "case_id": case["id"],
                "attempted": attempted,
                "context_sha256": hashlib.sha256(encoded(request)).hexdigest(),
                "output_sha256": output_hash,
                "valid": valid,
                "expected_action": matched,
                "inference_ms": round((time.perf_counter() - started) * 1000, 3) if attempted else None,
                "failure": failure,
                "expected_card": expected_card,
                "meaning_review": review_metadata,
                "provider": getattr(provider, "last_provider", None) if attempted else None,
                "model": getattr(provider, "last_model", model) if attempted else None,
                "request_id": getattr(provider, "last_request_id", None) if attempted else None,
                "usage": {
                    key: value
                    for key, value in (getattr(provider, "last_usage", {}) or {}).items()
                    if attempted
                    and key in {"prompt_tokens", "completion_tokens", "total_tokens", "cost"}
                    and type(value) in {int, float}
                    and math.isfinite(value)
                    and value >= 0
                },
            }
        )
    durations = sorted(r["inference_ms"] for r in results if r["inference_ms"] is not None)
    return {
        "model": model,
        "samples": len(results),
        "attempted_samples": len(durations),
        "valid_samples": sum(r["valid"] for r in results),
        "unattempted_samples": len(results) - len(durations),
        "semantic_quality": "not_reviewed",
        "valid_fraction": sum(r["valid"] for r in results) / len(results) if results else None,
        "expected_action_fraction": sum(r["expected_action"] for r in results) / len(results)
        if results
        else None,
        "expected_card_fraction": (
            sum(bool(r["expected_card"]) for r in results if r["expected_card"] is not None)
            / sum(r["expected_card"] is not None for r in results)
            if any(r["expected_card"] is not None for r in results)
            else None
        ),
        "p95_inference_ms": durations[max(0, math.ceil(0.95 * len(durations)) - 1)] if durations else None,
        "evidence_scope": "synthetic replay; excludes STT, network delivery and hardware audio",
        "results": results,
    }
