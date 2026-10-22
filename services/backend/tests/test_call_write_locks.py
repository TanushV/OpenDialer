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


@pytest.mark.postgres
def test_a_wrapup_correction_holds_the_lead_so_packet_refresh_waits(workspace, app, monkeypatch):
    """The connected browser journey's outcome correction answered 409 while background
    work wrote the same lead."""
    from datetime import timedelta

    from test_remediation_rep_workflow import finish

    service, seen = app.state.service, {}
    call = start(workspace, key="call-lock-correction")
    finish(workspace, call, callback_at=(service.clock() + timedelta(days=1)).isoformat())
    required, save = service.required, service.save_disposition

    def saving(*args, **kwargs):
        seen["saving"] = True
        return save(*args, **kwargs)

    def reading(s, kind, identity, p, **kwargs):
        row = required(s, kind, identity, p, **kwargs)
        if kind == "leads" and seen.get("saving") and "thread" not in seen:
            # Right after the wrap-up reads its lead: a background writer commits first.
            seen["thread"], seen["committed_first"] = edit_concurrently(service, "leads", row.id)
        return row

    monkeypatch.setattr(service, "save_disposition", saving)
    monkeypatch.setattr(service, "required", reading)
    c, h = workspace["client"], workspace["headers"]
    current = c.get(f"/v1/calls/{call['id']}", headers=h).json()
    response = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json={
            "request_id": "6f5e2b4e-8a1f-4a8e-9d55-0e2f2d1c0a11",
            "outcome": "not_interested",
            "contact_role": "right_party",
            "expected_version": current["version"],
            "expected_edit_version": current.get("edit_version", 0),
        },
    )
    assert response.status_code == 200, response.text
    seen["thread"].join(10)
    assert seen["committed_first"] is False


@pytest.mark.postgres
def test_a_task_edit_holds_its_lead_so_packet_refresh_waits(workspace, app, monkeypatch):
    """The connected journey's lost-response task edit answered 409 the same way."""
    from datetime import timedelta
    from uuid import uuid4

    from test_local_followthrough import tasks
    from test_remediation_rep_workflow import finish

    service, seen = app.state.service, {}
    call = start(workspace, key="call-lock-task")
    finish(workspace, call, callback_at=(service.clock() + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    original = service._sync_callback

    def syncing(s, p, lead):
        if "thread" not in seen:
            seen["thread"], seen["committed_first"] = edit_concurrently(service, "leads", lead.id)
        return original(s, p, lead)

    monkeypatch.setattr(service, "_sync_callback", syncing)
    response = workspace["client"].patch(
        f"/v1/tasks/{task['id']}",
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": task["version"], "note": "Human decision"},
    )
    assert response.status_code == 200, response.text
    seen["thread"].join(10)
    assert seen["committed_first"] is False


def stream_writer_holding(service, p, call_id, pause=0.5):
    """A writer in the documented order: the seller's authorization stream, then the call row."""
    import time

    holding, result = threading.Event(), {}

    def write():
        try:
            with service.db.transaction() as s:
                service.db.authorize_stream(s, p)
                holding.set()
                time.sleep(pause)  # A row-first writer would take the call row meanwhile.
                row = s.get(MODELS["call_attempts"], call_id, with_for_update=True)
                row.data = {**row.data, "stream_writer": True}
                row.version += 1
            result["committed"] = True
        except Exception as exc:  # noqa: BLE001 - the deadlock victim's error is the evidence
            result["error"] = type(exc).__name__ + ": " + str(exc).splitlines()[0]

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    assert holding.wait(5)
    return thread, result


@pytest.mark.postgres
def test_a_task_edit_takes_the_authorization_stream_before_its_rows(workspace, app):
    """Connected e2e (CI run on 8a862fa) deadlocked: a task edit locked the call, lead and task
    rows and then asked for the seller's authorization stream, while a writer holding that stream
    waited for the call row. Every writer takes the stream first, then call, lead, tasks."""
    from datetime import timedelta
    from uuid import uuid4

    from test_local_followthrough import tasks
    from test_remediation_privacy import principal
    from test_remediation_rep_workflow import finish

    service, p = app.state.service, principal(workspace)
    call = start(workspace, key="stream-order-task")
    finish(workspace, call, callback_at=(service.clock() + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    thread, result = stream_writer_holding(service, p, call["id"])
    response = workspace["client"].patch(
        f"/v1/tasks/{task['id']}",
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": task["version"], "note": "Human decision"},
    )
    thread.join(15)
    assert result == {"committed": True}, result
    assert response.status_code == 200, response.text


@pytest.mark.postgres
def test_a_wrapup_correction_takes_the_authorization_stream_before_its_rows(workspace, app):
    from datetime import timedelta

    from test_remediation_privacy import principal
    from test_remediation_rep_workflow import finish

    service, p = app.state.service, principal(workspace)
    call = start(workspace, key="stream-order-disposition")
    finish(workspace, call, callback_at=(service.clock() + timedelta(days=1)).isoformat())
    c, h = workspace["client"], workspace["headers"]
    current = c.get(f"/v1/calls/{call['id']}", headers=h).json()
    thread, result = stream_writer_holding(service, p, call["id"])
    response = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json={
            "request_id": "0b9d2f6e-3c41-4c55-8d0e-7f1e2a3b4c5d",
            "outcome": "not_interested",
            "contact_role": "right_party",
            "expected_version": current["version"],
            "expected_edit_version": current.get("edit_version", 0),
        },
    )
    thread.join(15)
    assert result == {"committed": True}, result
    assert response.status_code in (200, 409), response.text  # 409: the stream writer bumped it first
