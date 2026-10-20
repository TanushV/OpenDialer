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
        "context_fingerprint": "fixture-context",
    }


def test_review_has_identified_context_without_promoting_model_memory_to_fact():
    from app.coaching.context import guidance_review_request, upgrade_state

    request = {
        **context(),
        "state": upgrade_state(
            {"version": 7, "needs": [{"id": "need-1", "text": "A workflow concern.", "status": "open"}]}
        ),
        "prior_call_context": [
            {
                "call_id": "prior",
                "version": 3,
                "outcome": "callback_requested",
                "note_id": "note-1",
                "note_version": 4,
                "human_notes": "The rep confirmed a requested follow-up.",
            }
        ],
    }
    review = guidance_review_request(advice(), request)
    sources = {source["id"]: source for source in review["sources"]}
    assert sources["observation:need-1"]["epistemic_status"] == "model_interpreted"
    assert sources["human_note:note-1"]["version"] == 4
    assert sources["outcome:prior"]["version"] == 3


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
        "field_audit": [
            {"field": field, "classification": "nonassertive", "sources": [], "implied_premises": []}
            for field in ("say_or_ask", "reason")
        ],
        "context_fingerprint": "fixture-context",
    }
    assert ground_guidance(card, context())


@pytest.mark.parametrize(
    "change", ["missing_field", "duplicate", "unknown_source", "stale_context", "premise"]
)
def test_audit_identity_and_complete_visible_coverage_are_required(change):
    card = {
        **advice(1, text="What would make evaluation worthwhile?"),
        "evidence_segment_ids": [],
        "reason": "Invite their priorities.",
        "context_fingerprint": "fixture-context",
        "field_audit": [
            {"field": f, "classification": "nonassertive", "sources": [], "implied_premises": []}
            for f in ("say_or_ask", "reason")
        ],
    }
    if change == "missing_field":
        card["field_audit"].pop()
    elif change == "duplicate":
        card["field_audit"].append(dict(card["field_audit"][0]))
    elif change == "unknown_source":
        card["field_audit"][0]["sources"] = [{"kind": "fact", "id": "foreign", "version": 1}]
    elif change == "stale_context":
        card["context_fingerprint"] = "earlier-context"
    else:
        card["field_audit"][0]["implied_premises"] = ["The product works offline."]
    with pytest.raises(CardRejected):
        ground_guidance(card, context())


@pytest.mark.parametrize(
    "changes",
    [
        {"say_or_ask": 7},
        {"playbook_card_ids": [["bad"]]},
        {"evidence_segment_ids": None},
        {"expires_in_ms": "long"},
    ],
)
def test_malformed_legacy_guidance_is_rejected_without_a_runtime_error(changes):
    with pytest.raises(CardRejected):
        ground_guidance({**advice(1), **changes}, context())


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
            if options.get("during_generation"):
                options["during_generation"]()
            self.last_usage, self.last_request_id = {"cost": 0.002}, "generation-receipt"
            result = {**advice(request["state_version"], "claim", text=text), "reason": reason}
            result.update(options.get("result", {}))
            result.setdefault("context_fingerprint", request.get("context_fingerprint"))
            if options.get("progress"):
                await self.on_progress(result)
                options["progress"].set()
                await options["finish_generation"].wait()
            return result

        async def review(self, request, **kwargs):
            requests.append(("review", deepcopy(request)))
            options["review_deadline_ms"] = kwargs.get("deadline_ms")
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
async def test_help_hotkey_can_use_prepared_context_before_any_speech(workspace, app, monkeypatch):
    from test_remediation_coach import ordinary_advice

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, seen = app.state.service, []

    class Provider:
        last_usage, last_request_id = {"cost": 0.001}, "opening-help"
        last_provider, last_model = "Cerebras", "fixture"

        async def suggest(self, request, **kwargs):
            seen.append(request)
            return ordinary_advice(request, evidence_segment_ids=[])

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    result = await service.coach_turn(p, call["id"], urgent=True)
    assert result["action"] == "SHOW_CARD"
    assert len(seen) == 1 and seen[0]["manual_help"] and not seen[0]["recent_dialogue"]
    assert seen[0]["seller"] and seen[0]["prepared_script"]


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
async def test_approved_guarantee_is_reviewed_without_a_phrase_blacklist(workspace, app, monkeypatch):
    service, p, call, requests, _ = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="The approved service includes a guaranteed replacement.",
    )
    with service.db.transaction() as s:
        update(
            service.approved_playbook(s, p),
            product_description="The service includes a guaranteed replacement.",
        )
    result = await service.coach_turn(p, call["id"], urgent=True, question="Explain the replacement terms")
    assert result["action"] == "SHOW_CARD"
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    assert result["meaning_review"]["status"] == "model_reviewed"


