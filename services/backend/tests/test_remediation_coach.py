"""A22-A28: proactive freeform assistance and provider privacy, authored first."""

import json

import httpx
import pytest
from app.coaching.context import ground_guidance
from app.providers.openrouter import OpenRouterCoach
from conftest import start
from test_openrouter_coach import completion


def advice(version=1, evidence="s1", text="Which part of that constraint would be useful to clarify first?"):
    return {
        "action": "SHOW_CARD",
        "kind": "DISCOVERY",
        "say_or_ask": text,
        "reason": "Clarify the prospect's stated concern.",
        "evidence_segment_ids": [evidence],
        "playbook_card_ids": [],
        "state_version": version,
        "expires_in_ms": 20000,
        "state_update": None,
    }


def ordinary_advice(request, evidence="s1", **changes):
    """Explicit audited nonassertive fixture; claim tests keep the untyped advice helper."""
    return {
        **advice(request["state_version"], evidence),
        "context_fingerprint": request["context_fingerprint"],
        "field_audit": [
            {"field": field, "classification": "nonassertive", "sources": [], "implied_premises": []}
            for field in ("say_or_ask", "reason")
        ],
        **changes,
    }


def test_novel_grounded_question_does_not_need_a_matching_playbook_sentence():
    request = {
        "state_version": 1,
        "context_fingerprint": "novel-question",
        "recent_dialogue": [
            {
                "segment_id": "s1",
                "side": "remote",
                "is_final": True,
                "text": "Our approvals depend on a union committee.",
            }
        ],
        "approved_cards": [],
        "facts": [],
        "approved_claims": [],
    }
    assert ground_guidance(ordinary_advice(request), request)


@pytest.mark.asyncio
async def test_cerebras_request_is_zdr_and_prompt_allows_contextual_wording():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion(content=advice()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OpenRouterCoach("fake-key", http=http).suggest({"state_version": 1})
    body = seen[0]
    assert body["provider"]["zdr"] is True
    assert body["provider"]["data_collection"] == "deny"
    assert body["provider"]["allow_fallbacks"] is False
    assert "verbatim" not in body["messages"][0]["content"].lower()
    assert result["say_or_ask"] == advice()["say_or_ask"]


@pytest.mark.asyncio
async def test_unknown_topic_reaches_provider_without_matching_card(workspace, app, clock, monkeypatch):
    call = start(workspace)
    service = app.state.service
    captured = []

    class Coach:
        model = "fixture"
        last_usage = {}
        last_request_id = None
        last_provider = "Cerebras"
        last_model = "fixture"

        async def suggest(self, request, **kwargs):
            captured.append(request)
            return ordinary_advice(request, "unknown")

    monkeypatch.setattr(service, "configured_coach", lambda p: (Coach(), "openrouter"))
    # Only the inference boundary is made live; no carrier or external network is used.
    monkeypatch.setattr(service.settings, "simulation", False)
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    await service.transcript(
        p,
        call["id"],
        {
            "segment_id": "unknown",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "Our approvals depend on a union committee.",
        },
    )
    assert not captured
    from datetime import timedelta

    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(captured) == 1
    assert captured[0]["lead_context"]
    assert captured[0]["recent_dialogue"][-1]["text"].endswith("committee.")


def test_every_stable_chunk_is_observed_without_a_paid_request_per_partial(
    workspace, app, clock, monkeypatch
):
    call = start(workspace)
    seen = []

    async def observed(p, call_id, **kwargs):
        seen.append(call_id)
        return {"action": "NO_ACTION"}

    monkeypatch.setattr(app.state.service, "coach_turn", observed)
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"] + "/transcript"
    for revision in range(1, 8):
        assert (
            c.post(
                path,
                headers=h,
                json={
                    "segment_id": "chunk",
                    "revision": revision,
                    "side": "remote",
                    "text": "A nuanced constraint.",
                    "is_final": revision == 7,
                },
            ).status_code
            == 200
        )
    assert not seen
    from datetime import timedelta

    clock.now += timedelta(milliseconds=301)
    assert c.post("/v1/admin/jobs/run", headers=h, json={"queues": ["live_coach"]}).status_code == 200
    assert seen == [call["id"]]


def test_coach_preferences_do_not_disable_manual_help(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"] + "/coach"
    result = c.patch(path, headers=h, json={"muted": True, "pinned": False})
    assert result.status_code == 200, result.text
    response = c.post(
        path, headers=h, json={"question": "What is missing from our knowledge?", "urgent": True}
    )
    assert response.status_code == 200
    assert response.json().get("status") in {"ready", "unconfigured", "no_action"}
    assert response.json().get("private") is True


def test_model_failure_does_not_block_controls_or_fabricate_help(workspace, app, monkeypatch):
    call = start(workspace)

    async def fail(*args, **kwargs):
        raise TimeoutError("model unavailable")

    monkeypatch.setattr(app.state.service.coach, "suggest", fail)
    c, h = workspace["client"], workspace["headers"]
    result = c.post("/v1/calls/" + call["id"] + "/coach", headers=h)
    assert result.status_code == 200
    assert c.post("/v1/calls/" + call["id"] + "/hangup", headers=h).status_code == 200


def test_same_call_jobs_are_coalesced_before_provider_work(workspace, app, monkeypatch):
    call = start(workspace)
    service = app.state.service
    c, h = workspace["client"], workspace["headers"]
    for i in range(6):
        service.jobs.enqueue(
            "live_coach",
            {
                "call_id": call["id"],
                "seller_id": workspace["seller_id"],
                "user_id": workspace["user_id"],
                "state_version": i,
            },
            f"coalesce-{i}",
            tenant_id=workspace["tenant_id"],
        )
    seen = []

    async def provider(p, call_id, **kwargs):
        seen.append(call_id)
        return {"action": "NO_ACTION"}

    monkeypatch.setattr(service, "coach_turn", provider)
    response = c.post("/v1/admin/jobs/run", headers=h, json={"queues": ["live_coach"]})
    assert response.status_code == 200
    assert seen == [call["id"]], seen
