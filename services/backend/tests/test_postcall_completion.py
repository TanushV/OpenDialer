"""Date and inferred-task contracts authored before implementation."""

from datetime import UTC, datetime

import pytest
from app.db import doc
from app.providers.inference import InferenceResult
from conftest import start
from test_remediation_rep_workflow import p


@pytest.mark.parametrize(
    "expression,zone,expected",
    [
        ("Call me tomorrow at 2 PM.", "America/Los_Angeles", "2026-10-15T21:00:00+00:00"),
        ("Call me Tuesday at 14:30 Pacific time.", None, "2026-10-20T21:30:00+00:00"),
        ("Call on 2026-10-20 at 2 PM Eastern time.", None, "2026-10-20T18:00:00+00:00"),
        ("Call on October 20, 2026 at 2 PM Pacific time.", None, "2026-10-20T21:00:00+00:00"),
        ("Call tomorrow at 2 PM.", None, None),
        ("Maybe next Tuesday at 2 PM.", "America/Los_Angeles", None),
        ("Call on November 1, 2026 at 1:30 AM Pacific time.", None, None),
        ("Call on March 8, 2026 at 2:30 AM Pacific time.", None, None),
    ],
)
def test_date_resolution_uses_call_zone_and_rejects_dst_ambiguity(expression, zone, expected):
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {"verbatim_time_expression": expression, "due_at": expected, "needs_time_confirmation": False},
        call_at=datetime(2026, 10, 14, 18, tzinfo=UTC),
        timezone=zone,
    )
    assert result["due_at"] == expected
    assert result["needs_time_confirmation"] is (expected is None)


def test_vague_window_is_preserved_without_an_invented_exact_time():
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {
            "verbatim_time_expression": "Call next Tuesday afternoon.",
            "due_at": None,
            "needs_time_confirmation": True,
        },
        call_at=datetime(2026, 10, 14, 18, tzinfo=UTC),
        timezone="America/Los_Angeles",
    )
    assert result["due_at"] is None
    assert result["time_window"] == {
        "date": "2026-10-20",
        "period": "afternoon",
        "timezone": "America/Los_Angeles",
    }


def test_model_timestamp_must_match_the_evidenced_date_and_zone():
    from app.postcall.dates import resolve_time

    with pytest.raises(ValueError):
        resolve_time(
            {
                "verbatim_time_expression": "October 20, 2026 at 2 PM Pacific time",
                "due_at": "2026-10-20T14:00:00Z",
                "needs_time_confirmation": False,
            }
        )


def test_partial_and_non_scored_rubrics_cannot_publish_an_inflated_score():
    from app.postcall.inference import validate_analysis

    source = {"segment_id": "s", "is_final": True, "text": "Hello from our team."}
    body = {
        "summary": "The seller introduced the call.",
        "summary_evidence": ["s"],
        "problems": [],
        "objections": [],
        "commitments": [],
        "unknowns": [],
        "score_items": [
            {
                "dimension": "identity_relevance",
                "score": 2,
                "status": "SCORED",
                "evidence": ["s"],
                "explanation": "Introduced",
            }
        ],
    }
    with pytest.raises(ValueError, match="complete rubric"):
        validate_analysis(body, [source])


def analysis_body(action="Call back"):
    return {
        "summary": "A future conversation was requested.",
        "summary_evidence": ["s"],
        "problems": [],
        "objections": [],
        "unknowns": [],
        "score_items": [],
        "commitments": [
            {
                "kind": "callback",
                "owner": "rep",
                "action": action,
                "due_at": "2026-10-20T21:00:00+00:00",
                "verbatim_time_expression": "October 20, 2026 at 2 PM Pacific time",
                "evidence": ["s"],
                "needs_time_confirmation": False,
            }
        ],
    }


def setup_analysis(workspace, app, monkeypatch):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=h,
        json={
            "segment_id": "s",
            "revision": 1,
            "is_final": True,
            "side": "remote",
            "text": "Please call me on October 20, 2026 at 2 PM Pacific time.",
        },
    )
    c.post("/v1/calls/" + call["id"] + "/hangup", headers=h)
    state = {"body": analysis_body()}

    class Provider:
        async def complete(self, *args):
            return InferenceResult(
                state["body"], {"input_tokens": 30, "output_tokens": 30}, "fixture-interpretation"
            )

    monkeypatch.setattr(app.state.service, "inference", lambda *args: Provider())
    return call, state


