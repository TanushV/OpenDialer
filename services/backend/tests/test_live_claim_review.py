"""A24/A26: generated facts need meaning checks, not matching words or a phrasebook."""

import asyncio
from copy import deepcopy

import pytest
from app.coaching.context import ground_guidance
from app.coaching.engine import CardRejected
from app.db import update
from test_grounding_review import review_response
from test_live_memory import setup, turn
from test_remediation_coach import advice

KNOWLEDGE = "Our product is not compatible with offline use. It supports browser access."


def context():
    return {
        "state_version": 1,
        "private_question": "Help me answer",
        "seller": {"knowledge": KNOWLEDGE},
        "recent_dialogue": [],
    }


@pytest.mark.parametrize(
    "field,text",
    [
        ("say_or_ask", "Our product is compatible with offline use."),
        ("say_or_ask", "How can our offline product support your team?"),
        ("reason", "Our product is compatible with offline use."),
    ],
)
def test_word_overlap_or_a_question_mark_cannot_certify_a_novel_claim(field, text):
    card = {**advice(1, text="What would make that useful?"), "evidence_segment_ids": [], field: text}
    with pytest.raises(CardRejected):
        ground_guidance(card, context())


def test_a_novel_nonassertive_question_keeps_the_single_request_path():
    card = {
        **advice(1, text="What would make evaluating this worth your time?"),
        "evidence_segment_ids": [],
        "reason": "Invite the prospect to explain priorities.",
    }
    assert ground_guidance(card, context())


def review_fixture(
    workspace,
    app,
    monkeypatch,
    *,
    text="Our tool can be used through a web browser.",
    reason="Use the approved access capability.",
    verdict="SUPPORTED",
):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, requests, options = app.state.service, [], {}
    with service.db.transaction() as s:
        book = service.approved_playbook(s, p)
        update(book, product_description=KNOWLEDGE)
    turn(service, p, call, "claim", text="Does this work when the internet is disconnected?")

    class Provider:
        last_usage, last_request_id = {}, None
        last_provider, last_model = "Cerebras", "fixture"
        on_progress = None

        async def suggest(self, request, **kwargs):
            requests.append(("suggest", deepcopy(request)))
            self.last_usage, self.last_request_id = {"cost": 0.002}, "generation-receipt"
            result = {**advice(request["state_version"], "claim", text=text), "reason": reason}
            result.update(options.get("result", {}))
            if options.get("progress"):
                await self.on_progress(result)
                options["progress"].set()
                await options["finish_generation"].wait()
            return result

        async def review(self, request, **kwargs):
            requests.append(("review", deepcopy(request)))
            if options.get("during"):
                options["during"]()
            if options.get("review_started"):
                options["review_started"].set()
                await options["finish_review"].wait()
            self.last_usage, self.last_request_id = {"cost": 0.001}, "review-receipt"
            return review_response(request, verdict=verdict)

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    return service, p, call, requests, options


def usage(service, p):
    with service.db.transaction(False) as s:
        return [
            r.data
            for r in service.scoped(s, "usage_ledger", p)
            if r.data.get("work_kind") in {"live_coach", "live_coach_review"}
        ]


@pytest.mark.asyncio
async def test_reviewed_freeform_paraphrase_publishes_and_keeps_separate_receipts(
    workspace, app, monkeypatch
):
    service, p, call, requests, _ = review_fixture(workspace, app, monkeypatch)
    result = await service.coach_turn(p, call["id"], urgent=True, question="How should I answer?")
    assert result["action"] == "SHOW_CARD"
    assert result["say_or_ask"] == "Our tool can be used through a web browser."
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    assert {f["field"] for f in requests[-1][1]["fields"]} == {"say_or_ask", "reason"}
    assert result["meaning_review"]["status"] == "model_reviewed"
    charges = usage(service, p)
    assert len(charges) == 2 and all(row["status"] == "settled" for row in charges)
    assert sorted(row["cost_usd"] for row in charges) == [0.001, 0.002]


@pytest.mark.asyncio
async def test_review_rejects_contradiction_despite_identical_words(workspace, app, monkeypatch):
    service, p, call, requests, _ = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="Our product is compatible with offline use.",
        verdict="CONTRADICTED",
    )
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I say?")
    assert result["action"] == "NO_ACTION"
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    assert len(usage(service, p)) == 2


