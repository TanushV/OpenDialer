"""Remaining cross-cutting acceptance cases; no production edits precede this suite."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from app.db import doc, update
from app.research.validation import ValidationFailure, validate_script
from conftest import start


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "transcription_start",
        "transcription_stop",
        "record_stop",
        "playback_start",
        "playback_stop",
        "gather",
        "transfer",
        "answer",
        "bridge",
    ],
)
async def test_every_interrupted_control_command_leaves_sending(workspace, app, action):
    call = start(workspace)
    service = app.state.service
    p = principal(workspace)
    with service.db.transaction() as s:
        command = service.control_intent(s, p, call["id"], action, {}, suffix="crash-" + action)
        update(command, status="sending", sent_at=(service.clock() - timedelta(minutes=10)).isoformat())
        identifier = command.id
    await service.recover(p)
    with service.db.transaction(False) as s:
        result = service.required(s, "command_outbox", identifier, p)
        assert result.data["status"] in {
            "pending",
            "uncertain",
            "review_required",
            "cancelled",
            "acknowledged",
            "completed",
            "failed",
        }, doc(result)


def test_slow_write_does_not_hold_a_process_wide_read_mutex(workspace, app):
    service = app.state.service
    p = principal(workspace)

    def read():
        with service.db.transaction(False) as s:
            return service.required(s, "sellers", p["seller_id"], p).id

    # WAL readers should see the committed state while an unrelated write is open.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.db.transaction():
            result = pool.submit(read)
            assert result.result(timeout=1) == p["seller_id"]


@pytest.mark.parametrize("field", ["opener", "screening_intro", "gatekeeper"])
def test_all_spoken_template_fields_require_supported_personalization(clock, field):
    data = {
        "lead_id": "lead",
        "opener": "Hello from Example Seller.",
        "screening_intro": "Example Seller, business introduction.",
        "gatekeeper": "Who handles operations?",
        "discovery_questions": [],
        "fact_ids_used": [],
        "claims": [],
    }
    data[field] = "Your company just opened a new office in Seattle."
    with pytest.raises(ValidationFailure):
        validate_script(data, [], "example.test", "Example Seller", clock.now)


def test_no_new_generic_coaching_cards_are_published_as_seller_approved_knowledge(client):
    response = client.post(
        "/v1/setup",
        headers={"X-Local-Token": "test-local-secret"},
        json={
            "seller_name": "Unconfigured Seller",
            "campaign_name": "Empty knowledge",
            "email": "empty@example.test",
            "password": "test-only-strong-password",
        },
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    books = client.get("/v1/workspace", headers=headers).json()["playbooks"]
    assert not any(b.get("approved") and b.get("cards") for b in books)


def test_practice_uses_knowledge_but_does_not_create_calls_or_outcomes(workspace):
    c, h = workspace["client"], workspace["headers"]
    before = c.get("/v1/workspace", headers=h).json()
    attempts = c.get("/v1/metrics", headers=h).json()["attempts"]
    response = c.post(
        "/v1/coaching/practice",
        headers=h,
        json={"scenario": "A prospect has an unfamiliar procurement objection."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["simulation"] is True
    assert response.json()["status"] in {"ready", "unconfigured"}
    after = c.get("/v1/workspace", headers=h).json()
    assert before["calls"] == after["calls"]
    assert c.get("/v1/metrics", headers=h).json()["attempts"] == attempts


def test_configuration_rejects_silent_paid_or_non_zdr_fallback(workspace):
    c, h = workspace["client"], workspace["headers"]
    for body in [
        {"coach_provider": "groq"},
        {"recording_enabled": True},
        {"inference_data_collection": "allow"},
    ]:
        response = c.put("/v1/admin/operating-policy", headers=h, json=body)
        assert response.status_code == 422, response.text


def test_default_maintenance_runs_without_training_queue(workspace, app, monkeypatch):
    service = app.state.service
    seen = []

    async def recovery(p):
        seen.append("recovery")
        return {}

    monkeypatch.setattr(service, "recover", recovery)
    asyncio.run(service.maintenance_tick())
    assert "recovery" in seen


def test_transcript_correction_marks_derived_feature_observation_stale(workspace, app):
    call = start(workspace)
    service = app.state.service
    p = principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"] + "/transcript"
    initial = {
        "segment_id": "correction",
        "revision": 1,
        "side": "remote",
        "is_final": True,
        "text": "We need a new supplier.",
    }
    assert c.post(path, headers=h, json=initial).status_code == 200
    with service.db.transaction() as s:
        state = next(r for r in service.scoped(s, "conversation_states", p) if r.external_key == call["id"])
        update(
            state,
            observations=[{"concept": "Changing supplier", "source_segment_id": "correction", "revision": 1}],
        )
    assert (
        c.post(
            path, headers=h, json={**initial, "revision": 2, "text": "We do not need a new supplier."}
        ).status_code
        == 200
    )
    result = c.get("/v1/calls/" + call["id"], headers=h).json()
    assert not result["conversation_state"].get("observations")


def test_inventory_sync_reconciles_existing_provider_configuration(workspace, app, carrier, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    number = c.get("/v1/workspace", headers=h).json()["numbers"][0]

    async def inventory():
        return [
            {
                "id": "owned-provider-id",
                "phone_number": number["phone"],
                "connection_id": "changed-connection",
            }
        ]

    monkeypatch.setattr(carrier, "list_numbers", inventory)
    response = c.post("/v1/admin/numbers/sync", headers=h)
    assert response.status_code == 200
    saved = next(x for x in c.get("/v1/workspace", headers=h).json()["numbers"] if x["id"] == number["id"])
    assert saved["provider_id"] == "owned-provider-id"
    assert saved["connection_id"] == "changed-connection"
    assert response.json()["updated"] == 1


def test_existing_number_configuration_is_resumable_without_purchase(workspace, app, carrier, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    calls = []
    number = c.get("/v1/workspace", headers=h).json()["numbers"][0]

    async def configure(number_id, connection_id):
        calls.append((number_id, connection_id))
        return {"id": number_id, "connection_id": connection_id}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    service = app.state.service
    service.vault.write(workspace["tenant_id"], {"telnyx_connection_id": "allowed-connection"})
    with service.db.transaction() as s:
        update(
            service.required(s, "number_inventory", number["id"], principal(workspace)), provider_id="owned"
        )
    for _ in range(2):
        response = c.post("/v1/admin/numbers/" + number["id"] + "/configure", headers=h)
        assert response.status_code == 200, response.text
    assert calls == [("owned", "allowed-connection")]
    assert not carrier.purchases
    assert response.json()["callback_tested"] is False
