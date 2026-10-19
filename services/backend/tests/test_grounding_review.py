"""Publication requires independent meaning review, not just a matching quote."""

from datetime import timedelta

import pytest
from app.db import doc
from app.preparation.templates import ScriptTemplate, fill
from app.providers.inference import InferenceResult
from test_postcall_completion import (
    ConversationProvider,
    analysis_body,
    conversation_turn,
    setup_analysis,
    stage_review,
)
from test_remediation_rep_workflow import p


def review_response(request, *, verdict="SUPPORTED"):
    return {
        "fingerprint": request["fingerprint"],
        "verdicts": [
            {
                "field": item["field"],
                "verdict": verdict,
                "evidence": item["evidence"] or [source["id"] for source in request["sources"]],
            }
            for item in request["fields"]
        ],
    }


def source():
    return {
        "segment_id": "s",
        "revision": 1,
        "side": "remote",
        "is_final": True,
        "text": "Please call me on October 20, 2026 at 2 PM Pacific time.",
    }


def test_matching_evidence_does_not_authorize_unreviewed_paraphrase(workspace, app, monkeypatch):
    service, who, call, _, _, _ = stage_review(workspace, app, monkeypatch)
    result = service.analysis(who, call["id"])
    assert result["grounding_status"] == "review_required"
    assert "future conversation was requested" not in result["notes"]["summary"]
    with service.db.transaction(False) as session:
        assert not service.lead_records(session, who, "commitments", call["lead_id"])


@pytest.mark.parametrize("change", [None, "revision", "text", "side", "candidate"])
def test_complete_review_accepts_freeform_paraphrase_and_binds_source_version(
    workspace, app, monkeypatch, change
):
    from app.db import update
    from app.grounding import accept_review

    service, who, call, request, owner, review = stage_review(
        workspace, app, monkeypatch, analysis_body("Reconnect about their evaluation")
    )
    receipt = accept_review(review, review_response(review), "independent-review")
    if change in {"revision", "text", "side"}:
        changed = source() | {"revision": 2}
        if change == "revision":
            changed["text"] += " We will review the pricing then."
        if change == "text":
            changed["text"] = "No callback was agreed."
        if change == "side":
            changed["side"] = "rep"
        service._receive_transcript(who, call["id"], changed, None, True)
    elif change == "candidate":
        from copy import deepcopy

        with service.db.transaction() as s:
            row = service._by_key(s, who, "conversation_states", call["id"])
            state = deepcopy(row.data)
            for item in state["observations"]:
                item["text"] = "They agreed to purchase."
            update(row, **state)
    service._publish_state_review(who, call["id"], request, owner, receipt)
    service.analysis(who, call["id"])
    with service.db.transaction(False) as session:
        promises = [i.data for i in service.lead_records(session, who, "commitments", call["lead_id"])]
    if change:
        assert promises == []
    else:
        assert promises[0]["action"] == "Reconnect about their evaluation"
        assert promises[0]["due_at"] == "2026-10-20T21:00:00+00:00"


@pytest.mark.parametrize(
    "change",
    [
        "omitted",
        "duplicate",
        "unknown",
        "unsupported",
        "uncertain",
        "contradiction",
        "bad_source",
        "wrong_digest",
        "empty_receipt",
    ],
)
def test_incomplete_or_unsafe_reviews_cannot_publish(change, workspace, app, monkeypatch):
    from app.grounding import accept_review

    *_, request = stage_review(workspace, app, monkeypatch)
    review = review_response(request)
    if change == "omitted":
        review["verdicts"].pop()
    elif change == "duplicate":
        review["verdicts"].append(review["verdicts"][0])
    elif change == "unknown":
        review["verdicts"][0]["field"] = "unrequested"
    elif change in {"unsupported", "uncertain", "contradiction"}:
        review["verdicts"][0]["verdict"] = {
            "unsupported": "UNSUPPORTED",
            "uncertain": "UNCERTAIN",
            "contradiction": "CONTRADICTED",
        }[change]
    elif change == "bad_source":
        review["verdicts"][0]["evidence"] = ["different-call"]
    elif change == "wrong_digest":
        review["fingerprint"] = "old-source"
    with pytest.raises(ValueError):
        accept_review(request, review, None if change == "empty_receipt" else "review-1")


def test_every_visible_analysis_field_is_reviewed_including_omitted_assertions(workspace, app, monkeypatch):
    from app.postcall.analysis import DIMENSIONS

    body = analysis_body()
    body["summary"] += " Their budget was approved."
    body["problems"] = [
        {
            "text": "Procurement is delayed",
            "quote": "Please call me",
            "evidence": ["s"],
            "review_required": True,
        }
    ]
    body["objections"] = [
        {"text": "Timing constraint", "quote": "Please call me", "evidence": ["s"], "review_required": True}
    ]
    body["unknowns"] = ["They never discussed security"]
    body["score_items"] = [
        {
            "dimension": dim,
            "score": None,
            "status": "N/A",
            "evidence": ["s"],
            "explanation": "No opportunity was offered",
        }
        for dim in DIMENSIONS
    ]
    *_, request = stage_review(workspace, app, monkeypatch, body)
    fields = request["fields"]
    assert len(fields) == 5 + len(DIMENSIONS)
    assert any("budget was approved" in str(f["value"]) for f in fields)
    assert any(
        f["value"].get("commitment", {}).get("owner") == "rep" for f in fields if f["value"].get("commitment")
    )
    assert any(f["value"].get("status") == "not_applicable" for f in fields)
    assert any(f["value"].get("text") == "They never discussed security" for f in fields)


