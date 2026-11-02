"""Calls after the first get a "This call" section built from the lead's real history."""

from datetime import timedelta
from uuid import uuid4

from journey import answered_by_human, event, get, hang_up, post, ready, run_jobs, wrap_up
from test_campaign_prep import SCRIPT, save_script

FOLLOW = (
    SCRIPT
    + """
# Follow-up
Keep it short and offer the inspection again.

# Voicemail
Hi {first_name}, {rep} from {seller} one last time. Call me back if roof care matters this year.
"""
)


def section(ws, lead_id):
    sections = get(ws, f"/v1/leads/{lead_id}/packet")["script"]["sections"]
    return next((s for s in sections if s.get("key") == "follow_up"), None)


def place(ws):
    ready(ws)
    reservation = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]})
    return post(
        ws,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": str(uuid4())},
    )


def only_one_lead(ws):
    """Keep the San Francisco lead, which the seeded 415 number can call."""
    keep = None
    for lead in get(ws, "/v1/workspace/leads")["items"]:
        if lead["phone"].startswith("+1415") and keep is None:
            keep = lead["id"]
        else:
            post(ws, "/v1/suppressions", {"phone": lead["phone"], "reason": "opt_out"})
    return keep


def test_first_call_has_no_follow_up_and_a_missed_call_never_claims_a_conversation(workspace, clock):
    ws = workspace
    lead = only_one_lead(ws)
    assert section(ws, lead) is None
    missed = place(ws)
    event(ws, missed["id"], "call.hangup", hangup_cause="no_answer")
    run_jobs(ws, rounds=2)
    follow = section(ws, lead)
    assert follow["purpose"] == "retry"
    assert "No one has answered the 1 earlier call" in follow["text"]
    assert "conversation" not in follow["text"].casefold()


def test_a_callback_and_a_spoken_follow_up_get_their_own_wording(workspace, clock):
    ws = workspace
    save_script(ws, FOLLOW)
    lead = only_one_lead(ws)
    spoken = place(ws)
    answered_by_human(ws, spoken["id"])
    hang_up(ws, spoken["id"])
    due = clock.now + timedelta(minutes=5)
    wrap_up(
        ws, spoken["id"], outcome="callback_requested", callback_at=due.isoformat(), callback_note="Pricing"
    )
    follow = section(ws, lead)
    assert follow["purpose"] == "callback"
    assert "They asked for this callback: Pricing" in follow["text"]
    assert "calling back as promised" in follow["text"]
    assert "You spoke on" in follow["text"] or "Last conversation" in follow["text"]
    assert "offer the inspection again" in follow["text"]
    # Once the callback is done, the next call is an ordinary follow-up of that conversation.
    clock.now = due
    callback = place(ws)
    assert callback["lead_id"] == lead
    answered_by_human(ws, callback["id"])
    hang_up(ws, callback["id"])
    wrap_up(ws, callback["id"], outcome="not_interested")
    follow = section(ws, lead)
    assert follow["purpose"] in {"follow_up", "final"}
    assert "They asked for this callback" not in follow["text"]


def test_the_final_attempt_carries_the_scripts_own_voicemail(workspace):
    ws = workspace
    save_script(ws, FOLLOW)
    policy = get(ws, "/v1/workspace")["policy"]
    post(ws, f"/v1/admin/policies/{policy['id']}/publish", {"max_attempts": 2, "reason": "Two tries"})
    lead = only_one_lead(ws)
    missed = place(ws)
    event(ws, missed["id"], "call.hangup", hangup_cause="no_answer")
    run_jobs(ws, rounds=2)
    follow = section(ws, lead)
    assert follow["purpose"] == "final"
    assert "Final attempt. If it reaches voicemail, leave this message: Hi " in follow["text"]
    assert "one last time" in follow["text"] and "{seller}" not in follow["text"]
