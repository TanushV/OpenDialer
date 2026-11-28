"""Compare call results across one dimension at a time (call insights plan P7 and phase 7).

Each finished outreach call counts once in its group: a script test variant (only calls in the test), a path, the
local time band it was placed in, a lead list, the lead's trigger, or a bucket of a per-call number. Every group shows
its counts and a 95% range (Wilson), and says "not enough data" until it has `ENOUGH` conversations, because a
difference on a few calls is noise.
"""

import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.call_statistics import collect
from app.calling.context import PERSON
from app.platform.db import MODELS
from app.scheduling import prime_time

ENOUGH = 30
MEETINGS = {"meeting_booked", "meeting_held"}
DIMENSIONS = ("variant", "path", "band", "list", "trigger", "talk_share", "longest_monologue", "response_gap")
BUCKETS = {
    # (metric, edges, labels): a call's number goes in the first bucket whose upper edge it is under.
    "talk_share": ("talk_share", (0.4, 0.6), ("under 40%", "40% to 60%", "over 60%")),
    "longest_monologue": (
        "longest_rep_monologue_ms",
        (20000, 40000),
        ("under 20s", "20s to 40s", "over 40s"),
    ),
    "response_gap": ("median_response_ms", (700, 1500), ("under 0.7s", "0.7s to 1.5s", "over 1.5s")),
}


def wilson(hits, total, z=1.96):
    if not total:
        return None
    share = hits / total
    denominator = 1 + z * z / total
    centre = (share + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(share * (1 - share) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def rate(hits, total):
    return {
        "count": hits,
        "of": total,
        "rate": round(hits / total, 4) if total else None,
        "range": wilson(hits, total),
    }


def _leads(service, p, ids):
    model = MODELS["leads"]
    if not ids:
        return {}
    with service.db.transaction(False) as s:
        rows = s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], model.id.in_(list(ids))
            )
        )
        return {row.id: (row.data, row.op_zone) for row in rows}


def bucket(value, edges, labels):
    if value is None:
        return None
    return next((label for edge, label in zip(edges, labels, strict=False) if value < edge), labels[-1])


def group_of(row, by, lead, zone):
    data = row["_data"]
    if by == "variant":
        return row["script_variant"] if row["variant_in_test"] and row["script_variant"] else None
    if by == "path":
        return row.get("script_path") or "no path"
    if by == "band":
        started = data.get("submitted_at") or data.get("started_at")
        zones = prime_time.zones_of(zone)
        if not started or len(zones) != 1:
            return "zone unsure"
        local = datetime.fromisoformat(started).astimezone(ZoneInfo(zones[0]))
        return (
            prime_time.label(prime_time.default_band(local))
            if prime_time.slot(local) is not None
            else "outside"
        )
    if by == "list":
        names = (lead or {}).get("list_names") or [(lead or {}).get("list_name") or "no list"]
        return names[0]
    if by == "trigger":
        return ((lead or {}).get("trigger") or {}).get("type") or "none"
    if by in BUCKETS:
        metric, edges, labels = BUCKETS[by]
        return bucket((data.get("metrics") or {}).get(metric), edges, labels)
    raise ValueError(by)


def compare(service, p, by, *, include_test=False):
    if by not in DIMENSIONS:
        raise ValueError(by)
    calls = [
        row
        for row in collect(service, p, include_test=include_test)[0]
        if include_test or not row["test_call"]
    ]
    leads = _leads(service, p, {row["_lead_id"] for row in calls if row["_lead_id"]})
    counts = defaultdict(
        lambda: {"dials": 0, "conversations": 0, "past_opener": 0, "pitch": 0, "meetings": 0}
    )
    for row in calls:
        lead, zone = leads.get(row["_lead_id"], (None, None))
        group = group_of(row, by, lead, zone)
        if group is None:
            continue
        entry = counts[group]
        entry["dials"] += 1
        if row["_data"].get("outcome") in PERSON:
            entry["conversations"] += 1
            stages = row["_stages"]
            entry["past_opener"] += int(any(stage != "opening" for stage in stages))
            entry["pitch"] += int("pitch" in stages)
            entry["meetings"] += int(row["_data"].get("outcome") in MEETINGS)
    groups = []
    for name, entry in sorted(counts.items(), key=lambda item: -item[1]["dials"]):
        talks = entry["conversations"]
        groups.append(
            {
                "group": name,
                "dials": entry["dials"],
                "conversations": rate(talks, entry["dials"]),
                "past_opener": rate(entry["past_opener"], talks),
                "reached_pitch": rate(entry["pitch"], talks),
                "meetings": rate(entry["meetings"], talks),
                "enough": talks >= ENOUGH,
            }
        )
    return {"by": by, "groups": groups, "enough_at": ENOUGH}


def variants(service, p, template):
    """The current master's test with each arm's hypothesis and results (plan P7)."""
    test = (template or {}).get("variants")
    result = compare(service, p, "variant")
    if not test:
        return {"test": None, **result}
    seen = {group["group"]: group for group in result["groups"]}
    arms = [
        {
            "variant": name,
            "share": test["arms"][name]["share"],
            "hypothesis": test["arms"][name]["hypothesis"],
            "results": seen.get(name),
        }
        for name in test["order"]
    ]
    return {"test": {"id": test["id"], "when": test.get("when") or []}, "arms": arms, **result}