@pytest.mark.parametrize("slot_text", ["We are certified.", "are certified", "certified"])
def test_template_cannot_drop_negation_by_copying_a_substring(slot_text, clock):
    from app.research.validation import validate_script

    facts = [{"fact_id": "f", "statement": "We are not certified.", "status": "supported",
              "source_id": "source", "entity_domain": "example.test",
              "expires_at": (clock.now + timedelta(days=1)).isoformat()}]
    candidate = {
        "lead_id": "lead",
        "slots": {"relevance": {"text": slot_text, "fact_ids": ["f"]}, "proof": {"text": "", "fact_ids": []}},
    }
    with pytest.raises(ValueError):
        script = fill(
            candidate,
            ScriptTemplate().model_dump(),
            lead={"id": "lead"},
            seller="Seller",
            rep="Rep",
            facts=facts,
        )
        validate_script(script, facts, "example.test", "Seller", clock.now)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SUPPORTED", "CONTRADICTED", "UNCERTAIN"])
async def test_postcall_review_controls_publication_tasks_and_its_own_usage(
    workspace, app, monkeypatch, verdict
):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    state["verdict"] = verdict
    await service.analyze_postcall(p(workspace), call["id"])
    requests = [r for r in state["contexts"] if "fingerprint" in r]
    assert len(requests) == 1
    assert requests[0]["kind"] == "conversation"
    view = service.analysis(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        tasks = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
        note = service._by_key(s, p(workspace), "notes", call["id"])
        assert bool(tasks) is (verdict == "SUPPORTED")
        if verdict == "SUPPORTED":
            assert view["notes"]["summary"] == state["body"]["summary"]
            assert view["grounding_status"] == "model_reviewed"
        else:
            assert view["notes"]["summary"] != state["body"]["summary"]
            assert view["grounding_status"] == "review_required"
        assert state["body"]["summary"] not in str(note.data), (
            "Derived prose lives once in conversation state"
        )
        usage = [
            doc(r)
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "conversation_review"
        ]
    assert len(usage) == 1
    assert usage[0]["request_id"] == "review-receipt"
    assert usage[0]["cost_usd"] == pytest.approx(0.002)
    assert "Please call me" not in str(usage)


@pytest.mark.asyncio
async def test_missing_review_provider_cannot_create_inferred_task(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    state["missing_review"] = True
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        assert not service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])


@pytest.mark.asyncio
async def test_review_completion_cannot_extend_raw_lifetime(workspace, app, monkeypatch, clock):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service

    class SlowReview(ConversationProvider):
        async def review(self, context, **kwargs):
            clock.now += timedelta(seconds=301)
            self.last_usage, self.last_request_id = {"cost": 0.003}, "late-review"
            return review_response(context)

    monkeypatch.setattr(
        service,
        "configured_coach",
        lambda *args: (SlowReview(state), "openrouter"),
    )
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        assert not service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
        usage = [
            r
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("request_id") == "late-review"
        ]
        assert len(usage) == 1
    assert service.analysis(p(workspace), call["id"])["transcript"] == []


@pytest.mark.asyncio
async def test_source_correction_invalidates_the_review_status(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    assert service.analysis(p(workspace), call["id"])["grounding_status"] == "model_reviewed"
    await service.transcript(
        p(workspace),
        call["id"],
        {
            "segment_id": "s",
            "revision": 2,
            "is_final": True,
            "side": "remote",
            "text": "There is no agreed callback.",
        },
        defer_coach=True,
    )
    assert service.analysis(p(workspace), call["id"])["grounding_status"] == "unreviewed"


@pytest.mark.asyncio
async def test_extraction_deadline_cannot_outlive_original_evidence_expiry(
    workspace, app, monkeypatch, clock
):
    import asyncio
    import time

    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    clock.now += timedelta(seconds=299.95)
    cancelled = []

    class SlowExtractor(ConversationProvider):
        async def suggest(self, context, **kwargs):
            try:
                await asyncio.sleep(0.7)
                return conversation_turn(analysis_body(), context)
            finally:
                cancelled.append(True)

    monkeypatch.setattr(
        service,
        "configured_coach",
        lambda *args: (SlowExtractor(state), "openrouter"),
    )
    started = time.monotonic()
    await service.analyze_postcall(p(workspace), call["id"])
    assert time.monotonic() - started < 0.5
    assert cancelled == [True]
    with service.db.transaction(False) as s:
        usage = [
            r.data
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "conversation_finalize"
        ]
    assert len(usage) == 1 and usage[0]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_review_does_not_submit_after_budget_wait_consumed_lifetime():
    import time
    from types import SimpleNamespace

    from app.grounding import review_generated, review_request

    calls, released = [], []
    request = review_request(
        "analysis",
        [{"field": "summary", "value": "A short summary", "evidence": ["s"]}],
        [{"id": "s", "text": "A source"}],
    )

    class Provider:
        async def complete(self, *args):
            calls.append(True)
            return InferenceResult(review_response(request), {}, "expired-request")

    def reserve(*args, **kwargs):
        time.sleep(0.04)
        return True

    service = SimpleNamespace(
        inference=lambda *args: Provider(),
        reserve_optional_budget=reserve,
        usage=lambda *args, **kwargs: None,
        release_unsubmitted_budget=lambda *args: released.append(True),
    )
    with pytest.raises(ValueError):
        await review_generated(
            service,
            {"tenant_id": "t", "user_id": "u"},
            request,
            object_id="c",
            role="scorecard_review",
            seconds=0.01,
        )
    assert calls == []
    assert released == [True]
