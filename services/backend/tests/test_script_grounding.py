"""Inspect all visible script fields, including assertion-bearing questions."""

from copy import deepcopy
from datetime import timedelta

import pytest
from app.providers.inference import InferenceResult
from app.research.validation import ValidationFailure, validate_script
from test_grounding_review import review_response
from test_remediation_rep_workflow import p


def baseline():
    return {
        "lead_id": "lead",
        "opener": "Hello, this is Ada from Seller.",
        "screening_intro": "Ada from Seller, calling with a business introduction.",
        "gatekeeper": "Who handles this area?",
        "discovery_questions": ["What would make this useful?"],
        "proof": "",
        "closing_question": "Would a further conversation be useful?",
        "objection_cards": [],
        "claims": [],
        "fact_ids_used": [],
    }


@pytest.mark.parametrize(
    "field",
    [
        "opener",
        "screening_intro",
        "gatekeeper",
        "proof",
        "closing_question",
        "discovery_questions",
        "objection_cards",
    ],
)
def test_omitted_claims_do_not_exempt_any_visible_field(field, clock):
    candidate = baseline()
    text = "Your company has opened a new office in Seattle."
    candidate[field] = (
        [text]
        if field == "discovery_questions"
        else [{"objection": "Not a priority", "response": text}]
        if field == "objection_cards"
        else text
    )
    if field == "screening_intro":
        candidate[field] += " I am calling from Seller."
    with pytest.raises(ValidationFailure):
        validate_script(candidate, [], "example.test", "Seller", clock.now, approved=baseline())


def test_an_assertion_in_a_question_is_not_automatically_safe(clock):
    candidate = baseline()
    candidate["discovery_questions"] = ["How is your newly approved million-dollar budget being spent?"]
    with pytest.raises(ValidationFailure):
        validate_script(candidate, [], "example.test", "Seller", clock.now, approved=baseline())


def test_unchanged_authorized_text_does_not_need_paid_review(clock):
    assert validate_script(baseline(), [], "example.test", "Seller", clock.now, approved=baseline())


def test_freeform_supported_script_passes_full_field_review(clock):
    from app.grounding import accept_review
    from app.research.validation import script_review_request

    candidate = baseline()
    candidate["opener"] = "Your team serves commercial repair customers."
    candidate["fact_ids_used"] = ["f"]
    facts = [
        {
            "fact_id": "f",
            "statement": "We provide commercial repair.",
            "entity_domain": "example.test",
            "source_id": "source",
            "status": "supported",
            "expires_at": (clock.now + timedelta(days=1)).isoformat(),
        }
    ]
    request = script_review_request(candidate, facts, "example.test", "Seller", approved=baseline())
    assert [f["field"] for f in request["fields"]] == ["opener"]
    receipt = accept_review(request, review_response(request), "reviewed-script")
    assert validate_script(
        candidate, facts, "example.test", "Seller", clock.now, approved=baseline(), review=receipt
    )
    changed = deepcopy(candidate)
    changed["opener"] += " You are growing rapidly."
    with pytest.raises(ValidationFailure):
        validate_script(
            changed, facts, "example.test", "Seller", clock.now, approved=baseline(), review=receipt
        )
    facts[0]["statement"] = "We no longer provide commercial repair."
    with pytest.raises(ValidationFailure):
        validate_script(
            candidate, facts, "example.test", "Seller", clock.now, approved=baseline(), review=receipt
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SUPPORTED", "UNSUPPORTED"])
async def test_legacy_script_generation_reviews_novel_fields_before_publish(
    workspace, app, monkeypatch, verdict
):
    service = app.state.service
    lead = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]
    calls = []

    class Provider:
        async def complete(self, instructions, context, schema):
            calls.append(context)
            if "fingerprint" in context:
                response = review_response(context, verdict=verdict)
                if verdict == "SUPPORTED":
                    for item in response["verdicts"]:
                        item["verdict"] = "NON_FACTUAL"
                return InferenceResult(response, {"cost": 0.002}, "script-review")
            value = {k: context["approved_fallback"][k] for k in schema["properties"]}
            value["discovery_questions"] = ["Where would better support be most useful to your team?"]
            return InferenceResult(value, {"cost": 0.001}, "script-generation")

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    result = await service.prepare(p(workspace), [lead["id"]])
    assert result["failed"] == ([] if verdict == "SUPPORTED" else [lead["id"]])
    assert len(calls) == 2
    packet = service.packet(p(workspace), lead["id"])
    assert packet["script"]["fallback"] is (verdict != "SUPPORTED")
    if verdict == "SUPPORTED":
        assert packet["script"]["discovery_questions"] == [
            "Where would better support be most useful to your team?"
        ]
    with service.db.transaction(False) as s:
        costs = [
            r.data
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "grounding_review"
        ]
    assert len(costs) == 1 and costs[0]["cost_usd"] == pytest.approx(0.002)


def test_legacy_unreviewed_personalization_is_not_republished(workspace, app):
    from app.db import put, update

    service = app.state.service
    lead_id = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"]
    with service.db.transaction() as s:
        lead = service.required(s, "leads", lead_id, p(workspace))
        old = service.fallback_script(s, p(workspace), lead)
        old.update(fallback=False, opener="Your new Seattle office is expanding rapidly.")
        row = put(
            s,
            "script_versions",
            p(workspace)["tenant_id"],
            p(workspace)["seller_id"],
            old,
            now=service.clock(),
        )
        update(
            lead,
            preparation={
                "script_id": row.id,
                "status": "ready",
                "key": service.preparation_key(s, p(workspace), lead),
            },
        )
    packet = service.packet(p(workspace), lead_id)
    assert packet["script"]["fallback"] is True
    assert packet["preparation"]["status"] == "stale"


@pytest.mark.parametrize(
    "text", ["Results are not guaranteed.", "Never say as we discussed unless it is true."]
)
def test_approved_script_qualifications_are_not_rejected_by_keyword(text, clock):
    approved = baseline()
    approved["proof"] = text
    assert validate_script(approved, [], "example.test", "Seller", clock.now, approved=approved)
