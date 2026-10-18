DIMENSIONS = [
    "identity_relevance",
    "listening_response",
    "problem_discovery",
    "business_impact",
    "objection_handling",
    "next_step",
    "factual_accuracy",
]


def score_summary(items, dimensions=None):
    dimensions = dimensions or (DIMENSIONS if any("dimension" in i for i in items) else None)
    total = len(dimensions) if dimensions else len(items)
    if dimensions:
        # Legacy/partial items cannot inflate coverage or count a repeated dimension twice.
        items = list({i["dimension"]: i for i in items if i.get("dimension") in dimensions}.values())
    valid = [
        i for i in items if i.get("status") == "SCORED" and i.get("evidence") and i.get("score") is not None
    ]
    weight = sum(i.get("weight", 1) for i in valid)
    return {
        "score": 100 * sum(i.get("weight", 1) * i["score"] / i.get("max_score", 2) for i in valid) / weight
        if weight
        else None,
        "coverage": len(valid) / total if total else 0,
        "scorable_items": len(valid),
        "total_items": total,
    }


def behavioral_metrics(segments):
    timed = [
        s
        for s in segments
        if s.get("start_ms") is not None
        and s.get("end_ms") is not None
        and s.get("side") in {"rep", "remote"}
        and not s.get("system_audio")
    ]
    duration = {
        side: sum(max(0, s["end_ms"] - s["start_ms"]) for s in timed if s["side"] == side)
        for side in ("rep", "remote")
    }
    total = sum(duration.values())
    ordered = sorted(timed, key=lambda s: s["start_ms"])
    gaps = [
        max(0, b["start_ms"] - a["end_ms"])
        for a, b in zip(ordered, ordered[1:], strict=False)
        if a["side"] != b["side"]
    ]
    overlap = sum(
        max(0, min(a["end_ms"], b["end_ms"]) - max(a["start_ms"], b["start_ms"]))
        for i, a in enumerate(ordered)
        for b in ordered[i + 1 :]
        if a["side"] != b["side"]
    )
    return {
        "timed_segments": len(timed),
        "total_segments": len(segments),
        "rep_talk_ratio": duration["rep"] / total if total else None,
        "talk_ms": duration,
        "longest_monologue_ms": max((s["end_ms"] - s["start_ms"] for s in timed), default=None),
        "question_count": sum(s.get("text", "").count("?") for s in segments if s.get("side") == "rep"),
        "turn_exchanges": sum(a["side"] != b["side"] for a, b in zip(ordered, ordered[1:], strict=False)),
        "overlap_ms": overlap if timed else None,
        "response_gaps_ms": gaps,
    }


def build_analysis(call_id, segments, *, outcome=None, transcript_version=1, derived=None):
    stable = [s for s in segments if s.get("is_final") and not s.get("system_audio")]
    derived = derived or {}
    objections = derived.get("objections", [])
    commitments = derived.get("commitments", [])
    problems = derived.get("needs", [])
    # Conservative extraction-only baseline. No manufactured ratings when an evaluated inference model is absent.
    items = [
        {
            "dimension": d,
            "score": None,
            "max_score": 2,
            "weight": 1,
            "status": "N/A"
            if outcome in {"wrong_party", "voicemail", "no_answer", "ivr", "screening"}
            else "INSUFFICIENT_EVIDENCE",
            "evidence": [],
            "explanation": "Human review or an evaluated model is required.",
        }
        for d in DIMENSIONS
    ]
    return {
        "notes": {
            "call_id": call_id,
            "transcript_version": transcript_version,
            "summary": derived.get("summary")
            or "No model rundown is available. Review the recorded outcome and your notes.",
            "outcome": outcome or "unconfirmed",
            "problems": problems,
            "objections": objections,
            "commitments": commitments,
            "unknowns": ["Uninterpreted conversation details remain unknown"],
            "review_required": True,
        },
        "scorecard": {
            "rubric_version": "cold-introduction-v1",
            "transcript_version": transcript_version,
            "items": items,
            "overall": score_summary(items),
            "compliance_flags": [],
        },
        "metrics": behavioral_metrics(stable),
        "provider": "derived_state_or_unavailable",
    }
