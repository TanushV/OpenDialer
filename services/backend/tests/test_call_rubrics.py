"""A39: call-purpose rubrics, immutable anchors, full coverage and correction races."""

from copy import deepcopy

import pytest
from app.db import doc, get, update
from app.providers.inference import InferenceResult
from conftest import start
from test_grounding_review import review_response
from test_remediation_rep_workflow import p


def config(workspace):
    r = workspace["client"].get(
        "/v1/admin/rubrics", headers=workspace["headers"], params={"campaign_id": workspace["campaign_id"]}
    )
    assert r.status_code == 200, r.text
    return r.json()


def save_config(workspace, data):
    return workspace["client"].put("/v1/admin/rubrics", headers=workspace["headers"], json=data)


def choose(workspace, call, call_type):
    return workspace["client"].put(
        f"/v1/calls/{call['id']}/rubric",
        headers=workspace["headers"],
        json={
            "expected_version": call["version"],
            "call_type": call_type,
            "reason": "Rep confirms the actual conversation purpose",
        },
    )


def test_call_type_defaults_have_distinct_observable_anchors(workspace):
    data = config(workspace)
    assert set(data["rubrics"]) == {"cold_introduction", "qualification", "discovery", "follow_up"}
    assert data["default_call_type"] == "cold_introduction"
    signatures = set()
    for rubric in data["rubrics"].values():
        assert rubric["title"] and rubric["purpose"]
        dimensions = rubric["dimensions"]
        assert len({item["id"] for item in dimensions}) == len(dimensions)
        assert all(set(item["anchors"]) == {"0", "1", "2"} and item["weight"] > 0 for item in dimensions)
        signatures.add(str(dimensions))
    assert len(signatures) == 4


@pytest.mark.parametrize(
    "bad", ["duplicate", "zero_weight", "missing_anchor", "empty_rubric", "unknown_type"]
)
def test_invalid_rubrics_never_replace_the_campaign(workspace, bad):
    original = config(workspace)
    body = deepcopy(original)
    dimensions = body["rubrics"]["discovery"]["dimensions"]
    if bad == "duplicate":
        dimensions.append(deepcopy(dimensions[0]))
    elif bad == "zero_weight":
        dimensions[0]["weight"] = 0
    elif bad == "missing_anchor":
        dimensions[0]["anchors"].pop("1")
    elif bad == "empty_rubric":
        dimensions.clear()
    else:
        body["default_call_type"] = "guess_personality"
    assert save_config(workspace, body).status_code == 422
    assert config(workspace) == original


def test_config_versions_conflict_and_existing_call_keeps_its_anchors(workspace):
    original = config(workspace)
    call = start(workspace)
    assert call["rubric_snapshot"]["call_type"] == "cold_introduction"
    body = deepcopy(original)
    body["default_call_type"] = "discovery"
    body["rubrics"]["cold_introduction"]["dimensions"][0]["anchors"]["2"] = "A new approved anchor"
    assert save_config(workspace, body).status_code == 200
    assert save_config(workspace, body).status_code == 409
    analysis = (
        workspace["client"].get(f"/v1/calls/{call['id']}/analysis", headers=workspace["headers"]).json()
    )
    assert analysis["call"]["rubric_snapshot"] == call["rubric_snapshot"]
    assert analysis["scorecard"]["call_type"] == "cold_introduction"
    assert "A new approved anchor" not in str(analysis["scorecard"])


@pytest.mark.parametrize("outcome", ["wrong_party", "voicemail", "screening", "ivr", "no_answer"])
def test_non_sales_outcome_selects_operational_only_scorecard(workspace, app, outcome):
    call = start(workspace)
    service = app.state.service
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), outcome=outcome)
    card = service.analysis(p(workspace), call["id"])["scorecard"]
    assert card["call_type"] == "operational"
    assert card["overall"]["score"] is None and card["overall"]["coverage"] == 0
    assert not card["items"], "Wrong-party and machine calls have no sales performance rating"