@pytest.mark.asyncio
async def test_ordinary_open_question_does_not_add_an_inference_request(workspace, app, monkeypatch):
    service, p, call, requests, _ = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="What would make evaluating this worthwhile?",
        reason="Invite their priorities.",
    )
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I ask?")
    assert result["action"] == "SHOW_CARD"
    assert [kind for kind, _ in requests] == ["suggest"]
    assert len(usage(service, p)) == 1


@pytest.mark.asyncio
async def test_review_has_its_own_budget_admission(workspace, app, monkeypatch):
    service, p, call, requests, _ = review_fixture(
        workspace, app, monkeypatch, text="Our product is compatible with offline use."
    )
    reserve = service._reserve_coach

    async def budget(p, key, object_id, kind):
        return False if kind == "live_coach_review" else await reserve(p, key, object_id, kind)

    monkeypatch.setattr(service, "_reserve_coach", budget)
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I say?")
    assert result["action"] == "NO_ACTION"
    assert [kind for kind, _ in requests] == ["suggest"]
    assert len(usage(service, p)) == 1


@pytest.mark.asyncio
async def test_changed_sources_during_review_cannot_publish_but_both_requests_are_settled(
    workspace, app, monkeypatch
):
    service, p, call, requests, options = review_fixture(workspace, app, monkeypatch)

    def change():
        with service.db.transaction() as s:
            book = service.approved_playbook(s, p)
            update(book, product_description="This capability has been withdrawn.")

    options["during"] = change
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I say?")
    assert result["action"] == "NO_ACTION"
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    assert len(usage(service, p)) == 2


@pytest.mark.asyncio
async def test_early_factual_stream_is_not_displayed_before_meaning_review(workspace, app, monkeypatch):
    service, p, call, requests, options = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="Our product is compatible with offline use.",
        verdict="CONTRADICTED",
    )
    options.update(progress=asyncio.Event(), finish_generation=asyncio.Event())
    task = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True, question="Help now"))
    try:
        await asyncio.wait_for(options["progress"].wait(), 2)
        with service.db.transaction(False) as s:
            assert not service.scoped(s, "coach_events", p)
        options["finish_generation"].set()
        assert (await task)["action"] == "NO_ACTION"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_review_retains_its_uncertain_cost_and_generation_receipt(
    workspace, app, monkeypatch
):
    service, p, call, _, options = review_fixture(workspace, app, monkeypatch)
    options.update(review_started=asyncio.Event(), finish_review=asyncio.Event())
    task = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True, question="Help now"))
    try:
        await asyncio.wait_for(options["review_started"].wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        charges = usage(service, p)
        by_kind = {row["work_kind"]: row for row in charges}
        assert by_kind["live_coach"]["cost_usd"] == 0.002
        assert by_kind["live_coach_review"]["status"] == "uncertain"
        assert by_kind["live_coach_review"]["reserved_usd"] == 0.01
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("visible_field", ["say_or_ask", "reason", "alternatives.0", "implied_premise"])
@pytest.mark.asyncio
async def test_complete_field_audit(workspace, app, monkeypatch, visible_field):
    """C001: a generator cannot omit the field which contains the false premise."""
    service, p, call, requests, options = review_fixture(workspace, app, monkeypatch, verdict="CONTRADICTED")
    unsupported = "Our product works without an internet connection."
    candidate = {
        "say_or_ask": "What would make evaluation worthwhile?",
        "reason": "Invite their priorities.",
        "alternatives": [],
        "field_audit": [],  # Deliberately incomplete: independent review is mandatory.
        "claims": [],
    }
    if visible_field == "alternatives.0":
        candidate["alternatives"] = [unsupported]
    elif visible_field == "implied_premise":
        candidate["say_or_ask"] = "How would our offline access help you?"
    else:
        candidate[visible_field] = unsupported
    options["result"] = candidate
    result = await service.coach_turn(p, call["id"], urgent=True, question="Help now")
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    fields = {item["field"] for item in requests[-1][1]["fields"]}
    assert {"say_or_ask", "reason"} <= fields
    if candidate["alternatives"]:
        assert "alternatives.0" in fields
    assert result["action"] == "NO_ACTION"
    assert len(usage(service, p)) == 2
