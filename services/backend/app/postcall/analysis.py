DIMENSIONS = [
    "identity_relevance",
    "listening_response",
    "problem_discovery",
    "business_impact",
    "objection_handling",
    "next_step",
    "factual_accuracy",
]


def reviewed_tracker_matches(note, definitions, source_revisions, live_ids):
    known = {item["id"]: item for item in definitions}
    result = []
    for match in note.get("semantic_trackers", []):
        definition = known.get(match.get("tracker_id"))
        revisions = match.get("evidence_revisions", {})
        if not definition or match.get("version") != definition["version"] or not revisions:
            continue
        if any(source_revisions.get(identity) != revision for identity, revision in revisions.items()):
            continue
        result.append(
            {**match, "raw_evidence_status": "available" if set(revisions) <= live_ids else "expired"}
        )
    return result


def tracker_results(matches, original, current, segments, previous):
    """Retain reviewed old chunks, replace current evidence, and reject changed definitions."""
    versions = {t["id"]: t for t in original}
    current_versions = {t["id"]: t["version"] for t in current}
    source = {s["segment_id"]: s for s in segments}
    retained = [m for m in previous if not set(m.get("evidence", [])) <= set(source)]
    for item in matches:
        definition = versions[item["tracker_id"]]
        if current_versions.get(item["tracker_id"]) == definition["version"]:
            retained.append(
                {
                    **item,
                    "name": definition["name"],
                    "kind": "semantic",
                    "version": definition["version"],
                    "evidence_revisions": {i: source[i].get("revision", 1) for i in item["evidence"]},
                }
            )
    return retained[-100:]


def score_summary(items, dimensions=None):
    if dimensions is None:
        dimensions = DIMENSIONS if any("dimension" in i for i in items) else None
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


def build_analysis(call_id, segments, *, outcome=None, transcript_version=1, derived=None, rubric=None):
    from app.postcall.rubrics import effective

    rubric = rubric or effective({"outcome": outcome})
    stable = [s for s in segments if s.get("is_final") and not s.get("system_audio")]
    derived = derived or {}
    objections = derived.get("objections", [])
    commitments = derived.get("commitments", [])
    problems = derived.get("needs", [])
    # Conservative extraction-only baseline. No manufactured ratings when an evaluated inference model is absent.
    items = [
        {
            "dimension": d["id"],
            "title": d["title"],
            "anchors": d["anchors"],
            "score": None,
            "max_score": 2,
            "weight": d["weight"],
            "status": "N/A"
            if outcome in {"wrong_party", "voicemail", "no_answer", "ivr", "screening"}
            else "INSUFFICIENT_EVIDENCE",
            "evidence": [],
            "explanation": "Human review or an evaluated model is required.",
        }
        for d in rubric["dimensions"]
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
            "rubric_version": rubric["rubric_version"],
            "call_type": rubric["call_type"],
            "title": rubric["title"],
            "purpose": rubric["purpose"],
            "transcript_version": transcript_version,
            "items": items,
            "overall": score_summary(items, [d["id"] for d in rubric["dimensions"]]),
            "compliance_flags": [],
        },
        "metrics": behavioral_metrics(stable),
        "provider": "derived_state_or_unavailable",
    }
