"""Stage drop-off (D31): where answered calls end in the script, by outcome. The only learning view kept.

Each finished call's after-call summary names the script part it ended in (`summary.ended_at`). Calls nobody answered
are not counted; an answered call whose transcript shows no part is answered but not placed.
"""


def drop_off(calls, stages):
    """`calls`: dicts with `human`, `outcome` and `ended_at_part`. `stages`: the script's stage names in order."""
    answered = [c for c in calls if c.get("human")]
    rows = {stage: {"stage": stage, "calls": 0, "outcomes": {}} for stage in stages}
    placed = 0
    for call in answered:
        part = call.get("ended_at_part")
        if not part:
            continue
        placed += 1
        row = rows.setdefault(part["stage"], {"stage": part["stage"], "calls": 0, "outcomes": {}})
        row["calls"] += 1
        outcome = call.get("outcome") or "no_outcome"
        row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
    return {"answered": len(answered), "placed": placed, "stages": list(rows.values())}