def test_rep_purpose_correction_invalidates_scores_but_preserves_human_note_and_reviews(workspace, app):
    call = start(workspace)
    service = app.state.service
    initial = service.analysis(p(workspace), call["id"])
    service.edit_notes(p(workspace), call["id"], initial["notes_version"], "My confirmed human notes")
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.post(
            f"/v1/scorecards/{initial['scorecard_id']}/review",
            headers=h,
            json={
                "reason": "This was a follow-up, not an introduction",
                "status": "disputed",
                "evidence": [],
            },
        ).status_code
        == 200
    )
    response = choose(workspace, initial["call"], "follow_up")
    assert response.status_code == 200, response.text
    assert choose(workspace, initial["call"], "discovery").status_code == 409
    after = service.analysis(p(workspace), call["id"])
    assert after["scorecard"]["call_type"] == "follow_up"
    assert after["scorecard"]["overall"]["coverage"] == 0
    assert after["human_notes"] == "My confirmed human notes"
    assert len(after["reviews"]) == 1
    assert after["reviews"][0]["rubric_version"] == initial["scorecard"]["rubric_version"]


def test_unknown_call_purpose_has_no_manufactured_sales_score(workspace, app):
    call = start(workspace)
    assert choose(workspace, call, "unknown").status_code == 200
    card = app.state.service.analysis(p(workspace), call["id"])["scorecard"]
    assert card["call_type"] == "unknown" and not card["items"]
    assert card["overall"]["score"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change_during_request", [False, True])
async def test_model_uses_selected_dimensions_weights_and_rejects_obsolete_rubric(
    workspace, app, monkeypatch, change_during_request
):
    data = config(workspace)
    data["default_call_type"] = "discovery"
    data["rubrics"]["discovery"]["dimensions"] = [
        {
            "id": "customer_context",
            "title": "Customer context",
            "weight": 2,
            "anchors": {
                "0": "Missed an offered explanation",
                "1": "Asked generally",
                "2": "Explored their workflow",
            },
        },
        {
            "id": "agreed_action",
            "title": "Agreed action",
            "weight": 1,
            "anchors": {
                "0": "No next action despite opportunity",
                "1": "Tentative action",
                "2": "Confirmed a specific action",
            },
        },
    ]
    assert save_config(workspace, data).status_code == 200
    call = start(workspace)
    c, h, service = workspace["client"], workspace["headers"], app.state.service
    assert (
        c.post(
            f"/v1/calls/{call['id']}/transcript",
            headers=h,
            json={
                "segment_id": "discovery-source",
                "revision": 1,
                "is_final": True,
                "side": "remote",
                "text": "Our current process requires each department to approve a request independently, and we want to understand whether a simpler review process could fit the way our team works.",
            },
        ).status_code
        == 200
    )
    requests = []

    class Provider:
        async def complete(self, instructions, context, schema):
            requests.append(deepcopy(context))
            if "fingerprint" in context:
                return InferenceResult(review_response(context), {"cost": 0.001}, "rubric-review")
            if change_during_request:
                with service.db.transaction(False) as s:
                    current = doc(get(s, "call_attempts", call["id"]))
                assert choose(workspace, current, "follow_up").status_code == 200
            return InferenceResult(
                {
                    "summary": "The buyer described decentralized internal approvals.",
                    "summary_evidence": ["discovery-source"],
                    "problems": [],
                    "objections": [],
                    "commitments": [],
                    "unknowns": [],
                    "score_items": [
                        {
                            "dimension": dim["dimension"],
                            "score": 2 if i == 0 else 0,
                            "status": "SCORED",
                            "evidence": ["discovery-source"],
                            "explanation": "Reviewed against the observed opportunity",
                        }
                        for i, dim in enumerate(context["rubric"])
                    ],
                },
                {"cost": 0.001},
                "rubric-extraction",
            )

    monkeypatch.setattr(service, "inference", lambda *_: Provider())
    await service.analyze_postcall(p(workspace), call["id"])
    assert requests[0]["call_type"] == "discovery"
    assert requests[0]["rubric"][0]["anchors"]["2"] == "Explored their workflow"
    result = service.analysis(p(workspace), call["id"])["scorecard"]
    if change_during_request:
        assert result["call_type"] == "follow_up" and result["overall"]["score"] is None
    else:
        assert result["overall"]["coverage"] == 1
        assert result["overall"]["score"] == pytest.approx(200 / 3)
