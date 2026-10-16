"""Reproducible inference-only replay; real-call usefulness needs a reviewed reference set."""

import asyncio
import math
import time

from app.coaching.context import ground_guidance


async def evaluate(cases, provider, *, model, deadline_ms=1200):
    results = []
    for case in cases:
        started = time.perf_counter()
        valid, matched, failure, expected_card = False, False, None, None
        try:
            result = await asyncio.wait_for(provider.suggest(case, deadline_ms), deadline_ms / 1000)
            ground_guidance(result, case)
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
        results.append(
            {
                "case_id": case["id"],
                "valid": valid,
                "expected_action": matched,
                "inference_ms": round((time.perf_counter() - started) * 1000, 3),
                "failure": failure,
                "expected_card": expected_card,
                "provider": getattr(provider, "last_provider", None),
                "model": getattr(provider, "last_model", model),
                "request_id": getattr(provider, "last_request_id", None),
                "usage": getattr(provider, "last_usage", {}),
            }
        )
    durations = sorted(r["inference_ms"] for r in results)
    return {
        "model": model,
        "samples": len(results),
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