@pytest.mark.asyncio
async def test_rephrased_commitment_updates_one_task_and_preserves_rep_edit(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        tasks = [doc(t) for t in service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "open"
    assert tasks[0]["due_at"] == "2026-10-20T21:00:00+00:00"
    service.edit_task(
        p(workspace),
        tasks[0]["id"],
        {
            "expected_version": tasks[0]["version"],
            "due_at": "2026-10-21T21:00:00+00:00",
            "timezone": "America/Los_Angeles",
        },
    )
    state["body"] = analysis_body("Reconnect with the prospect")
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        tasks2 = [doc(t) for t in service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])]
        commitments = [doc(t) for t in service.lead_records(s, p(workspace), "commitments", call["lead_id"])]
    assert len(tasks2) == len(commitments) == 1
    assert tasks2[0]["id"] == tasks[0]["id"]
    assert tasks2[0]["due_at"] == "2026-10-21T21:00:00+00:00"
    assert "verbatim_time_expression" not in commitments[0]


@pytest.mark.asyncio
async def test_corrected_source_holds_inferred_task_without_erasing_it(workspace, app, monkeypatch):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    c, h = workspace["client"], workspace["headers"]
    before = c.get("/v1/leads/" + call["lead_id"] + "/packet", headers=h).json()["tasks"]
    assert len(before) == 1
    response = c.post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=h,
        json={
            "segment_id": "s",
            "revision": 2,
            "is_final": True,
            "side": "remote",
            "text": "I cannot agree to a callback yet.",
        },
    )
    assert response.status_code == 200
    packet = c.get("/v1/leads/" + call["lead_id"] + "/packet", headers=h).json()
    assert packet["tasks"][0]["id"] == before[0]["id"]
    assert packet["tasks"][0]["status"] == "needs_confirmation"
    assert packet["tasks"][0]["source_stale"] is True
    assert packet["lead"].get("callback_due_at") is None
    assert packet["lead"]["outreach_hold"] == "callback_time_confirmation"


@pytest.mark.parametrize(
    "expression",
    [
        "October 20, 2026 around 2 PM Pacific time",
        "October 20, 2026 at 2 PM or 3 PM Pacific time",
        "October 20 or 21, 2026 at 2 PM Pacific time",
        "October 20, 2026 at 2 PM CST",
        "October 20, 2026 after 2 PM Pacific time",
    ],
)
def test_non_exact_time_cannot_become_an_automatic_callback(expression):
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {"verbatim_time_expression": expression, "due_at": None, "needs_time_confirmation": False}
    )
    assert result["due_at"] is None
    assert result["needs_time_confirmation"] is True


@pytest.mark.parametrize("status", ["completed", "cancelled"])
@pytest.mark.asyncio
async def test_inference_never_reopens_finished_tasks(workspace, app, monkeypatch, status):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = doc(service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0])
    service.edit_task(p(workspace), task["id"], {"expected_version": task["version"], "status": status})
    state["body"] = analysis_body("Revisit this later")
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        after = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
        assert len(after) == 1
        assert after[0].data["status"] == status


@pytest.mark.asyncio
async def test_source_correction_preserves_human_task_but_flags_conflict(workspace, app, monkeypatch):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = doc(service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0])
    service.edit_task(
        p(workspace),
        task["id"],
        {
            "expected_version": task["version"],
            "due_at": "2026-10-21T21:00:00+00:00",
            "timezone": "America/Los_Angeles",
        },
    )
    workspace["client"].post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=workspace["headers"],
        json={
            "segment_id": "s",
            "revision": 2,
            "is_final": True,
            "side": "remote",
            "text": "There is no agreed callback.",
        },
    )
    packet = (
        workspace["client"]
        .get("/v1/leads/" + call["lead_id"] + "/packet", headers=workspace["headers"])
        .json()
    )
    assert packet["tasks"][0]["due_at"] == "2026-10-21T21:00:00+00:00"
    assert packet["tasks"][0]["status"] == "open"
    assert packet["tasks"][0]["source_stale"] is True
    assert packet["tasks"][0]["review_required"] is True


@pytest.mark.asyncio
async def test_new_interpretation_retracts_only_still_available_sources(workspace, app, monkeypatch, clock):
    from datetime import timedelta

    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    state["body"]["commitments"] = []
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0]
        assert task.data["status"] == "cancelled"
        assert task.data["source_retracted"] is True
    # Retrying after raw expiry must not replace a valid rundown with the baseline.
    before = service.analysis(p(workspace), call["id"])["notes"]["summary"]
    clock.now += timedelta(seconds=301)
    await service.analyze_postcall(p(workspace), call["id"])
    after = service.analysis(p(workspace), call["id"])
    assert after["notes"]["summary"] == before
    assert after["transcript"] == []


@pytest.mark.asyncio
async def test_postcall_disposition_race_does_not_publish_old_interpretation(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service

    class RacingProvider:
        async def complete(self, *args):
            service.save_disposition(p(workspace), call["id"], {"outcome": "wrong_party"})
            return InferenceResult(state["body"], {}, "racing-result")

    monkeypatch.setattr(service, "inference", lambda *args: RacingProvider())
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction(False) as s:
        assert not service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
