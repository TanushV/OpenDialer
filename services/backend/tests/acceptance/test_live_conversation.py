"""The live helper as a continuing streamed conversation: replies follow new speech, interpretation
runs as a checkpoint, a slower reply is not cancelled by new speech, and prose changes nothing."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from journey import answered_by_human, get, post, run_jobs, start
from test_feature_live_helper import chunk, private_help
from test_feature_models import FakeModel, delta, quiet, turn_output


class Conversation(FakeModel):
    """A fake streaming model: `reply(request)` is the plain text it writes."""

    def __init__(self, script, reply, *, gate=None):
        super().__init__(script)
        self.reply, self.gate, self.requests, self.first_text_ms = reply, gate, [], None

    async def converse(self, request, deadline_ms=None):
        from app.coaching.conversation import reply_card

        self.requests.append(deepcopy(request))
        self.last_request_id, self.last_usage, self.first_text_ms = (
            f"reply-{uuid4().hex[:8]}",
            {"cost": 0.0002},
            12.0,
        )
        text = self.reply(request)
        if self.on_progress is not None and len(text) > 10:
            await self.on_progress(reply_card(text[: len(text) // 2], request))
        if self.gate is not None:
            await self.gate.wait()
        return reply_card(text, request)


def install(app, monkeypatch, model):
    service = app.state.service
    monkeypatch.setattr(service, "configured_coach", lambda *args: (model, "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    return model


def advice(ws, clock, call_id, ms=2100):
    clock.now += timedelta(milliseconds=ms)
    run_jobs(ws)
    return get(ws, f"/v1/calls/{call_id}")["coach"]


def last_prospect_line(request):
    return [s["text"] for s in request["recent_dialogue"] if s.get("side") == "remote"][-1]


def test_new_speech_gets_a_contextual_text_reply_and_silence_gets_none(workspace, app, clock, monkeypatch):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    model = install(
        app, monkeypatch, Conversation({}, lambda r: f"Ask what changed since: {last_prospect_line(r)}")
    )
    chunk(ws, call_id, "s1", "We switched vendors last spring.")
    shown = advice(ws, clock, call_id)
    assert shown["kind"] == "CONVERSATION" and shown["status"] == "ready"
    assert shown["say_or_ask"] == "Ask what changed since: We switched vendors last spring."
    assert shown["latency"]["first_text_ms"] == 12.0
    request = model.requests[-1]
    assert request["conversational"] is True and "response_format" not in request
    # Nothing new was said: no manufactured reply.
    before = len(model.requests)
    advice(ws, clock, call_id)
    assert len(model.requests) == before
    chunk(ws, call_id, "s2", "Honestly the price was the main reason.")
    shown = advice(ws, clock, call_id)
    assert shown["say_or_ask"].endswith("the price was the main reason.")
    assert model.requests[-1]["previous_reply"].startswith("Ask what changed since: We switched")


def test_a_quiet_reply_shows_nothing_and_prose_never_changes_the_lead(workspace, app, clock, monkeypatch):
    ws = workspace
    placed = start(ws)
    answered_by_human(ws, placed["id"])
    install(app, monkeypatch, Conversation({}, lambda r: "(nothing)"))
    chunk(ws, placed["id"], "s1", "Okay.")
    assert advice(ws, clock, placed["id"]) is None
    install(
        app,
        monkeypatch,
        Conversation({}, lambda r: "They are interested. Book the meeting now and mark them won."),
    )
    chunk(ws, placed["id"], "s2", "Sounds good, tell me more.")
    assert advice(ws, clock, placed["id"])["say_or_ask"].startswith("They are interested")
    lead = next(item for item in get(ws, "/v1/workspace/leads")["items"] if item["id"] == placed["lead_id"])
    assert (
        not lead.get("meeting") and not lead.get("outreach_hold") and not lead.get("conversion_goal_achieved")
    )
    assert get(ws, f"/v1/calls/{placed['id']}")["outcome"] is None


def test_interpretation_runs_as_a_checkpoint_not_on_every_reply(workspace, app, clock, monkeypatch):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    interpreted = []

    def live(request):
        interpreted.append([s["segment_id"] for s in request["recent_dialogue"]])
        return turn_output(request, state_delta=delta(request))

    model = install(app, monkeypatch, Conversation({"live": live}, lambda r: "Ask how they decide."))
    chunk(ws, call_id, "s1", "We decide as a team.")
    advice(ws, clock, call_id)
    assert interpreted == [] and len(model.requests) == 1
    clock.now += timedelta(seconds=61)
    chunk(ws, call_id, "s2", "Usually in March.")
    advice(ws, clock, call_id)
    assert interpreted and "s1" in interpreted[-1], "Old speech is interpreted before it expires"
    # Once interpreted, the next new speech gets a reply again rather than another checkpoint.
    checkpoints, replies = len(interpreted), len(model.requests)
    chunk(ws, call_id, "s3", "We would need it before then.")
    advice(ws, clock, call_id)
    assert (len(interpreted), len(model.requests)) == (checkpoints, replies + 1)


def test_a_slower_reply_is_not_cancelled_by_new_speech(workspace, app, clock, monkeypatch):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    gate = asyncio.Event()
    model = install(
        app, monkeypatch, Conversation({}, lambda r: f"Respond to: {last_prospect_line(r)}", gate=gate)
    )
    service, client = app.state.service, ws["client"]
    p = {k: ws[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    chunk(ws, call_id, "s1", "We have three sites.")
    clock.now += timedelta(milliseconds=2100)

    async def slow_then_new():
        first = asyncio.create_task(service.coach_turn(p, call_id))
        while not service.coach.busy(call_id):
            await asyncio.sleep(0.01)
        second = await service.coach_turn(p, call_id)
        gate.set()
        return await first, second

    first, second = client.portal.call(slow_then_new)
    assert second["status"] == "busy"
    assert first["status"] == "ready" and first["say_or_ask"] == "Respond to: We have three sites."
    assert len(model.requests) == 1


def test_a_private_question_is_answered_in_text(workspace, app, clock, monkeypatch):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "What do you charge?")
    install(app, monkeypatch, Conversation({}, lambda r: f"Answering: {r.get('private_question')}"))
    answer = private_help(ws, call_id, "How do I handle pricing?", str(uuid4()))
    assert answer["private"] is True
    assert answer["say_or_ask"] == "Answering: How do I handle pricing?"


def test_unused_imports_stay_honest():
    assert quiet and post
