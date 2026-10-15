import re

DIMENSIONS = [
    "identity_relevance",
    "listening_response",
    "problem_discovery",
    "business_impact",
    "objection_handling",
    "next_step",
    "factual_accuracy",
]


def score_summary(items):
    valid = [
        i for i in items if i.get("status") == "SCORED" and i.get("evidence") and i.get("score") is not None
    ]
    weight = sum(i.get("weight", 1) for i in valid)
    return {
        "score": 100 * sum(i.get("weight", 1) * i["score"] / i.get("max_score", 2) for i in valid) / weight
        if weight
        else None,
        "coverage": len(valid) / len(items) if items else 0,
        "scorable_items": len(valid),
        "total_items": len(items),
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


def build_analysis(call_id, segments, *, outcome=None, transcript_version=1):
    stable = [s for s in segments if s.get("is_final") and not s.get("system_audio")]
    objections, commitments, problems = [], [], []
    for s in stable:
        text, evidence = s["text"], [s["segment_id"]]
        if s.get("side") == "remote":
            if re.search(r"another provider|too expensive|not interested|no budget|already use", text, re.I):
                objections.append({"text": text, "status": "unresolved", "evidence": evidence})
            if re.search(r"call (?:me )?back|next (?:tuesday|week)|send (?:me )?", text, re.I):
                commitments.append(
                    {
                        "owner": "rep",
                        "action": text,
                        "due_at": None,
                        "verbatim_time_expression": text,
                        "evidence": evidence,
                        "needs_time_confirmation": True,
                    }
                )
            if re.search(r"problem|struggling|need help|difficult", text, re.I):
                problems.append({"text": text, "evidence": evidence})
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
            "summary": " ".join(s["text"] for s in stable)[:1200] or "No final transcript is available.",
            "outcome": outcome or "unconfirmed",
            "problems": problems,
            "objections": objections,
            "commitments": commitments,
            "unknowns": ["Budget and appointment details require confirmation"],
            "suggested_crm_updates": [],
            "review_required": True,
        },
        "scorecard": {
            "rubric_version": "cold-introduction-v1",
            "items": items,
            "overall": score_summary(items),
            "compliance_flags": [],
        },
        "metrics": behavioral_metrics(stable),
        "provider": "extractive_baseline",
    }
