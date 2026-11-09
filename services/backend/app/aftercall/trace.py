"""The shape of a call from its timed transcript: who talked how long, questions asked and turn gaps."""


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
