"""D29, R11.3: a callback rings the app whenever it is open, paused or not, for any caller.

Live, November 10: every callback heard the greeting, because the app was rung only while it was calling with audio
up, and only for callers on a lead. An app that is closed (it stops saying it is open) still gives the greeting and a
missed-call task.
"""

from datetime import timedelta

from journey import call, get, run_jobs
from test_feature_calling import Telnyx, inbound, inbound_task, pause
from test_number_setup import connect

STRANGER = "+12025550123"


def reachable(ws, registered=True):
    return call(ws, "POST", "/v1/rep/reachable", json={"registered": registered})


def ring(ws, clock, tag):
    Telnyx(ws, clock).deliver("call.initiated", inbound(tag, STRANGER), "evt-" + tag)
    run_jobs(ws)
    call(ws, "POST", "/v1/admin/control/run")
    return inbound_task(ws, tag)


def transfers(carrier):
    return [a["payload"]["to"] for a in carrier.actions if a["action"] == "transfer"]


def greetings(carrier):
    return [a for a in carrier.actions if a["action"] == "speak"]


def test_an_open_paused_app_rings_for_any_caller(workspace, carrier, clock):
    ws = workspace
    connect(ws, sip_username="workspace-audio")
    pause(ws)
    reachable(ws)
    task = ring(ws, clock, "open-app")
    assert task["route"] == "rep"
    assert task["routed_rep_id"] == get(ws, "/v1/workspace")["user"]["id"]
    assert transfers(carrier) == ["sip:workspace-audio@sip.telnyx.com"]
    assert not greetings(carrier)


def test_an_app_that_stopped_saying_it_is_open_gives_the_greeting_and_a_task(workspace, carrier, clock):
    ws = workspace
    connect(ws, sip_username="workspace-audio")
    pause(ws)
    reachable(ws)
    clock.now += timedelta(seconds=91)
    task = ring(ws, clock, "closed-app")
    assert task["route"] == "callback_queue" and task["routed_rep_id"] is None
    assert not transfers(carrier)
    assert len(greetings(carrier)) == 1


def test_an_open_app_without_its_call_client_is_not_rung(workspace, carrier, clock):
    ws = workspace
    connect(ws, sip_username="workspace-audio")
    pause(ws)
    reachable(ws, registered=False)
    task = ring(ws, clock, "no-client")
    assert task["route"] == "callback_queue"
    assert not transfers(carrier)
