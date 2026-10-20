import httpx
import pytest
from app.coaching.engine import CoachEngine
from app.providers.telnyx import TelnyxCarrier
from conftest import start
from test_postcall_completion import finalize


@pytest.mark.asyncio
async def test_coach_circuit_breaker_falls_back_without_backlog(monkeypatch):
    import app.coaching.engine as engine_module

    # A frozen deadline clock keeps admission on time under machine load; the breaker is under test.
    monkeypatch.setattr(engine_module, "monotonic", lambda: 0.0)

    class Failing:
        count = 0

        async def suggest(self, request, deadline_ms):
            self.count += 1
            raise TimeoutError()

    provider = Failing()
    engine = CoachEngine()
    fallback = {"action": "NO_ACTION"}
    results = [await engine.suggest("call", {}, provider, fallback, deadline_ms=10) for _ in range(5)]
    # Failures stay quiet with an explicit status; only the open circuit uses the fallback.
    assert [r.get("status") for r in results[:3]] == ["unavailable"] * 3
    assert all(r["action"] == "NO_ACTION" for r in results) and results[3:] == [fallback] * 2
    assert provider.count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,allowed", [(None, True), ("outbound-pstn-profile", False)])
async def test_scoped_credential_must_not_bypass_backend_pstn_gate(profile, allowed):
    def handler(request):
        # Documented shapes: the credential names its connection only as resource_id.
        if "/telephony_credentials/" in request.url.path:
            return httpx.Response(200, json={"data": {"resource_id": "connection:incoming-only"}})
        assert request.url.path == "/v2/credential_connections/incoming-only"
        return httpx.Response(
            200,
            json={
                "data": {
                    "sip_uri_calling_preference": "internal",
                    "outbound": {"outbound_voice_profile_id": profile},
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelnyxCarrier("secret", "callcontrol", http=client)
        if allowed:
            assert await provider.verify_incoming_only_credential("cred")
        else:
            with pytest.raises(ValueError):
                await provider.verify_incoming_only_credential("cred")


def test_optional_inference_budget_is_reserved_atomically(workspace):
    service = workspace["client"].app.state.service
    p = {k: workspace[k] for k in ["tenant_id", "seller_id", "user_id"]}
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/budget", headers=h, json={"daily_limit_usd": 0.05, "monthly_limit_usd": 0.05})
    assert service.reserve_optional_budget(p, "test-one", 0.04)
    assert not service.reserve_optional_budget(p, "test-two", 0.04)
    assert service.reserve_optional_budget(p, "test-one", 0.04)


@pytest.mark.asyncio
async def test_provider_postcall_validates_evidence_and_preserves_human_notes(workspace, monkeypatch):
    from test_postcall_completion import ConversationProvider
    from test_remediation_privacy import principal

    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "evidence-1",
            "revision": 1,
            "side": "remote",
            "text": "We need help with repair. Please call back next week.",
            "is_final": True,
        },
    )

    state = {
        "requests": 0,
        "body": {
            "summary": "The prospect requested repair help and a callback.",
            "summary_evidence": ["evidence-1"],
            "problems": [
                {
                    "text": "Need repair help",
                    "evidence": ["evidence-1"],
                    "quote": "We need help with repair.",
                }
            ],
            "objections": [],
            "commitments": [
                {
                    "owner": "rep",
                    "action": "Call back",
                    "due_at": None,
                    "verbatim_time_expression": "next week",
                    "evidence": ["evidence-1"],
                    "needs_time_confirmation": True,
                }
            ],
            "unknowns": ["Exact callback time"],
            "score_items": [],
        },
    }

    service = c.app.state.service
    monkeypatch.setattr(
        service, "configured_coach", lambda *args: (ConversationProvider(state), "openrouter")
    )
    before = service.analysis(principal(workspace), call["id"])
    service.edit_notes(
        principal(workspace), call["id"], before["notes_version"], "Rep requested a precise callback time."
    )
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    await finalize(service, principal(workspace), call["id"])
    analysis = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert state["requests"] == 2
    assert analysis["finalization"]["status"] == "ready"
    assert analysis["human_notes"] == "Rep requested a precise callback time."
    assert analysis["notes"]["problems"][0]["evidence_segment_ids"] == ["evidence-1"]
    assert analysis["notes"]["problems"][0]["evidence_revisions"] == {"evidence-1": 1}
    packet = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()
    assert len(packet["commitments"]) == len(packet["tasks"]) == 1
    assert packet["tasks"][0]["status"] == "needs_confirmation"
    assert packet["tasks"][0]["due_at"] is None


def test_tracker_matches_have_evidence_and_are_versioned(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    tracker = c.post(
        "/v1/admin/trackers",
        headers=h,
        json={
            "name": "Existing provider",
            "kind": "keyword",
            "phrases": ["another provider"],
            "definition": "Prospect mentions an existing supplier",
            "positive_examples": ["We use another provider"],
            "negative_examples": ["We have no provider"],
        },
    )
    assert tracker.status_code == 200, tracker.text
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "track-1",
            "revision": 1,
            "side": "remote",
            "text": "We already use another provider.",
            "is_final": True,
        },
    )
    analysis = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert analysis["trackers"][0]["evidence"] == ["track-1"]


