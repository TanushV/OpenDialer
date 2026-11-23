"""Who a call is with and what its speech may do, for an outreach lead or an approved test contact.

A test call carries `test_contact` and its recipient lives in `test_contacts`; never look it up as a lead. Test calls
share the normal script, speech and helper, but none of the outreach permissions or side effects (work package
section 3).
"""

from app import ops
from app.calling.blockers import readable_phone
from app.calling.records import CallView
from app.calling.state import TERMINAL
from app.compliance.policy import authorize
from app.platform.db import MODELS, workspace

# Outcomes that mean a person was reached; the rep's saved outcome is the evidence, not detection (work package 6.16).
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


def call_party(service, s, p, call):
    """(recipient row, is_test) for a call record."""
    test = CallView.model_validate(call.data).test_contact
    return ops.required(service, s, "test_contacts" if test else "leads", call.data["lead_id"], p), test


def practice_call(call):
    """A call to an approved test destination: its dialogue is practice and never an outreach suppression."""
    return CallView.model_validate(call.data).test_contact


def live_transcription_enabled(service, p):
    """The Settings switch: off means no call audio leaves this computer for speech or the helper."""
    return service.configs.read(workspace(p)).get("live_transcription", True) is not False


def speech_allowed(service, s, p, call):
    """Whether this call may stream speech now: tests always (while live), outreach under its policy."""
    if call.data["state"] in TERMINAL or not live_transcription_enabled(service, p):
        return False
    recipient, test = call_party(service, s, p, call)
    if test:
        return True
    number = ops.required(service, s, "number_inventory", call.data["number_id"], p)
    scope = {**p, **CallView.model_validate(call.data).dial_scope}
    return authorize(
        ops.context(service, s, scope, recipient, number, permission="transcription", active=True)
    ).allowed


def spoke_before(service, s, p, lead):
    """Whether an earlier call with this lead reached a person, by its saved outcome."""
    calls = MODELS["call_attempts"]
    prior = ops.lead_records(service, s, p, "call_attempts", lead.id, calls.op_status.in_(TERMINAL), limit=20)
    return any(CallView.model_validate(row.data).outcome in PERSON for row in prior)


def attempt_voicemail_cue(service, s, p, lead, campaign, ordinal, allowance, callback_due):
    """The advisory voicemail cue recorded on an outreach attempt (work package 6.12)."""
    return ops.voicemail_cue(
        service,
        lead.data,
        campaign,
        ordinal,
        allowance,
        callback_due=callback_due,
        spoke_before=spoke_before(service, s, p, lead),
    )


def with_callback_number(script, number):
    """The call's script snapshot with `{callback_number}` filled from the caller ID used on this call."""
    if not script or not number:
        return script
    phone = readable_phone(number.data["phone"])
    sections = [
        {**section, "text": section["text"].replace("{callback_number}", phone)}
        if isinstance(section.get("text"), str)
        else section
        for section in script.get("sections", [])
    ]
    return {**script, "sections": sections}
