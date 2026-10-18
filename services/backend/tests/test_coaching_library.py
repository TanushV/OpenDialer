"""A28: personal improvement, reviewed examples and private non-live practice."""

from datetime import timedelta
from uuid import uuid4

import pytest
from app.db import get, put, update
from conftest import start
from test_context_reads import count_decrypted
from test_live_memory import turn
from test_remediation_coach import advice
from test_remediation_privacy import persisted_content, principal

LESSON = "Clarify who owns the procurement decision before describing the next step."


def source(workspace, app):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    turn(service, p, call, "lesson")
    with service.db.transaction() as s:
        put(
            s,
            "conversation_states",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "version": 1,
                "privacy_contract": 1,
                "coaching_points": [
                    {
                        "id": "tip-1",
                        "text": LESSON,
                        "status": "confirmed",
                        "evidence_segment_ids": ["lesson"],
                        "evidence_revisions": {"lesson": 1},
                    }
                ],
            },
            key=call["id"],
            now=service.clock(),
        )
    return call, p


def publish(workspace, call, **changes):
    return workspace["client"].post(
        "/v1/admin/snippets",
        headers=workspace["headers"],
        json={
            "call_id": call["id"],
            "coaching_point_id": "tip-1",
            "expected_transcript_version": 1,
            "review_reference": "Reviewed as a useful process clarification example",
            **changes,
        },
    )


def test_personal_goals_are_versioned_and_do_not_become_business_outcomes(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    first = c.get("/v1/coaching", headers=h)
    assert first.status_code == 200, first.text
    body = {
        "expected_version": first.json()["goals_version"],
        "goals": [
            {"id": str(uuid4()), "text": "Ask one useful clarification before pitching", "status": "active"}
        ],
    }
    saved = c.put("/v1/coaching/goals", headers=h, json=body)
    assert saved.status_code == 200, saved.text
    assert c.get("/v1/coaching", headers=h).json()["goals"] == body["goals"]
    assert c.put("/v1/coaching/goals", headers=h, json=body).status_code == 409
    p = principal(workspace)
    with app.state.db.transaction(False) as s:
        assert not app.state.service.scoped(s, "outcomes", p)
        assert not app.state.service.scoped(s, "conversion_events", p)


def test_reviewed_example_copies_derived_tip_once_and_keeps_no_raw_quote(workspace, app, clock):
    call, p = source(workspace, app)
    response = publish(workspace, call)
    assert response.status_code == 200, response.text
    example = response.json()
    assert example["text"] == LESSON and example["reviewed_by"] == p["user_id"]
    assert publish(workspace, call).json()["id"] == example["id"]
    assert "quote" not in example
    clock.now += timedelta(seconds=301)
    # Expired raw evidence must not erase an already reviewed paraphrase.
    app.state.service.purge_legacy_raw_content()
    view = workspace["client"].get("/v1/coaching", headers=workspace["headers"]).json()
    assert view["examples"][0]["text"] == LESSON
    assert view["examples"][0]["raw_evidence_status"] == "expired"


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"coaching_point_id": "invented"}, 422),
        ({"expected_transcript_version": 0}, 409),
        ({"review_reference": ""}, 422),
        ({"text": "unreviewed extra instructions"}, 422),
    ],
)
def test_only_the_exact_reviewed_current_tip_can_be_published(workspace, app, changes, status):
    call, _ = source(workspace, app)
    assert publish(workspace, call, **changes).status_code == status


def test_corrected_source_and_withdrawn_example_cannot_reenter_context(workspace, app):
    call, p = source(workspace, app)
    example = publish(workspace, call)
    assert example.status_code == 200, example.text
    turn(app.state.service, p, call, "lesson", revision=2, text="The prior description was incorrect.")
    view = workspace["client"].get("/v1/coaching", headers=workspace["headers"])
    assert view.status_code == 200
    assert view.json()["examples"] == []
    removed = workspace["client"].patch(
        f"/v1/admin/snippets/{example.json()['id']}",
        headers=workspace["headers"],
        json={"expected_version": example.json()["version"], "approved": False},
    )
    assert removed.status_code == 200, removed.text


def test_personal_coaching_reads_only_bounded_own_history(workspace, app, clock, monkeypatch):
    call, p = source(workspace, app)
    with app.state.db.transaction() as s:
        for i in range(250):
            old = put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": call["lead_id"],
                    "rep_id": "other-rep",
                    "state": "FINALIZED",
                    "started_at": clock.now.isoformat(),
                },
                now=clock.now,
            )
            put(
                s,
                "conversation_states",
                p["tenant_id"],
                p["seller_id"],
                {"call_id": old.id, "coaching_points": [{"text": "Another rep's private coaching"}]},
                key=old.id,
            )
    decoded = count_decrypted(app, monkeypatch)
    result = workspace["client"].get("/v1/coaching", headers=workspace["headers"])
    assert result.status_code == 200, result.text
    assert LESSON in result.text and "Another rep's private coaching" not in result.text
    assert len(decoded) <= 70, len(decoded)


@pytest.mark.asyncio
async def test_approved_examples_reach_practice_and_live_context_without_gating_generation(
    workspace, app, monkeypatch
):
    call, p = source(workspace, app)
    assert publish(workspace, call).status_code == 200
    requests = []

    class Provider:
        last_provider, last_model = "Cerebras", "fixture"
        last_usage, last_request_id = {}, None

        async def suggest(self, request, **kwargs):
            requests.append(request)
            self.last_request_id = f"example-{len(requests)}"
            self.last_usage = {"cost": 0.001}
            result = advice(request["state_version"], "lesson")
            result["evidence_segment_ids"] = [] if request.get("simulation") else ["lesson"]
            return result

    service = app.state.service
    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    practiced = await service.practice(p, "How can I clarify the decision process?")
    assert practiced["status"] == "ready"
    await service.coach_turn(p, call["id"], urgent=True, question="What can I clarify?")
    assert len(requests) == 2
    assert all(r["reviewed_examples"][0]["text"] == LESSON for r in requests)
    assert "How can I clarify the decision process?" not in persisted_content(service, p)
    with service.db.transaction(False) as s:
        assert len(service.scoped(s, "call_attempts", p)) == 1
        assert not service.scoped(s, "outcomes", p)


@pytest.mark.asyncio
async def test_revoked_practice_requester_receives_no_private_result(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)

    class Provider:
        last_usage, last_request_id = {"cost": 0.001}, "practice-revoked"

        async def suggest(self, request, **kwargs):
            with service.db.transaction() as s:
                update(get(s, "users", p["user_id"]), disabled=True)
            result = advice(0)
            result["evidence_segment_ids"] = []
            return result

    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    result = await service.practice(p, "Private practice question")
    assert result["status"] != "ready" and "guidance" not in result
    with service.db.transaction(False) as s:
        usage = service.scoped(s, "usage_ledger", p)
        assert len(usage) == 1 and usage[0].data["cost_usd"] == 0.001
