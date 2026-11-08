"""R4.6, R4.8 and G3: the recovery pass reads quiet live calls and ends any call that overruns its state."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.calling.recovery import POLL_SECONDS, due_at, next_step
from app.calling.state import TIMEOUTS
from conftest import start

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def at(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def test_a_quiet_live_call_is_polled_and_an_overrun_one_takes_its_exit():
    ringing = {"state": "RINGING", "state_at": at(10), "provider_id": "v3:x"}
    assert next_step(ringing, NOW) == "poll"
    assert due_at(ringing) == NOW - timedelta(seconds=10) + timedelta(seconds=POLL_SECONDS)
    assert next_step({**ringing, "state_at": at(121)}, NOW) == "hangup.requested"
    assert next_step({"state": "ENDING", "state_at": at(61), "provider_id": "v3:x"}, NOW) == "call.ended"


def test_answered_calls_run_to_the_call_time_limit_from_their_start():
    call = {"state": "HUMAN", "state_at": at(5), "started_at": at(3599), "max_call_seconds": 3600}
    assert next_step({**call, "provider_id": "v3:x"}, NOW) == "poll"
    assert next_step({**call, "started_at": at(3601)}, NOW) == "hangup.requested"


def test_calls_without_a_carrier_reference_wait_for_their_limit():
    assert next_step({"state": "AUTHORIZED", "state_at": at(5)}, NOW) is None
    assert next_step({"state": "AUTHORIZED", "state_at": at(31)}, NOW) == "dial.unconfirmed"
    assert next_step({"state": "DIAL_UNCONFIRMED", "state_at": at(301)}, NOW) == "call.ended"
    assert due_at({"state": "ENDED"}) is None and next_step({"state": "FINALIZED"}, NOW) is None


def test_every_unfinished_state_has_a_due_time():
    for state in TIMEOUTS:
        assert due_at({"state": state, "state_at": at(0)}) is not None


def principal(ws):
    return {k: ws[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def stored(service, call_id):
    from app.platform.db import MODELS

    with service.db.transaction(False) as s:
        return s.get(MODELS["call_attempts"], call_id).data


def test_a_call_whose_leg_is_gone_is_finished_by_a_status_read(workspace, app, carrier, clock):
    """R4.8: the hang-up event never arrived; the next status read ends the call."""
    service = app.state.service
    call = start(workspace, key="r48-missed-hangup")
    for leg in carrier.calls.values():
        leg["state"] = "hangup"
    clock.now += timedelta(seconds=POLL_SECONDS + 1)
    asyncio.run(service.recover(principal(workspace)))
    assert stored(service, call["id"])["state"] in {"ENDED", "FINALIZED"}


def test_a_call_ringing_past_its_limit_is_hung_up(workspace, app, carrier, clock):
    """G3: a ringing call cannot stay ringing; past its limit the pass sends the hang-up."""
    service = app.state.service
    call = start(workspace, key="g3-stuck-ringing")
    clock.now += timedelta(seconds=TIMEOUTS["RINGING"][0] + 1)
    asyncio.run(service.recover(principal(workspace)))
    assert stored(service, call["id"])["state"] in {"ENDING", "ENDED", "FINALIZED"}
    assert carrier.hangups
