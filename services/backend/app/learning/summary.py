"""What StatML has learned, in plain terms (R8.8, R9.25): calls used, best hours and triggers, wrong-party rate by
number source and status, the live model and its last change. Read from the latest saved learning dataset."""

from collections import defaultdict

MIN_GROUP = 3  # Groups with fewer calls are not ranked.


def _rate(groups, key):
    ranked = [
        {"value": value, "calls": len(rows), "rate": round(sum(rows) / len(rows), 3)}
        for value, rows in groups.items()
        if len(rows) >= MIN_GROUP and value not in {None, "unknown", "none"}
    ]
    return sorted(ranked, key=lambda r: (-r[key], -r["calls"], str(r["value"])))


def summary(rows, models, *, required_rows=40):
    """`rows` are training rows; `models` are model version records."""
    labeled = [r for r in rows if r.get("human") in (0, 1)]
    pickup_by_hour, goal_by_trigger = defaultdict(list), defaultdict(list)
    wrong_by_source, wrong_by_status = defaultdict(list), defaultdict(list)
    for row in labeled:
        features = row.get("features") or {}
        pickup_by_hour[features.get("local_hour")].append(int(row["human"]))
        if row.get("conversion") is not None:
            goal_by_trigger[features.get("trigger_type")].append(int(row["conversion"]))
        if row["human"] == 1:
            wrong = int(row.get("outcome") == "wrong_party")
            wrong_by_source[features.get("number_source")].append(wrong)
            wrong_by_status[features.get("number_status")].append(wrong)
    live = next((m for m in models if m.get("active")), None)
    changes = sorted(
        (
            (m.get(field), kind)
            for m in models
            for field, kind in (("promoted_at", "promoted"), ("rolled_back_at", "rolled back"))
            if m.get(field)
        ),
        reverse=True,
    )
    return {
        "calls_used": len(labeled),
        "required_calls": required_rows,
        "learning": len(labeled) >= required_rows,
        "best_hours": _rate(pickup_by_hour, "rate")[:3],
        "best_triggers": _rate(goal_by_trigger, "rate")[:3],
        "wrong_party_by_number_source": _rate(wrong_by_source, "calls"),
        "wrong_party_by_number_status": _rate(wrong_by_status, "calls"),
        "model": {"live": bool(live), "promoted_at": live.get("promoted_at") if live else None},
        "last_change": {"at": changes[0][0], "kind": changes[0][1]} if changes else None,
    }
