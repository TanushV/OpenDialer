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
    """Union each observed side on one timeline; never compare every segment pair.

    A monologue is continuous speech by one side without the other side speaking.
    Exchanges require different exclusive speaking intervals. Simultaneous speech
    is measured as overlap, not an inferred transfer of the conversational floor.
    """
    import math

    events, timed, questions = {}, 0, 0
    for segment in segments:
        side = segment.get("side")
        if side not in {"rep", "remote"} or segment.get("system_audio"):
            continue
        if side == "rep":
            count = segment.get("question_count")
            questions += (
                count
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0
                else (segment.get("text") or "").count("?")
            )
        start, end = segment.get("start_ms"), segment.get("end_ms")
        if (
            not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (start, end)
            )
            or start < 0
            or end <= start
        ):
            continue
        timed += 1
        index = 0 if side == "rep" else 1
        events.setdefault(start, [0, 0])[index] += 1
        events.setdefault(end, [0, 0])[index] -= 1

    active = [0, 0]
    duration = {"rep": 0, "remote": 0}
    previous = None
    last_span_side = last_solo = None
    run = longest = overlap = silence = 0
    gaps = []
    for at, delta in sorted(events.items()):
        if previous is not None:
            width = at - previous
            rep, remote = active[0] > 0, active[1] > 0
            duration["rep"] += width if rep else 0
            duration["remote"] += width if remote else 0
            solo = "rep" if rep and not remote else "remote" if remote and not rep else None
            if solo:
                run = run + width if solo == last_span_side else width
                longest = max(longest, run)
                if last_solo is not None and solo != last_solo:
                    gaps.append(silence)
                last_solo = solo
            else:
                run = 0
            if rep and remote:
                overlap += width
            silence = 0 if rep or remote else silence + width
            last_span_side = solo
        active[0] += delta[0]
        active[1] += delta[1]
        previous = at
    total = sum(duration.values())
    return {
        "timed_segments": timed,
        "total_segments": len(segments),
        "rep_talk_ratio": duration["rep"] / total if total else None,
        "talk_ms": duration,
        "longest_monologue_ms": longest if timed else None,
        "question_count": questions,
        "turn_exchanges": len(gaps),
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
            or "\n".join(item["text"] for item in derived.get("rundown_points", []))[:2400]
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