def test_cdr_reconciliation_is_idempotent_and_keeps_leg_units(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    body = {
        "source_reference": "synthetic-carrier-cdr-fixture",
        "rows": [
            {
                "provider_record_id": "cdr-one",
                "call_id": call["id"],
                "leg_id": "prospect-leg",
                "duration_seconds": 61,
                "cost_usd": 0.02,
                "kind": "voice",
            }
        ],
    }
    a = c.post("/v1/admin/billing/reconcile", headers=h, json=body)
    b = c.post("/v1/admin/billing/reconcile", headers=h, json=body)
    assert a.status_code == b.status_code == 200, a.text
    assert a.json()["matched"] == 1 and b.json()["duplicates"] == 1
    assert a.json()["duration_seconds"] == 61


def test_live_coach_respects_the_optional_budget_before_provider_effect(workspace, monkeypatch):
    from conftest import start

    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "budget-turn",
            "revision": 1,
            "side": "remote",
            "text": "We use another provider.",
            "is_final": True,
        },
    )
    service = c.app.state.service
    service.settings.simulation = False
    c.put("/v1/admin/budget", headers=h, json={"daily_limit_usd": 0, "monthly_limit_usd": 0})
    calls = []

    class Provider:
        async def suggest(self, request, **kwargs):
            calls.append(request)
            return {"action": "NO_ACTION"}

    monkeypatch.setattr(service, "configured_coach", lambda *args: (Provider(), "openrouter"))
    r = c.post(f"/v1/calls/{call['id']}/coach", headers=h)
    assert r.status_code == 200
    assert not calls


@pytest.mark.asyncio
async def test_repeated_failed_provider_attempts_each_need_a_fresh_budget_hold(workspace, monkeypatch):
    from conftest import start

    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "retry-budget",
            "revision": 1,
            "side": "remote",
            "text": "We need help.",
            "is_final": True,
        },
    )
    service = c.app.state.service
    # The ended call's hold settles to its estimate, so the base is measured after hangup.
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    with service.db.transaction(False) as s:
        base = sum(
            r.data["cost_usd"] if r.data.get("cost_usd") is not None else r.data.get("reserved_usd", 0)
            for r in service.scoped(s, "usage_ledger", workspace)
        )
    assert (
        c.put(
            "/v1/admin/budget",
            headers=h,
            json={"daily_limit_usd": base + 0.021, "monthly_limit_usd": base + 0.021},
        ).status_code
        == 200
    )
    calls = []

    class Failing:
        last_usage, last_request_id = {}, None
        last_provider, last_model = "Cerebras", "fixture"

        async def suggest(self, *args, **kwargs):
            calls.append(True)
            raise TimeoutError("No provider receipt")

    monkeypatch.setattr(service, "configured_coach", lambda *args: (Failing(), "openrouter"))
    for _ in range(3):
        await service.analyze_postcall(workspace, call["id"])
    assert len(calls) == 2
    with service.db.transaction(False) as s:
        attempts = [
            r
            for r in service.scoped(s, "usage_ledger", workspace)
            if r.data.get("work_kind") == "conversation_finalize"
        ]
        assert len(attempts) == 2 and all(r.data["object_id"] == call["id"] for r in attempts)
        assert all(r.data["status"] == "uncertain" and r.data["reserved_usd"] == 0.01 for r in attempts)
        assert len({r.data["request_key"] for r in attempts}) == 2
