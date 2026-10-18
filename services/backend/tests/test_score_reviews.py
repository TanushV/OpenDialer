"""A39: human ratings remain separate, versioned and recoverable after AI revisions."""

from copy import deepcopy
from datetime import timedelta

import pytest
from app.db import get, update
from conftest import start
from test_call_rubrics import choose
from test_remediation_rep_workflow import p


def setup(workspace, app):
    call = start(workspace)
    service = app.state.service
    service._receive_transcript(
        p(workspace),
        call["id"],
        {
            "segment_id": "review-source",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "Our review process has several teams involved and we would like to understand a simpler approach.",
        },
        None,
        True,
    )
    return service.analysis(p(workspace), call["id"])


def body(analysis, **changes):
    return {
        "request_id": "human-review-1",
        "expected_version": analysis.get("scorecard_version", 1),
        "reason": "The discussion did offer a concrete opportunity to explore the process.",
        "dimension": "problem_discovery",
        "status": "SCORED",
        "score": 2,
        "evidence": ["review-source"],
        **changes,
    }


def submit(workspace, analysis, value):
    return workspace["client"].post(
        f"/v1/scorecards/{analysis['scorecard_id']}/review", headers=workspace["headers"], json=value
    )


def test_human_score_is_separate_from_ai_and_survives_reanalysis_and_raw_expiry(workspace, app, clock):
    a = setup(workspace, app)
    result = submit(workspace, a, body(a))
    assert result.status_code == 200, result.text
    service = app.state.service
    with service.db.transaction() as s:
        card = get(s, "scorecards", a["scorecard_id"])
        items = deepcopy(card.data["items"])
        items[2].update(score=0, status="SCORED", evidence=["review-source"], explanation="New AI assessment")
        update(card, items=items)
    clock.now += timedelta(seconds=301)
    after = service.analysis(p(workspace), a["call"]["id"])
    assert after["scorecard"]["items"][2]["score"] == 0, "Never overwrite the AI record with a human score"
    reviewed = after["reviewed_scorecard"]
    assert reviewed["items"][2]["score"] == 2 and reviewed["items"][2]["source"] == "human"
    assert reviewed["overall"]["score"] == 100 and reviewed["overall"]["coverage"] == pytest.approx(1 / 7)
    assert after["reviews"][0]["raw_evidence_status"] == "expired"
    assert after["reviews"][0]["applies"] is True


@pytest.mark.parametrize("change", ["transcript", "purpose"])
def test_source_changes_preserve_but_suspend_prior_human_rating(workspace, app, change):
    a = setup(workspace, app)
    assert submit(workspace, a, body(a)).status_code == 200
    service = app.state.service
    if change == "purpose":
        assert choose(workspace, a["call"], "follow_up").status_code == 200
    else:
        service._receive_transcript(
            p(workspace),
            a["call"]["id"],
            {
                "segment_id": "review-source",
                "revision": 2,
                "side": "remote",
                "is_final": True,
                "text": "The earlier transcription was inaccurate; no process was discussed.",
            },
            None,
            True,
        )
    after = service.analysis(p(workspace), a["call"]["id"])
    assert len(after["reviews"]) == 1 and after["reviews"][0]["reason"] == body(a)["reason"]
    assert after["reviews"][0]["applies"] is False
    assert after["reviewed_scorecard"]["overall"]["score"] is None


def test_review_retry_is_idempotent_and_changed_payload_conflicts(workspace, app):
    a = setup(workspace, app)
    value = body(a)
    first = submit(workspace, a, value)
    assert first.status_code == 200, first.text
    second = submit(workspace, a, value)
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    assert submit(workspace, a, {**value, "score": 1}).status_code == 409
    assert len(app.state.service.analysis(p(workspace), a["call"]["id"])["reviews"]) == 1


def test_concurrent_reviews_cannot_silently_replace_a_rating(workspace, app):
    a = setup(workspace, app)
    assert submit(workspace, a, body(a)).status_code == 200
    assert submit(workspace, a, body(a, request_id="other-review", score=1)).status_code == 409


@pytest.mark.parametrize(
    "case",
    [
        "state",
        "range",
        "unscored_number",
        "missing_source",
        "wrong_source",
        "dimension",
        "blank",
        "missing_version",
        "missing_request",
    ],
)
def test_invalid_rating_cannot_be_recorded(workspace, app, case):
    a = setup(workspace, app)
    value = body(a)
    if case == "state":
        value["status"] = "guaranteed"
    elif case == "range":
        value["score"] = 3
    elif case == "unscored_number":
        value["status"] = "N/A"
    elif case == "missing_source":
        value["evidence"] = []
    elif case == "wrong_source":
        value["evidence"] = ["other-call"]
    elif case == "dimension":
        value["dimension"] = "personality"
    elif case == "blank":
        value["reason"] = "   "
    elif case == "missing_version":
        value.pop("expected_version")
    else:
        value.pop("request_id")
    assert submit(workspace, a, value).status_code == 422
    assert not app.state.service.analysis(p(workspace), a["call"]["id"])["reviews"]


def test_reviewer_can_mark_no_opportunity_and_resolve_a_specific_appeal(workspace, app):
    a = setup(workspace, app)
    appeal = submit(workspace, a, body(a, status="disputed", score=None))
    assert appeal.status_code == 200
    current = app.state.service.analysis(p(workspace), a["call"]["id"])
    resolved = submit(
        workspace,
        current,
        body(
            current, request_id="resolution", status="N/A", score=None, resolves_review_id=appeal.json()["id"]
        ),
    )
    assert resolved.status_code == 200, resolved.text
    after = app.state.service.analysis(p(workspace), a["call"]["id"])
    assert after["reviewed_scorecard"]["items"][2]["status"] == "N/A"
    assert after["reviewed_scorecard"]["overall"]["score"] is None
    original = next(r for r in after["reviews"] if r["id"] == appeal.json()["id"])
    assert original["resolution_id"] == resolved.json()["id"]
    assert original["status"] == "disputed", "Keep the original appeal as history"
    assert (
        submit(
            workspace,
            after,
            body(
                after,
                request_id="resolution-2",
                status="resolved",
                score=None,
                resolves_review_id=appeal.json()["id"],
            ),
        ).status_code
        == 409
    )


def test_cannot_resolve_a_nonexistent_or_unrelated_appeal(workspace, app):
    a = setup(workspace, app)
    assert (
        submit(workspace, a, body(a, status="resolved", score=None, resolves_review_id="missing")).status_code
        == 422
    )


def test_stale_scorecard_cannot_accept_new_rating_before_read_refresh(workspace, app):
    a = setup(workspace, app)
    assert choose(workspace, a["call"], "unknown").status_code == 200
    assert submit(workspace, a, body(a)).status_code == 409


def test_foreign_rep_cannot_review_the_call(workspace, app):
    a = setup(workspace, app)
    c, h = workspace["client"], workspace["headers"]
    created = c.post(
        "/v1/admin/users",
        headers=h,
        json={"email": "other-rep@example.test", "password": "other-rep-test-password", "role": "rep"},
    )
    assert created.status_code == 200, created.text
    login = c.post(
        "/v1/auth/session", json={"email": "other-rep@example.test", "password": "other-rep-test-password"}
    )
    assert login.status_code == 200, login.text
    scoped = {**workspace, "headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}
    assert submit(scoped, a, body(a)).status_code == 404
