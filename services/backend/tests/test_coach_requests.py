"""A23/A25/A26: private request identity, cancel and late responses."""

import asyncio
from uuid import uuid4

import pytest
from app.db import get, update
from fastapi import HTTPException
from test_coach_lifecycle import holds
from test_live_memory import setup, turn
from test_remediation_privacy import persisted_content


@pytest.mark.asyncio
async def test_manual_retry_reuses_the_same_answer_and_charge(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service, identity = app.state.service, str(uuid4())
    turn(service, p, call, "retry")
    options = dict(urgent=True, question="Explain the private concern?", request_id=identity)
    first = await service.coach_turn(p, call["id"], **options)
    second = await service.coach_turn(p, call["id"], **options)
    assert first["id"] == second["id"]
    assert len(requests) == len(holds(service, p)) == 1
    assert "Explain the private concern?" not in persisted_content(service, p)
    with pytest.raises(HTTPException) as error:
        await service.coach_turn(p, call["id"], urgent=True, question="Different?", request_id=identity)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_before_request_delivery_prevents_later_submission(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service, identity = app.state.service, str(uuid4())
    turn(service, p, call, "late-delivery")
    result = await asyncio.to_thread(service.cancel_help, p, call["id"], identity)
    assert result["status"] == "cancelled"
    later = await service.coach_turn(p, call["id"], urgent=True, question="Too late?", request_id=identity)
    assert later["action"] == "NO_ACTION" and later["status"] == "cancelled"
    assert requests == [] and holds(service, p) == []


@pytest.mark.asyncio
async def test_cancel_running_help_stops_provider_but_preserves_billed_liability(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, identity, entered = app.state.service, str(uuid4()), asyncio.Event()
    turn(service, p, call, "cancel")

    class Provider:
        last_usage, last_request_id = {}, None

        async def suggest(self, *args, **kwargs):
            self.last_usage, self.last_request_id = {"cost": 0.002}, "manual-cancel"
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    task = asyncio.create_task(
        service.coach_turn(p, call["id"], urgent=True, question="Help?", request_id=identity)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        again = await service.coach_turn(p, call["id"], urgent=True, question="Help?", request_id=identity)
        assert again["status"] == "pending"
        await asyncio.to_thread(service.cancel_help, p, call["id"], identity)
        assert (await asyncio.wait_for(task, 1))["action"] == "NO_ACTION"
        assert holds(service, p)[0]["cost_usd"] == 0.002
        assert not service.coach.active
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_old_request_cannot_cancel_new_private_answer(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, first, second = app.state.service, str(uuid4()), str(uuid4())
    turn(service, p, call, "new-current")
    await service.coach_turn(p, call["id"], urgent=True, question="First?", request_id=first)
    original, entered, release = service.configured_coach, asyncio.Event(), asyncio.Event()

    def configured(p):
        provider, route = original(p)
        suggest = provider.suggest

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return await suggest(*args, **kwargs)

        provider.suggest = wait
        return provider, route

    monkeypatch.setattr(service, "configured_coach", configured)
    task = asyncio.create_task(
        service.coach_turn(p, call["id"], urgent=True, question="Second?", request_id=second)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.to_thread(service.cancel_help, p, call["id"], first)
        release.set()
        assert (await task)["action"] == "SHOW_CARD"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_cancel_route_has_typed_identity_and_call_scope(workspace, app):
    from conftest import start
    from test_remediation_privacy import principal

    call = start(workspace)
    c, h, service, p = workspace["client"], workspace["headers"], app.state.service, principal(workspace)
    url = f"/v1/calls/{call['id']}/coach/cancel"
    assert c.post(url, headers=h, json={"request_id": "not-a-uuid"}).status_code == 422
    identity = str(uuid4())
    assert c.post(url, headers=h, json={"request_id": identity}).status_code == 200
    with service.db.transaction() as s:
        update(get(s, "users", p["user_id"]), role="rep")
        update(get(s, "call_attempts", call["id"]), rep_id="different-rep")
    assert c.post(url, headers=h, json={"request_id": str(uuid4())}).status_code == 404


@pytest.mark.asyncio
async def test_expired_request_session_cannot_publish_answer(workspace, app, monkeypatch):
    from app.security import decode_session

    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    p = decode_session(service.db, service.settings, workspace["access_token"], service.clock())
    turn(service, p, call, "session")

    def revoke():
        with service.db.transaction() as s:
            update(get(s, "sessions", p["session_id"]), revoked=True)

    changes["during"] = revoke
    result = await service.coach_turn(
        p, call["id"], urgent=True, question="Private?", request_id=str(uuid4())
    )
    assert result["action"] == "NO_ACTION"


@pytest.mark.asyncio
async def test_manual_request_metadata_has_a_bound_and_original_expiry(workspace, app, clock, monkeypatch):
    from datetime import timedelta

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    for _ in range(32):
        await asyncio.to_thread(service.cancel_help, p, call["id"], str(uuid4()))
    with pytest.raises(HTTPException) as error:
        await asyncio.to_thread(service.cancel_help, p, call["id"], str(uuid4()))
    assert error.value.status_code == 429
    clock.now += timedelta(seconds=301)
    assert (await asyncio.to_thread(service.cancel_help, p, call["id"], str(uuid4())))[
        "status"
    ] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_between_answer_publication_and_response_hides_the_answer(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, identity, published = app.state.service, str(uuid4()), asyncio.Event()
    turn(service, p, call, "cancel-published")
    original = service._validated_help

    async def pause(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result.get("id")
        published.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_validated_help", pause)
    task = asyncio.create_task(
        service.coach_turn(p, call["id"], urgent=True, question="Help?", request_id=identity)
    )
    try:
        await asyncio.wait_for(published.wait(), 2)
        await asyncio.to_thread(service.cancel_help, p, call["id"], identity)
        await asyncio.wait_for(task, 1)
        with service.db.transaction(False) as s:
            for record in service.scoped(s, "coach_events", p):
                assert service.coach_content(p, {"id": record.id, **record.data}) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_context_assembly_cannot_extend_private_answer_raw_lifetime(workspace, app, clock, monkeypatch):
    from datetime import timedelta

    from app.service import dt

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, received = app.state.service, clock.now
    original = service._help_context

    def slow(*args, **kwargs):
        clock.now += timedelta(seconds=30)
        return original(*args, **kwargs)

    class Provider:
        last_usage, last_request_id = {}, None

        async def suggest(self, request, **kwargs):
            from test_remediation_coach import advice

            value = advice(request["state_version"])
            value["evidence_segment_ids"] = []
            return value

    monkeypatch.setattr(service, "_help_context", slow)
    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    result = await service.coach_turn(
        p, call["id"], urgent=True, question="What should I clarify?", request_id=str(uuid4())
    )
    assert result["action"] == "SHOW_CARD"
    with service.db.transaction(False) as s:
        event = get(s, "coach_events", result["id"])
        assert dt(event.data["raw_expires_at"]) <= received + timedelta(seconds=300)


@pytest.mark.asyncio
async def test_private_help_does_not_mutate_the_call_control_or_wrapup_version(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, identity = app.state.service, str(uuid4())
    turn(service, p, call, "control-version")
    with service.db.transaction(False) as s:
        before = get(s, "call_attempts", call["id"]).version
    await service.coach_turn(p, call["id"], urgent=True, question="Help?", request_id=identity)
    await asyncio.to_thread(service.cancel_help, p, call["id"], identity)
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).version == before, (
            "Helper metadata cannot invalidate a rep's call control or wrap-up edit"
        )
