HUMAN = {
    "right_party",
    "gatekeeper",
    "callback_requested",
    "meeting_booked",
    "meeting_held",
    "not_interested",
    "wrong_party",
    "opt_out",
    "qualified_conversation",
    "sale_won",
}
RIGHT = {
    "right_party",
    "callback_requested",
    "meeting_booked",
    "meeting_held",
    "qualified_conversation",
    "sale_won",
}
NEXT = {"callback_requested", "meeting_booked", "meeting_held", "qualified_conversation", "sale_won"}


def pooled_predictions(history, *, segment, hour):
    # Segment/time evidence is shrunk toward pooled counts; a few missed calls cannot create a confident exclusion.
    observed = [r for r in history if r.get("outcome")]
    pooled_human = (sum(r["outcome"] in HUMAN for r in observed) + 2) / (len(observed) + 10)
    local = [
        r for r in observed if r.get("segment") == segment and abs(r.get("local_hour", hour) - hour) <= 2
    ]
    human = sum(r["outcome"] in HUMAN for r in local)
    right = sum(r["outcome"] in RIGHT for r in local)
    next_step = sum(r["outcome"] in NEXT for r in local)
    return {
        "p_human": (human + 10 * pooled_human) / (len(local) + 10),
        "p_right_party_given_human": (right + 1) / (human + 3),
        "p_next_step_given_right_party": (next_step + 1) / (right + 4),
        "expected_rep_seconds": (sum(r.get("duration_seconds", 60) for r in local) + 5 * 60)
        / (len(local) + 5),
        "expected_attempt_cost_usd": 0.02,
        "observations": len(local),
        "model_version": "pooled-beta-v1",
    }