@pytest.mark.asyncio
async def test_live_latency_receipt_includes_review_and_coalescing(workspace, app, clock, monkeypatch):
    import time
    from datetime import timedelta

    service, p, call, _, options = review_fixture(workspace, app, monkeypatch)
    options["during"] = lambda: time.sleep(0.04)
    clock.now += timedelta(milliseconds=301)
    result = await service.coach_turn(p, call["id"])
    assert result["action"] == "SHOW_CARD"
    latency = result["latency"]
    assert latency["review_ms"] >= 35
    assert latency["coalescing_ms"] >= 300
    assert latency["total_server_ms"] >= latency["generation_ms"] + latency["review_ms"]
    assert latency["stt_ms"] is None


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
    service, p, call, requests, options = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="What would make evaluating this worthwhile?",
        reason="Invite their priorities.",
    )
    options["result"] = {
        "field_audit": [
            {"field": field, "classification": "nonassertive", "sources": [], "implied_premises": []}
            for field in ("say_or_ask", "reason")
        ]
    }
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
    assert [kind for kind, _ in requests] == ["suggest", "review"], result
    fields = {item["field"] for item in requests[-1][1]["fields"]}
    assert {"say_or_ask", "reason"} <= fields
    if candidate["alternatives"]:
        assert "alternatives.0" in fields
    assert result["action"] == "NO_ACTION"
    assert len(usage(service, p)) == 2


def audit(field, classification="nonassertive"):
    return {"field": field, "classification": classification, "sources": [], "implied_premises": []}


@pytest.mark.asyncio
async def test_slow_generation_keeps_the_full_review_budget(workspace, app, clock, monkeypatch):
    """CI flake: a runner slower than the 3 s first-useful ceiling must not skip the review."""
    from datetime import timedelta

    service, p, call, requests, options = review_fixture(workspace, app, monkeypatch)
    options["during_generation"] = lambda: setattr(clock, "now", clock.now + timedelta(seconds=3.5))
    result = await service.coach_turn(p, call["id"], urgent=True, question="How should I answer?")
    assert [kind for kind, _ in requests] == ["suggest", "review"], result
    assert result["action"] == "SHOW_CARD" and result["meaning_review"]["status"] == "model_reviewed"
    remaining = service.settings.runtime_limits.manual_full_ms - 3500
    assert abs(options["review_deadline_ms"] - remaining) <= 1, "Review uses the full answer budget"


@pytest.mark.asyncio
async def test_non_factual_verdict_cannot_support_an_unaudited_field(workspace, app, monkeypatch):
    service, p, call, requests, _ = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="Our product is compatible with offline use.",
        verdict="NON_FACTUAL",
    )
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I say?")
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    assert not any(field["allows_non_factual"] for field in requests[-1][1]["fields"])
    assert result["action"] == "NO_ACTION", "NON_FACTUAL cannot approve a factual product claim"


