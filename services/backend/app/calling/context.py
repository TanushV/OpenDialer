"""Who a call is with and what its speech may do, for an outreach lead or an approved test contact.

A test call carries `test_contact` and its recipient lives in `test_contacts`; never look it up as a lead. Test calls
share the normal script, speech and helper, but none of the outreach permissions or side effects (work package
section 3).
"""

from app import ops
from app.calling.records import CallView
from app.calling.state import TERMINAL
from app.compliance.policy import authorize
from app.platform.db import workspace


def call_party(service, s, p, call):
    """(recipient row, is_test) for a call record."""
    test = CallView.model_validate(call.data).test_contact
    return ops.required(service, s, "test_contacts" if test else "leads", call.data["lead_id"], p), test


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
