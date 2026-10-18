"""Delayed work must obey the same fixed-template and receipt contracts."""

import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest
from app.db import put
from app.providers.batch import OpenAIBatch
from test_preparation_completion import TEMPLATE, configure
from test_remediation_rep_workflow import p


@pytest.fixture
def deferred(workspace, app, monkeypatch, clock):
    configure(workspace)
    client, headers = workspace["client"], workspace["headers"]
    lead_id = workspace["lead_ids"][0]
    service = app.state.service
    with service.db.transaction() as s:
        lead = service.required(s, "leads", lead_id, p(workspace))
        fact = put(
            s,
            "research_facts",
            p(workspace)["tenant_id"],
            p(workspace)["seller_id"],
            {
                "fact_id": "batch-source",
                "lead_id": lead_id,
                "domain": lead.data["domain"],
                "entity_domain": lead.data["domain"],
                "statement": "The company offers maintenance.",
                "source_id": "synthetic-source",
                "status": "supported",
                "expires_at": (clock.now + timedelta(days=1)).isoformat(),
            },
            object_id="batch-source",
            now=clock.now,
        )
    client.put(
        "/v1/admin/settings",
        headers=headers,
        json={"inference_mode": "api", "openai_api_key": "synthetic-batch-key"},
    )
    state = {
        "requests": [],
        "id": "batch-fixture",
        "candidate": {
            "lead_id": lead_id,
            "slots": {"relevance": {"text": "", "fact_ids": []}, "proof": {"text": "", "fact_ids": []}},
        },
        "cost": 0.004,
        "fact_id": fact.id,
    }

    async def submit(self, requests):
        state["requests"].extend(requests)
        return {"id": "batch-fixture", "status": "in_progress"}

    async def retrieve(self, batch_id):
        return {
            "id": state["id"],
            "status": "completed",
            "results": [
                {
                    "custom_id": state["requests"][0]["custom_id"],
                    "response": {
                        "status_code": 200,
                        "body": {
                            "id": "resp-batch-fixture",
                            "status": "completed",
                            "output": [
                                {"content": [{"type": "output_text", "text": json.dumps(state["candidate"])}]}
                            ],
                            "usage": {
                                "input_tokens": 50,
                                "output_tokens": 20,
                                **({"cost": state["cost"]} if state["cost"] is not None else {}),
                            },
                        },
                    },
                }
            ],
        }

    monkeypatch.setattr(OpenAIBatch, "submit", submit)
    monkeypatch.setattr(OpenAIBatch, "retrieve", retrieve)
    response = client.post(
        "/v1/admin/batches/scripts",
        headers=headers,
        json={"lead_ids": [lead_id], "idempotency_key": "fixed-template"},
    )
    assert response.status_code == 200, response.text
    state["intent"] = response.json()
    state["lead_id"] = lead_id
    state["finish"] = lambda: client.post(
        f"/v1/admin/batches/{state['intent']['id']}/reconcile", headers=headers
    )
    return state


def test_delayed_request_uses_slot_schema_and_publishes_current_template(deferred, workspace, app):
    request = deferred["requests"][0]
    assert set(request["body"]["text"]["format"]["schema"]["properties"]) == {"lead_id", "slots"}
    result = deferred["finish"]()
    assert result.status_code == 200, result.text
    assert result.json()["published"] == [deferred["lead_id"]]
    packet = app.state.service.packet(p(workspace), deferred["lead_id"])
    assert packet["script"]["gatekeeper"] == "Could you direct me to whoever handles customer service?"
    assert packet["preparation"]["status"] == "ready"
    version = packet["script"]["script_version"]
    assert deferred["finish"]().json() == result.json()
    assert app.state.service.packet(p(workspace), deferred["lead_id"])["script"]["script_version"] == version


def test_delayed_request_cannot_replace_template_after_edit(deferred, workspace, app):
    # A legacy full-script response is valid under the old adapter. It must still
    # be rejected when its template/source context has changed during the batch.
    service = app.state.service
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", deferred["lead_id"], p(workspace))
        deferred["candidate"] = service.fallback_script(s, p(workspace), lead)
    configure(workspace, script_template={**TEMPLATE, "opener": "Changed from {seller}."})
    result = deferred["finish"]()
    assert result.status_code == 200
    assert result.json()["published"] == []
    assert result.json()["retry_lead_ids"] == [deferred["lead_id"]]
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "script_versions", p(workspace))


def test_batch_result_must_match_the_persisted_provider_identity(deferred):
    deferred["id"] = "another-batch"
    assert deferred["finish"]().status_code == 409


@pytest.mark.parametrize("cost", [0.004, None])
def test_rejected_result_settles_one_budget_hold_and_preserves_uncertainty(deferred, workspace, app, cost):
    deferred["cost"] = cost
    deferred["candidate"] = {"lead_id": "wrong-lead"}
    for _ in range(2):
        assert deferred["finish"]().status_code == 200
    service = app.state.service
    with service.db.transaction(False) as s:
        holds = service.scoped(s, "usage_ledger", p(workspace))
        assert len(holds) == 1
        assert holds[0].data["status"] == ("settled" if cost is not None else "uncertain")
        assert holds[0].data["cost_usd"] == cost
        assert holds[0].data["reserved_usd"] == (0 if cost is not None else 0.1)


