"""Reproducible inference-only replay; real-call usefulness needs a reviewed reference set."""

import asyncio
import math
import time

from app.coaching.engine import validate_card


async def evaluate(cases, provider, *, model, deadline_ms=1200):
    results = []
    for case in cases:
        started = time.perf_counter()
        valid, matched, failure = False, False, None
        try:
            result = await asyncio.wait_for(provider.suggest(case, deadline_ms), deadline_ms / 1000)
            validate_card(
                result,
                state_version=case["state_version"],
                segment_ids={s["segment_id"] for s in case["recent_dialogue"]},
                card_ids={c["id"] for c in case["approved_cards"]},
            )
            valid = True
            matched = result["action"] == case["expected_action"]
        except Exception as exc:
            failure = type(exc).__name__
        results.append(
            {
                "case_id": case["id"],
                "valid": valid,
                "expected_action": matched,
                "inference_ms": round((time.perf_counter() - started) * 1000, 3),
                "failure": failure,
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
        "p95_inference_ms": durations[max(0, math.ceil(0.95 * len(durations)) - 1)] if durations else None,
        "evidence_scope": "synthetic replay; excludes STT, network delivery and hardware audio",
        "results": results,
    }