@pytest.mark.asyncio
async def test_non_factual_supports_only_the_field_the_generator_audited_nonassertive(
    workspace, app, monkeypatch
):
    service, p, call, requests, options = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="Which access situations matter most to your team?",
        verdict={"say_or_ask": "NON_FACTUAL", "reason": "SUPPORTED"},
    )
    options["result"] = {"field_audit": [audit("say_or_ask"), audit("reason", "supported_fact")]}
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I ask?")
    fields = {f["field"]: f["allows_non_factual"] for f in requests[-1][1]["fields"]}
    assert fields == {"say_or_ask": True, "reason": False}
    assert result["action"] == "SHOW_CARD"
    assert result["reason"] == "Use the approved access capability."


@pytest.mark.asyncio
async def test_rejected_factual_reason_still_shows_the_audited_nonassertive_question(
    workspace, app, monkeypatch
):
    """Spec E01.6: rejection withholds every factual field; an audited clarification may remain."""
    service, p, call, requests, options = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="Which access situations matter most to your team?",
        reason="Our product is compatible with offline use.",
        verdict={"say_or_ask": "NON_FACTUAL", "reason": "CONTRADICTED"},
    )
    options["result"] = {"field_audit": [audit("say_or_ask"), audit("reason", "material_claim")]}
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I ask?")
    assert [kind for kind, _ in requests] == ["suggest", "review"]
    assert result["action"] == "SHOW_CARD"
    assert result["say_or_ask"] == "Which access situations matter most to your team?"
    assert result["reason"] == "" and "offline" not in str(result)
    assert result["meaning_review"]["rejected_fields"] == ["reason"]
    assert len(usage(service, p)) == 2


@pytest.mark.asyncio
async def test_rejected_primary_can_fall_back_to_an_audited_nonassertive_alternative(
    workspace, app, monkeypatch
):
    service, p, call, _, options = review_fixture(
        workspace,
        app,
        monkeypatch,
        text="Our product is compatible with offline use.",
        reason="Our product is compatible with offline use.",
        verdict={"say_or_ask": "CONTRADICTED", "reason": "CONTRADICTED", "alternatives.0": "NON_FACTUAL"},
    )
    clarification = "Which offline situations matter most to your team?"
    options["result"] = {
        "alternatives": [clarification],
        "field_audit": [
            audit("say_or_ask", "material_claim"),
            audit("reason", "material_claim"),
            audit("alternatives.0"),
        ],
    }
    result = await service.coach_turn(p, call["id"], urgent=True, question="What should I say?")
    assert result["action"] == "SHOW_CARD"
    assert result["say_or_ask"] == clarification
    assert result["reason"] == "" and result["alternatives"] == []
    assert "compatible" not in str(result)


@pytest.mark.parametrize("delta", ["commitment", "score"])
def test_commitment_or_rating_update_routes_audited_advice_to_review(delta):
    from app.coaching.context import MeaningReviewRequired

    source = {"kind": "segment", "id": "s1", "version": 1}
    update = {
        "stage": "next_step",
        "observations": [],
        "rubric_items": [],
        "tracker_matches": [],
        "interpreted_segments": [],
    }
    if delta == "commitment":
        update["observations"].append(
            {
                "kind": "commitment",
                "text": "The prospect agreed to a follow-up call.",
                "state": "open",
                "sources": [source],
                "replaces": [],
                "quote": "",
                "commitment": {
                    "kind": "callback",
                    "owner": "rep",
                    "due_at": None,
                    "verbatim_time_expression": "next week",
                    "needs_time_confirmation": True,
                },
            }
        )
    else:
        update["rubric_items"].append(
            {
                "dimension": "discovery",
                "status": "scored",
                "score": 2,
                "explanation": "The rep explored the need.",
                "sources": [source],
            }
        )
    card = {
        **advice(1, text="What would make evaluation worthwhile?"),
        "evidence_segment_ids": [],
        "reason": "Invite their priorities.",
        "context_fingerprint": "fixture-context",
        "field_audit": [audit("say_or_ask"), audit("reason")],
        "state_update": update,
    }
    assert ground_guidance({**card, "state_update": None}, context())
    with pytest.raises(MeaningReviewRequired):
        ground_guidance(card, context())
