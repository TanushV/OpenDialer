"""How well voicemail detection matches what really happened (R6.4, risk R2).

The only ground truth is a call the rep kept open: Stay after a machine result, then an outcome that needs a person.
Skipped calls are counted but cannot be checked. The report is meaningful once `ENOUGH` real calls exist.
"""

ENOUGH = 50
PERSON = frozenset(
    {
        "right_party",
        "qualified_conversation",
        "callback_requested",
        "meeting_booked",
        "meeting_held",
        "not_interested",
        "gatekeeper",
        "wrong_party",
        "opt_out",
        "sale_won",
    }
)


def report(calls):
    real = [c for c in calls if c.get("outcome") and not c.get("is_test")]
    detected = [c for c in real if c.get("machine_detected") or c.get("screening_detected")]
    stayed = [c for c in detected if c.get("stay_on_voicemail")]
    wrong = [c for c in stayed if c["outcome"] in PERSON]
    missed = [c for c in real if not c.get("machine_detected") and c["outcome"] == "voicemail"]
    return {
        "calls": len(real),
        "ready": len(real) >= ENOUGH,
        "detected_as_machine": len(detected),
        "skipped": len(detected) - len(stayed),
        "stayed": len(stayed),
        "stayed_and_reached_a_person": len(wrong),
        "voicemail_not_detected": len(missed),
    }
