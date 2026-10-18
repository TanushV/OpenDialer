"""Publication requires independent meaning review, not just a matching quote."""

from copy import deepcopy
from datetime import timedelta

import pytest
from app.db import doc
from app.postcall.inference import validate_analysis
from app.preparation.templates import ScriptTemplate, fill
from app.providers.inference import InferenceResult
from test_postcall_completion import analysis_body, setup_analysis
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


def test_matching_evidence_does_not_authorize_unreviewed_paraphrase():
    with pytest.raises(ValueError, match="review"):
        validate_analysis(analysis_body(), [source()])


def test_complete_review_accepts_freeform_paraphrase_and_binds_source_version():
    from app.grounding import accept_review
    from app.postcall.inference import analysis_review_request

    body = analysis_body("Reconnect about their evaluation")
    request = analysis_review_request(body, [source()])
    receipt = accept_review(request, review_response(request), "independent-review")
    result = validate_analysis(body, [source()], review=receipt)
    assert result["commitments"][0]["action"] == "Reconnect about their evaluation"
    assert result["commitments"][0]["due_at"] == "2026-10-20T21:00:00+00:00"
    for changed in [
        {**source(), "revision": 2},
        {**source(), "text": "No callback was agreed."},
        {**source(), "side": "rep"},
    ]:
        with pytest.raises(ValueError):
            validate_analysis(body, [changed], review=receipt)
    changed_body = {**body, "summary": "They agreed to purchase."}
    with pytest.raises(ValueError):
        validate_analysis(changed_body, [source()], review=receipt)


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
def test_incomplete_or_unsafe_reviews_cannot_publish(change):
    from app.grounding import accept_review
    from app.postcall.inference import analysis_review_request

    request = analysis_review_request(analysis_body(), [source()])
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


def test_every_visible_analysis_field_is_reviewed_including_omitted_assertions():
    from app.postcall.analysis import DIMENSIONS
    from app.postcall.inference import analysis_review_request

    body = analysis_body()
    body["summary"] += " Their budget was approved."
    body["problems"] = [{"text": "Procurement is delayed", "quote": "Please call me", "evidence": ["s"]}]
    body["objections"] = [{"text": "Timing constraint", "quote": "Please call me", "evidence": ["s"]}]
    body["unknowns"] = ["They never discussed security"]
    body["score_items"] = [
        {
            "dimension": dim,
            "score": None,
            "status": "N/A",
            "evidence": [],
            "explanation": "No opportunity was offered",
        }
        for dim in DIMENSIONS
    ]
    request = analysis_review_request(body, [source()])
    fields = {item["field"]: item for item in request["fields"]}
    assert set(fields) == {
        "summary",
        "problems/0",
        "objections/0",
        "commitments/0",
        "unknowns/0",
        *(f"score_items/{i}" for i in range(len(DIMENSIONS))),
    }
    assert "budget was approved" in fields["summary"]["value"]
    assert fields["commitments/0"]["value"]["owner"] == "rep"
    assert fields["score_items/0"]["value"]["status"] == "N/A"


@pytest.mark.parametrize("slot_text", ["We are certified.", "are certified", "certified"])
def test_template_cannot_drop_negation_by_copying_a_substring(slot_text):
    facts = [{"fact_id": "f", "statement": "We are not certified."}]
    candidate = {
        "lead_id": "lead",
        "slots": {"relevance": {"text": slot_text, "fact_ids": ["f"]}, "proof": {"text": "", "fact_ids": []}},
    }
    with pytest.raises(ValueError):
        fill(
            candidate,
            ScriptTemplate().model_dump(),
            lead={"id": "lead"},
            seller="Seller",
            rep="Rep",
            facts=facts,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SUPPORTED", "CONTRADICTED", "UNCERTAIN"])
async def test_postcall_review_controls_publication_tasks_and_its_own_usage(
    workspace, app, monkeypatch, verdict
):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    requests = []

    class Reviewer:
        async def complete(self, instructions, context, schema):
            requests.append((instructions, deepcopy(context)))
            return InferenceResult(
                review_response(context, verdict=verdict), {"cost": 0.002}, "review-receipt"
            )

    extractor = service.inference
    monkeypatch.setattr(
        service,
        "inference",
        lambda tenant, role, user=None: (
            Reviewer() if role == "scorecard_review" else extractor(tenant, role, user)
        ),
    )
    await service.analyze_postcall(p(workspace), call["id"])
    assert len(requests) == 1
    assert "contradict" in requests[0][0].lower()
    with service.db.transaction(False) as s:
        tasks = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
        note = service._by_key(s, p(workspace), "notes", call["id"])
        assert bool(tasks) is (verdict == "SUPPORTED")
        if verdict == "SUPPORTED":
            assert note.data["notes"]["summary"] == state["body"]["summary"]
            assert note.data["notes"]["grounding"]["status"] == "model_reviewed"
        else:
            assert note.data["notes"]["summary"] != state["body"]["summary"]
            assert note.data["grounding_status"] == "review_required"
        usage = [
            doc(r)
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "grounding_review"
        ]
    assert len(usage) == 1
    assert usage[0]["request_id"] == "review-receipt"
    assert usage[0]["cost_usd"] == pytest.approx(0.002)
    assert "Please call me" not in str(usage)


@pytest.mark.asyncio
async def test_missing_review_provider_cannot_create_inferred_task(workspace, app, monkeypatch):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    extractor = service.inference
    monkeypatch.setattr(
        service,
        "inference",
        lambda tenant, role, user=None: None if role == "scorecard_review" else extractor(tenant, role, user),
    )
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        assert not service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])


@pytest.mark.asyncio
async def test_review_completion_cannot_extend_raw_lifetime(workspace, app, monkeypatch, clock):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    extractor = service.inference

    class SlowReview:
        async def complete(self, instructions, context, schema):
            clock.now += timedelta(seconds=301)
            return InferenceResult(review_response(context), {"cost": 0.003}, "late-review")

    monkeypatch.setattr(
        service,
        "inference",
        lambda tenant, role, user=None: (
            SlowReview() if role == "scorecard_review" else extractor(tenant, role, user)
        ),
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
    call, _ = setup_analysis(workspace, app, monkeypatch)
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

    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    clock.now += timedelta(seconds=299.95)
    cancelled = []

    class SlowExtractor:
        async def complete(self, *args):
            try:
                await asyncio.sleep(0.7)
                return InferenceResult(analysis_body(), {}, "too-late")
            finally:
                cancelled.append(True)

    monkeypatch.setattr(
        service,
        "inference",
        lambda tenant, role, user=None: SlowExtractor() if role == "postcall_extract" else None,
    )
    started = time.monotonic()
    await service.analyze_postcall(p(workspace), call["id"])
    assert time.monotonic() - started < 0.5
    assert cancelled == [True]
    with service.db.transaction(False) as s:
        usage = [
            r.data
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "postcall_extract"
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
