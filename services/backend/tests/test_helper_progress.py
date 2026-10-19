"""A23/A26/A27: show complete validated help before its explanation finishes."""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from app.db import get
from app.providers.openrouter import OpenRouterCoach
from test_live_memory import setup, turn
from test_remediation_coach import advice, ordinary_advice
from test_remediation_privacy import persisted_content


def frame(content="", **fields):
    return (
        "data: "
        + json.dumps(
            {
                "id": "progress-fixture",
                "model": "fixture",
                "provider": "Cerebras",
                "choices": [{"delta": {"content": content}, "finish_reason": None}],
                **fields,
            }
        )
        + "\n\n"
    ).encode()


class PausedStream(httpx.AsyncByteStream):
    def __init__(self, result, ready, release, *, broken=False, provider="Cerebras"):
        self.result, self.ready, self.release = result, ready, release
        self.broken, self.provider = broken, provider

    async def __aiter__(self):
        # The primary response and all its evidence/identity fields finish first.
        primary = {k: self.result[k] for k in ("state_version", "context_fingerprint", "field_audit")}
        guidance = {
            k: self.result[k]
            for k in ("kind", "say_or_ask", "evidence_segment_ids", "playbook_card_ids", "expires_in_ms")
        }
        guidance["alternatives"] = []
        prefix = json.dumps(primary)[:-1] + ', "guidance": ' + json.dumps(guidance)[:-1] + ', "reason": "'
        yield frame(prefix, provider=self.provider)
        self.ready.set()
        await self.release.wait()
        if self.broken is True:
            yield frame(error={"message": "synthetic provider failure"})
            return
        suffix = ', "say_or_ask": "Altered later?"' if self.broken == "duplicate" else ""
        yield frame('A fuller explanation after the useful answer."' + suffix + '}, "state_delta": null}')
        yield frame(
            choices=[{"delta": {}, "finish_reason": "stop"}],
            usage={"cost": 0.001, "completion_tokens": 60},
        )
        yield b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_complete_primary_answer_is_visible_before_explanation_and_one_receipt_is_updated(
    workspace, app, monkeypatch
):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, ready, release = app.state.service, asyncio.Event(), asyncio.Event()
    turn(service, p, call, "progress")

    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(200, stream=PausedStream(ordinary_advice(context, "progress"), ready, release))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            service, "configured_coach", lambda _: (OpenRouterCoach("fixture", http=http), "openrouter")
        )
        identity = str(uuid4())
        task = asyncio.create_task(
            service.coach_turn(p, call["id"], urgent=True, question="Explain?", request_id=identity)
        )
        try:
            await asyncio.wait_for(ready.wait(), 2)
            # The stream is blocked, so this proves a real early result, not merely final publication.
            response = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"])
            visible = response.json()["coach"]
            assert visible is not None and visible["say_or_ask"] == advice()["say_or_ask"]
            assert visible["status"] == "streaming"
            assert not task.done()
            assert visible["say_or_ask"] not in persisted_content(service, p)
            release.set()
            final = await task
            assert final["id"] == visible["id"]
            assert final["status"] == "ready"
            assert final["reason"] == "A fuller explanation after the useful answer."
            with service.db.transaction(False) as s:
                assert len(service.scoped(s, "coach_events", p)) == 1
                holds = [
                    r for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "live_coach"
                ]
                assert len(holds) == 1 and holds[0].data["cost_usd"] == 0.001
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["cancel", "hangup", "correct", "expire", "failure", "duplicate"])
async def test_partial_help_is_withdrawn_at_a_changed_boundary(workspace, app, clock, monkeypatch, boundary):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, ready, release = app.state.service, asyncio.Event(), asyncio.Event()
    turn(service, p, call, "progress")

    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(
            200,
            stream=PausedStream(
                ordinary_advice(context, "progress"),
                ready,
                release,
                broken="duplicate" if boundary == "duplicate" else boundary == "failure",
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            service, "configured_coach", lambda _: (OpenRouterCoach("fixture", http=http), "openrouter")
        )
        identity = str(uuid4())
        task = asyncio.create_task(
            service.coach_turn(p, call["id"], urgent=True, question="Explain?", request_id=identity)
        )
        try:
            await asyncio.wait_for(ready.wait(), 2)
            with service.db.transaction(False) as s:
                early = service.scoped(s, "coach_events", p)
                assert len(early) == 1, (
                    "A complete primary answer must be published before testing withdrawal"
                )
                early_id = early[0].id
            if boundary == "cancel":
                await asyncio.to_thread(service.cancel_help, p, call["id"], identity)
            elif boundary == "hangup":
                await service.hangup(p, call["id"])
            elif boundary == "correct":
                turn(service, p, call, "progress", revision=2, text="The earlier description was incorrect.")
            elif boundary == "expire":
                clock.now += timedelta(seconds=301)
            release.set()
            await task
            with service.db.transaction(False) as s:
                row = get(s, "coach_events", early_id)
                assert service.coach_content(p, {"id": row.id, **row.data}) is None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transport_cancel_during_early_publication_cannot_leave_an_unfinished_answer(
    workspace, app, monkeypatch
):
    import threading

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "progress")
    stored, resume = threading.Event(), threading.Event()
    ready, release = asyncio.Event(), asyncio.Event()
    original = service._publish_help

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        stored.set()
        assert resume.wait(3)
        return result

    monkeypatch.setattr(service, "_publish_help", delayed)

    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        return httpx.Response(200, stream=PausedStream(ordinary_advice(context, "progress"), ready, release))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            service, "configured_coach", lambda _: (OpenRouterCoach("fixture", http=http), "openrouter")
        )
        task = asyncio.create_task(
            service.coach_turn(p, call["id"], urgent=True, question="Explain?", request_id=str(uuid4()))
        )
        try:
            assert await asyncio.to_thread(stored.wait, 2)
            task.cancel()
            resume.set()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            with service.db.transaction(False) as s:
                records = service.scoped(s, "coach_events", p)
                assert records
                assert all(service.coach_content(p, {"id": row.id, **row.data}) is None for row in records)
        finally:
            resume.set()
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["unsupported_number", "wrong_provider", "incomplete_string"])
async def test_stream_never_displays_unverified_or_incomplete_primary_fields(
    workspace, app, monkeypatch, unsafe
):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, ready, release = app.state.service, asyncio.Event(), asyncio.Event()
    turn(service, p, call, "progress")

    class InvalidStream(PausedStream):
        async def __aiter__(self):
            if unsafe == "incomplete_string":
                yield frame('{"action":"SHOW_CARD", "say_or_ask":"Unfinished pricing is $')
                ready.set()
                await release.wait()
                yield frame(error={"message": "Incomplete"})
            else:
                async for value in super().__aiter__():
                    yield value

    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        result = ordinary_advice(context, "progress")
        if unsafe == "unsupported_number":
            result["say_or_ask"] = "We guarantee 999 meetings."
        return httpx.Response(
            200,
            stream=InvalidStream(
                result, ready, release, provider="Other" if unsafe == "wrong_provider" else "Cerebras"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            service, "configured_coach", lambda _: (OpenRouterCoach("fixture", http=http), "openrouter")
        )
        task = asyncio.create_task(
            service.coach_turn(p, call["id"], urgent=True, question="Explain?", request_id=str(uuid4()))
        )
        try:
            await asyncio.wait_for(ready.wait(), 2)
            with service.db.transaction(False) as s:
                assert service.scoped(s, "coach_events", p) == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
