"""Concurrent reps claim in parallel; authorization changes still order against every claim."""

import threading
import time

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from test_control_completion import principal


def second_rep(service, p, workspace):
    from app.db import put

    from scripts import measure_completion as measure

    measure.workload_capacity(service, p, 2)
    with service.db.transaction() as s:
        user = put(
            s,
            "users",
            p["tenant_id"],
            p["seller_id"],
            {"role": "rep", "email": "parallel-rep@example.test", "name": "Parallel rep", "disabled": False},
            now=service.clock(),
        )
    second = {**p, "user_id": user.id, "role": "rep"}
    for who in (p, second):
        service.presence(
            who,
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "synthetic-" + who["user_id"],
            },
        )
    return second


@pytest.mark.postgres
def test_a_claim_takes_the_seller_lock_shared_and_its_number_row(workspace, app):
    service, p = app.state.service, principal(workspace)
    statements = []

    def seen(connection, cursor, statement, *rest):
        statements.append(statement)

    event.listen(service.db.engine, "before_cursor_execute", seen)
    try:
        service.presence(
            p,
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "synthetic-" + p["user_id"],
            },
        )
        statements.clear()
        service.reserve(p, workspace["campaign_id"])
    finally:
        event.remove(service.db.engine, "before_cursor_execute", seen)
    locks = [s for s in statements if "pg_advisory_xact_lock" in s]
    assert locks and all("pg_advisory_xact_lock_shared" in s for s in locks), locks
    assert any("number_inventory" in s and "FOR UPDATE" in s for s in statements)


@pytest.mark.postgres
def test_an_exclusive_authorization_change_still_blocks_a_claim_until_it_commits(workspace, app):
    service, p = app.state.service, principal(workspace)
    second = second_rep(service, p, workspace)
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def opt_out_like_writer():
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)  # Exclusive, as suppressions and consent take it.
            held.set()
            release.wait(10)

    writer = threading.Thread(target=opt_out_like_writer)
    writer.start()
    assert held.wait(10)
    claimer = threading.Thread(target=lambda: (service.reserve(second, workspace["campaign_id"]), done.set()))
    claimer.start()
    time.sleep(0.5)
    assert not done.is_set(), "A claim cannot interleave with an authorization change"
    release.set()
    writer.join(10)
    claimer.join(10)
    assert done.is_set()


@pytest.mark.postgres
def test_two_reps_claims_do_not_wait_for_each_other(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    second = second_rep(service, p, workspace)
    inside, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = service.inventory_evidence

    def slow_first(s, who, *args, **kwargs):
        if who["user_id"] == p["user_id"] and s.info.get("write") and not inside.is_set():
            inside.set()  # The first rep's claim is inside its transaction, holding its locks.
            release.wait(10)
        return original(s, who, *args, **kwargs)

    monkeypatch.setattr(service, "inventory_evidence", slow_first)
    first = threading.Thread(target=lambda: service.reserve(p, workspace["campaign_id"]))
    first.start()
    assert inside.wait(10)
    try:
        other = threading.Thread(
            target=lambda: (service.reserve(second, workspace["campaign_id"]), finished.set())
        )
        other.start()
        other.join(8)
        assert finished.is_set(), "The second rep's claim completes while the first is still open"
    finally:
        release.set()
        first.join(10)


def test_a_concurrent_uniqueness_conflict_reselects_instead_of_failing(workspace, app, monkeypatch):
    from conftest import ready

    ready(workspace)
    service = app.state.service
    original, attempts = service._claim_selection, []

    def conflict_once(who, campaign_id, plan):
        attempts.append(plan["selection"]["lead_id"])
        if len(attempts) == 1:
            raise IntegrityError("INSERT INTO active_claims", {}, Exception("duplicate key"))
        return original(who, campaign_id, plan)

    monkeypatch.setattr(service, "_claim_selection", conflict_once)
    result = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert result.status_code == 200, result.text
    assert len(attempts) == 2
