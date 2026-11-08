"""Shared outcome definitions and transparent, heartbeat-supported working intervals."""

from app.optimization.numbering import timestamp, wilson


def connect_health_counts(values, *, min_support=30):
    count, base = values.get("connect_recent_n", 0), values.get("connect_reference_n", 0)
    successes, reference = values.get("connect_recent_s", 0), values.get("connect_reference_s", 0)
    unknown = values.get("connect_recent_unknown", 0)
    low, high = wilson(successes, count)
    ref_low, ref_high = wilson(reference, base)
    supported = count >= min_support and base >= min_support
    return {
        "status": "warning"
        if supported and high < ref_low
        else "observed"
        if supported
        else "insufficient_data",
        "samples": count,
        "unknown": unknown,
        "reference_samples": base,
        "rate": successes / count if count else None,
        "all_attempt_rate": successes / (count + unknown) if count + unknown else None,
        "confidence_low": low,
        "confidence_high": high,
        "reference_low": ref_low,
        "reference_high": ref_high,
        "reference_rate": reference / base if base else None,
        "comparison": "Last 24 hours versus preceding 27 days; 95% Wilson intervals. Descriptive, not adjusted for changes in the list or proof of a spam label.",
    }


def call_timing(call, event_type, payload, *, now):
    """Source event time is independent of delivery order and never revives a leg."""
    try:
        at = timestamp(payload.get("occurred_at")) or now
        started, ended = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
    except (ValueError, TypeError):
        return {}
    if not started or at < started or at > now:
        return {}
    values = {}
    if event_type == "call.answered" and (not ended or at <= ended):
        answered = timestamp(call.get("answered_at"))
        values["answered_at"] = min(answered, at).isoformat() if answered else at.isoformat()
        values["answer_timing_source"] = payload.get("timing_source", "receipt")
    elif event_type == "call.bridged" and (not ended or at <= ended):
        if payload.get("timing_source") == "provider":
            bridged = timestamp(call.get("bridged_at"))
            values["bridged_at"] = min(bridged, at).isoformat() if bridged else at.isoformat()
    elif event_type == "call.hangup":
        values["ended_at"] = min(ended, at).isoformat() if ended else at.isoformat()
    answered = timestamp(values.get("answered_at") or call.get("answered_at"))
    ended = timestamp(values.get("ended_at") or call.get("ended_at"))
    if answered and ended and ended >= answered:
        values["connected_seconds"] = (ended - answered).total_seconds()
    return values
