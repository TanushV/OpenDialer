"""Semantic trackers share the reviewed analysis and cannot outlive corrected evidence."""

from datetime import timedelta

import pytest
from app.db import get, update
from conftest import NOW
from test_postcall_completion import setup_analysis
from test_remediation_rep_workflow import p


@pytest.mark.parametrize("bad", ["foreign", "forward", "cycle", "too_many_sources", "unknown_speaker"])
def test_reviewed_state_sources(bad):
    """C008: malformed lineage is rejected; unknown speech cannot confirm a promise."""
    from app.coaching.context import merge_semantic_state
    from app.coaching.engine import CardRejected

    segment = {
        "segment_id": "s",
        "revision": 1,
        "is_final": True,
        "side": "remote",
        "text": "An approval discussion is useful.",
    }
    item = {
        "text": "An internal signoff question remains.",
        "quote": segment["text"],
        "evidence_segment_ids": ["s"],
        "status": "confirmed",
    }
    prior = {
        "stage": "discovery",
        "needs": [
            {
                "id": "prior",
                "text": "Older issue",
                "evidence_segment_ids": ["s"],
                "evidence_revisions": {"s": 1},
            }
        ],
    }
    if bad == "foreign":
        item["evidence_segment_ids"] = ["other-call-source"]
    elif bad == "forward":
        item["replaces"] = ["future-observation"]
    elif bad == "cycle":
        item.update(
            id="prior", replaces=["prior"], sources=[{"kind": "observation", "id": "prior", "version": 1}]
        )
    elif bad == "too_many_sources":
        item["evidence_segment_ids"] = ["s"] * 33
    else:
        segment["side"] = "unknown"
    if bad == "unknown_speaker":
        result = merge_semantic_state(prior, {"stage": "discovery", "needs": [item]}, [segment], at=NOW)
        assert all(row.get("epistemic_status") != "human_confirmed" for row in result.get("needs", []))
        assert all(
            row.get("status") != "confirmed"
            for row in result.get("needs", [])
            if row.get("text") == item["text"]
        )
    else:
        with pytest.raises(CardRejected):
            merge_semantic_state(prior, {"stage": "discovery", "needs": [item]}, [segment], at=NOW)


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
    requests = state["contexts"] = []
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
    assert any(item["field"] == "tracker_matches/" + tracker["id"] for item in requests[1]["fields"])
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