def test_delayed_result_does_not_overwrite_newer_immediate_script(deferred, workspace, app, monkeypatch):
    service = app.state.service
    monkeypatch.setattr(service, "inference", lambda *args: None)
    client = workspace["client"]
    client.post(
        "/v1/research/batches", headers=workspace["headers"], json={"lead_ids": [deferred["lead_id"]]}
    )
    client.post("/v1/admin/jobs/run", headers=workspace["headers"])
    before = service.packet(p(workspace), deferred["lead_id"])["script"]["script_version"]
    assert before != "approved-fallback-v1"
    result = deferred["finish"]()
    assert result.status_code == 200
    assert result.json()["published"] == []
    assert result.json()["retry_lead_ids"] == [deferred["lead_id"]]
    assert service.packet(p(workspace), deferred["lead_id"])["script"]["script_version"] == before


@pytest.mark.asyncio
async def test_batch_preparation_reads_do_not_block_control_event_loop(deferred, workspace, app, monkeypatch):
    service = app.state.service
    packet = service.packet

    def slow_packet(*args):
        time.sleep(0.2)
        return packet(*args)

    monkeypatch.setattr(service, "packet", slow_packet)
    delays = []

    async def heartbeat():
        for _ in range(8):
            started = time.monotonic()
            await asyncio.sleep(0.02)
            delays.append(time.monotonic() - started)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        result, _ = await asyncio.gather(
            client.post(
                "/v1/admin/batches/scripts",
                headers=workspace["headers"],
                json={"lead_ids": [deferred["lead_id"]], "idempotency_key": "slow-build"},
            ),
            heartbeat(),
        )
    assert result.status_code == 200, result.text
    assert max(delays) < 0.1, delays


def test_removed_batch_evidence_is_an_object_retry_not_a_reconciliation_failure(deferred, workspace, app):
    context = json.loads(deferred["requests"][0]["body"]["input"][1]["content"])
    assert context["facts"][0]["fact_id"] == deferred["fact_id"]
    deferred["candidate"]["slots"]["relevance"] = {
        "text": "The company offers maintenance.",
        "fact_ids": [deferred["fact_id"]],
    }
    service = app.state.service
    with service.db.transaction() as s:
        s.delete(service.required(s, "research_facts", deferred["fact_id"], p(workspace)))
    result = deferred["finish"]()
    assert result.status_code == 200, result.text
    assert result.json()["published"] == []
    assert result.json()["retry_lead_ids"] == [deferred["lead_id"]]


def test_changed_fact_statement_invalidates_a_previously_filled_slot(deferred, workspace, app):
    from app.db import update

    deferred["candidate"]["slots"]["relevance"] = {
        "text": "The company offers maintenance.",
        "fact_ids": [deferred["fact_id"]],
    }
    service = app.state.service
    with service.db.transaction() as s:
        fact = service.required(s, "research_facts", deferred["fact_id"], p(workspace))
        update(fact, statement="The company no longer offers maintenance.")
    result = deferred["finish"]()
    assert result.status_code == 200, result.text
    assert result.json()["published"] == []
    assert result.json()["retry_lead_ids"] == [deferred["lead_id"]]


def test_batch_budget_is_bound_to_its_intent_and_legacy_binding_is_repaired(deferred, workspace, app):
    from app.db import update

    service, principal = app.state.service, p(workspace)
    with service.db.transaction() as s:
        hold = next(
            r
            for r in service.scoped(s, "usage_ledger", principal)
            if r.data.get("work_kind") == "script_batch"
        )
        assert hold.data["object_id"] == deferred["intent"]["id"]
        update(hold, object_id=None)  # Actual installed-version reservation shape.
        identity = hold.id
    response = deferred["finish"]()
    assert response.status_code == 200, response.text
    with service.db.transaction(False) as s:
        hold = service.required(s, "usage_ledger", identity, principal)
        assert hold.data["object_id"] == deferred["intent"]["id"]
        assert hold.data["cost_usd"] == 0.004


def test_batch_creation_failure_cannot_leave_an_orphan_budget_hold(workspace, app, monkeypatch):
    import app.preparation.batches as batches

    service, principal = app.state.service, p(workspace)
    service.vault.write(principal["tenant_id"], {"inference_mode": "api", "openai_api_key": "synthetic"})
    original = batches.put

    def fail_intent(s, kind, *args, **kwargs):
        if kind == "command_outbox":
            raise RuntimeError("Intent storage failed")
        return original(s, kind, *args, **kwargs)

    monkeypatch.setattr(batches, "put", fail_intent)
    with pytest.raises(RuntimeError, match="Intent storage failed"):
        workspace["client"].post(
            "/v1/admin/batches/scripts",
            headers=workspace["headers"],
            json={
                "lead_ids": workspace["lead_ids"][:1],
                "idempotency_key": "no-orphan-hold",
            },
        )
    with service.db.transaction(False) as s:
        assert not [
            r
            for r in service.scoped(s, "usage_ledger", principal)
            if r.data.get("work_kind") == "script_batch"
        ]


def test_legacy_batch_receipt_can_settle_its_original_unbound_hold(deferred, workspace, app):
    from app.db import update

    service, principal = app.state.service, p(workspace)
    with service.db.transaction() as s:
        hold = next(
            r
            for r in service.scoped(s, "usage_ledger", principal)
            if r.data.get("work_kind") == "script_batch"
        )
        update(hold, object_id=None)
        identity = hold.id
    assert deferred["finish"]().status_code == 200
    with service.db.transaction(False) as s:
        hold = service.required(s, "usage_ledger", identity, principal)
        assert hold.data["object_id"] == deferred["intent"]["id"] and hold.data["cost_usd"] == 0.004
