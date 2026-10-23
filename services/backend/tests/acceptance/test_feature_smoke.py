"""Smoke journey proving the HTTP-only helpers drive a whole call."""

from journey import answered_by_human, get, hang_up, start, wrap_up


def test_call_answered_hung_up_and_wrapped_up(workspace):
    call = start(workspace)
    assert answered_by_human(workspace, call["id"])["state"] == "HUMAN"
    assert hang_up(workspace, call["id"])["state"] == "ENDED"
    wrap_up(workspace, call["id"], outcome="not_interested", contact_role="right_party")
    assert get(workspace, f"/v1/calls/{call['id']}")["outcome"] == "not_interested"
