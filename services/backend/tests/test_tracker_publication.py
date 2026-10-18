"""Semantic trackers share the reviewed analysis and cannot outlive corrected evidence."""

from copy import deepcopy
from datetime import timedelta

import pytest
from app.db import get, update
from app.providers.inference import InferenceResult
from test_grounding_review import review_response
from test_postcall_completion import setup_analysis
from test_remediation_rep_workflow import p


def setup(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    result = workspace["client"].post(
        "/v1/admin/trackers",
        headers=workspace["headers"],
        json={
            "name": "Explicit callback",
            "kind": "semantic",
            "definition": "The prospect agrees to a future phone call",
            "positive_examples": ["Please call next week"],
            "negative_examples": ["I will not take another call"],
        },
    )
    assert result.status_code == 200, result.text
    tracker = result.json()
    state["body"]["tracker_matches"] = [{"tracker_id": tracker["id"], "evidence": ["s"]}]
    requests = []

    class Provider:
        async def complete(self, instructions, context, schema):
            requests.append(deepcopy(context))
            if "fingerprint" in context:
                if state.get("during_review"):
                    state["during_review"]()
                return InferenceResult(review_response(context), {"cost": 0.001}, "tracker-review")
            # Accommodate the old standalone path so the regression detects its extra request.
            if "rubric" not in context:
                return InferenceResult(
                    {"matches": state["body"]["tracker_matches"]}, {"cost": 0.001}, "extra-tracker"
                )
            return InferenceResult(state["body"], {"cost": 0.001}, "tracker-extract")

    monkeypatch.setattr(app.state.service, "inference", lambda *_: Provider())
    return call, tracker, state, requests


@pytest.mark.asyncio
async def test_tracker_shares_one_extraction_and_meaning_review_with_original_versions(
    workspace, app, monkeypatch, clock
):
    call, tracker, _, requests = setup(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    assert len(requests) == 2, "Tracker detection must not add another inference round trip"
    assert requests[0]["trackers"][0]["id"] == tracker["id"]
    assert any(item["field"] == "tracker_matches/0" for item in requests[1]["fields"])
    assert requests[1]["context"]["rubric"]["dimensions"], "Meaning review needs the actual scoring anchors"
    result = service.analysis(p(workspace), call["id"])["trackers"]
    assert len(result) == 1
    assert result[0]["version"] == tracker["version"]
    assert result[0]["evidence_revisions"] == {"s": 1}
    clock.now += timedelta(seconds=301)
    result = service.analysis(p(workspace), call["id"])["trackers"]
    assert len(result) == 1 and result[0]["raw_evidence_status"] == "expired"
    assert "Please call me" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "source", "definition", "deleted"])
async def test_tracker_publication_rechecks_expiry_and_exact_sources(
    workspace, app, monkeypatch, clock, change
):
    call, tracker, state, _ = setup(workspace, app, monkeypatch)
    service = app.state.service

    def mutate():
        if change == "expired":
            clock.now += timedelta(seconds=301)
        elif change == "source":
            service._receive_transcript(
                p(workspace),
                call["id"],
                {
                    "segment_id": "s",
                    "revision": 2,
                    "side": "remote",
                    "is_final": True,
                    "text": "A future conversation is not agreed.",
                },
                None,
                True,
            )
        else:
            with service.db.transaction() as s:
                row = get(s, "trackers", tracker["id"])
                if change == "deleted":
                    s.delete(row)
                else:
                    update(row, definition="The prospect explicitly rejects further calls")

    state["during_review"] = mutate
    await service.analyze_postcall(p(workspace), call["id"])
    assert not service.analysis(p(workspace), call["id"])["trackers"]


@pytest.mark.asyncio
async def test_correction_invalidates_published_tracker_without_erasing_human_note(
    workspace, app, monkeypatch
):
    call, _, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    result = service.analysis(p(workspace), call["id"])
    assert result["trackers"]
    service.edit_notes(p(workspace), call["id"], result["notes_version"], "The rep's own durable note")
    service._receive_transcript(
        p(workspace),
        call["id"],
        {
            "segment_id": "s",
            "revision": 2,
            "side": "remote",
            "is_final": True,
            "text": "There is no agreed callback.",
        },
        None,
        True,
    )
    result = service.analysis(p(workspace), call["id"])
    assert not result["trackers"]
    assert result["human_notes"] == "The rep's own durable note"


@pytest.mark.asyncio
async def test_no_tracker_request_after_raw_expiry(workspace, app, monkeypatch, clock):
    call, _, _, requests = setup(workspace, app, monkeypatch)
    clock.now += timedelta(seconds=301)
    await app.state.service.analyze_postcall(p(workspace), call["id"])
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["unknown", "duplicate", "wrong_evidence"])
async def test_malformed_tracker_result_cannot_publish(workspace, app, monkeypatch, bad):
    call, _, state, _ = setup(workspace, app, monkeypatch)
    if bad == "duplicate":
        state["body"]["tracker_matches"] *= 2
    elif bad == "unknown":
        state["body"]["tracker_matches"][0]["tracker_id"] = "unrelated-tracker"
    else:
        state["body"]["tracker_matches"][0]["evidence"] = ["unrelated-source"]
    await app.state.service.analyze_postcall(p(workspace), call["id"])
    assert not app.state.service.analysis(p(workspace), call["id"])["trackers"]
