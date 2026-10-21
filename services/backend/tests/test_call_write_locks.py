"""Call start and hangup lock the rows they update before reading them (PostgreSQL).

The two-rep shared profile named its 409s: four hangups lost a StaleDataError on
call_attempts and one call start on leads. Each read the row unlocked, a concurrent
writer committed a newer version, and the optimistic version check failed."""

import threading

import pytest
from app.db import MODELS
from conftest import start


def edit_concurrently(service, kind, identity):
    committed = threading.Event()

    def edit():
        with service.db.transaction() as s:
            row = s.get(MODELS[kind], identity, with_for_update=True)
            row.data = {**row.data, "concurrent_edit": True}
            row.version += 1
        committed.set()

    thread = threading.Thread(target=edit, daemon=True)
    thread.start()
    return thread, committed.wait(1.0)


@pytest.mark.postgres
def test_a_call_start_holds_its_lead_so_background_lead_writes_wait(workspace, app, monkeypatch):
    service, seen = app.state.service, {}
    original, claim = service.record_decision, service._claim_dial

    def dialing(*args, **kwargs):
        seen["dialing"] = True
        return claim(*args, **kwargs)

    def deciding(s, p, lead, decision):
        if seen.get("dialing") and "thread" not in seen and decision.allowed:
            seen["thread"], seen["committed_first"] = edit_concurrently(service, "leads", lead.id)
        return original(s, p, lead, decision)

    monkeypatch.setattr(service, "_claim_dial", dialing)
    monkeypatch.setattr(service, "record_decision", deciding)
    call = start(workspace)
    seen["thread"].join(10)
    assert seen["committed_first"] is False, "A background lead edit must wait for the dial claim"
    with service.db.transaction(False) as s:
        lead = s.get(MODELS["leads"], call["lead_id"])
        assert lead.data["concurrent_edit"] is True and lead.data["attempt_count"] >= 1


@pytest.mark.postgres
def test_a_simulated_hangup_receipt_holds_the_call_so_event_writes_wait(workspace, app, monkeypatch):
    service, seen = app.state.service, {}
    call = start(workspace, key="call-lock-hangup")
    original = service.finish_call

    def finishing(s, p, row, reason=None):
        if "thread" not in seen:
            seen["thread"], seen["committed_first"] = edit_concurrently(service, "call_attempts", row.id)
        return original(s, p, row, reason)

    monkeypatch.setattr(service, "finish_call", finishing)
    response = workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    seen["thread"].join(10)
    assert seen["committed_first"] is False, "An event write must wait for the hangup receipt"
    with service.db.transaction(False) as s:
        row = s.get(MODELS["call_attempts"], call["id"])
        assert row.data["concurrent_edit"] is True and row.data["state"] in {"ENDED", "FINALIZED"}
