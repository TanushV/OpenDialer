"""A06/A30/A49: original deferred-effect recovery, no duplicate submissions."""

import asyncio
from datetime import timedelta

import pytest
from app.db import doc, get, update
from app.providers.batch import OpenAIBatch
from test_remediation_rep_workflow import p


@pytest.fixture
def batch(workspace, app, monkeypatch):
    service, principal = app.state.service, p(workspace)
    service.vault.write(principal["tenant_id"], {"inference_mode": "api", "openai_api_key": "synthetic-key"})
    state = {"sent": [], "reads": [], "lose": True, "metadata": {}}

    async def submit(provider, requests):
        state["sent"].append(requests)
        state["metadata"] = getattr(provider, "metadata", {})
        if state["lose"]:
            raise TimeoutError("sensitive response must not enter recovery status")
        return {"id": "batch-original", "status": "in_progress", "metadata": state["metadata"]}

    async def retrieve(provider, identity):
        state["reads"].append(identity)
        return {"id": identity, "status": "in_progress", "metadata": state["metadata"]}

    monkeypatch.setattr(OpenAIBatch, "submit", submit)
    monkeypatch.setattr(OpenAIBatch, "retrieve", retrieve)
    state["retrieve"] = retrieve
    state["service"], state["p"] = service, principal
    state["lead"] = (
        workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"]
    )
    return state


def submit(w, b, key="original-submission"):
    return w["client"].post(
        "/v1/admin/batches/scripts",
        headers=w["headers"],
        json={
            "lead_ids": [b["lead"]],
            "idempotency_key": key,
        },
    )


def stored(b):
    with b["service"].db.transaction(False) as s:
        return next(
            doc(r)
            for r in b["service"].scoped(s, "command_outbox", b["p"])
            if r.data.get("kind") == "script_batch"
        )


def reconcile(w, row, **patch):
    return w["client"].post(
        f"/v1/admin/batches/{row['id']}/reconcile",
        headers=w["headers"],
        json={
            "expected_version": row["version"],
            "provider_batch_id": "batch-original",
            **patch,
        },
    )


def test_uncertain_batch_is_listed_without_sending_or_exposing_prompts(workspace, batch):
    assert submit(workspace, batch).status_code == 503
    original = stored(batch)
    response = workspace["client"].get("/v1/admin/batches", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    item = next(r for r in response.json()["batches"] if r["id"] == original["id"])
    assert item["status"] == "uncertain" and item["can_reconcile"]
    assert (
        "contexts" not in item and "sensitive" not in response.text and "synthetic-key" not in response.text
    )
    assert len(batch["sent"]) == 1 and not batch["reads"] and stored(batch) == original


def test_second_click_with_new_key_cannot_resubmit_pending_leads(workspace, batch):
    assert submit(workspace, batch).status_code == 503
    result = submit(workspace, batch, "accidental-second-key")
    assert result.status_code == 409, result.text
    assert len(batch["sent"]) == 1


def test_recover_original_batch_by_bound_metadata_without_resubmission(workspace, batch):
    assert submit(workspace, batch).status_code == 503
    row = stored(batch)
    assert batch["metadata"].get("opendialer_intent") == row["id"]
    response = reconcile(workspace, row)
    assert response.status_code == 200, response.text
    current = stored(batch)
    assert current["provider_batch_id"] == "batch-original" and current["status"] == "in_progress"
    assert len(batch["sent"]) == 1
    with batch["service"].db.transaction(False) as s:
        holds = batch["service"].scoped(s, "usage_ledger", batch["p"])
        assert len(holds) == 1 and holds[0].data["reserved_usd"] == 0.1


@pytest.mark.parametrize("bad", ["metadata", "identity", "status"])
def test_unrelated_or_invalid_recovery_receipt_cannot_bind(workspace, batch, monkeypatch, bad):
    assert submit(workspace, batch).status_code == 503
    original = stored(batch)

    async def wrong(provider, identity):
        value = await batch["retrieve"](provider, identity)
        value[{"metadata": "metadata", "identity": "id", "status": "status"}[bad]] = {
            "metadata": {"opendialer_intent": "another-intent"},
            "identity": "another-batch",
            "status": "invented",
        }[bad]
        return value

    monkeypatch.setattr(OpenAIBatch, "retrieve", wrong)
    response = reconcile(workspace, original)
    assert response.status_code == 409, response.text
    assert not stored(batch).get("provider_batch_id") and len(batch["sent"]) == 1


def test_recovery_expected_version_is_checked_before_provider_request(workspace, batch):
    assert submit(workspace, batch).status_code == 503
    response = reconcile(workspace, stored(batch), expected_version=999)
    assert response.status_code == 409 and not batch["reads"]


@pytest.mark.parametrize("change", ["role", "lease"])
def test_late_recovery_cannot_publish_after_owner_or_authorization_change(
    workspace, batch, monkeypatch, change
):
    batch["lose"] = False
    assert submit(workspace, batch).status_code == 200
    row = stored(batch)

    async def changed(provider, identity):
        with batch["service"].db.transaction() as s:
            if change == "role":
                update(get(s, "users", batch["p"]["user_id"]), role="rep")
            else:
                update(get(s, "command_outbox", row["id"]), lease_owner="replacement-owner")
        return {"id": identity, "status": "completed", "results": []}

    monkeypatch.setattr(OpenAIBatch, "retrieve", changed)
    result = reconcile(workspace, row)
    assert result.status_code in {403, 409}, result.text
    assert stored(batch)["status"] != "completed"


@pytest.mark.parametrize("value", [{}, {"id": "batch-original", "status": "invented"}])
def test_malformed_submission_is_uncertain_not_a_permanent_sending_row(workspace, batch, monkeypatch, value):
    async def invalid(provider, requests):
        batch["sent"].append(requests)
        return value

    monkeypatch.setattr(OpenAIBatch, "submit", invalid)
    result = submit(workspace, batch)
    assert result.status_code == 503, result.text
    assert stored(batch)["status"] == "uncertain"


def test_cancelled_submission_retains_liability_and_recovery_state(workspace, batch, monkeypatch):
    import httpx

    async def cancelled(provider, requests):
        batch["sent"].append(requests)
        raise asyncio.CancelledError()

    monkeypatch.setattr(OpenAIBatch, "submit", cancelled)

    async def attempt():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workspace["client"].app), base_url="http://127.0.0.1"
        ) as c:
            await c.post(
                "/v1/admin/batches/scripts",
                headers=workspace["headers"],
                json={"lead_ids": [batch["lead"]], "idempotency_key": "cancelled"},
            )

    # Starlette's HTTP middleware can translate an internally cancelled handler
    # into its no-response error. Either transport outcome still owes recovery.
    with pytest.raises((asyncio.CancelledError, RuntimeError)) as error:
        asyncio.run(attempt())
    if isinstance(error.value, RuntimeError):
        assert str(error.value) == "No response returned."
    assert stored(batch)["status"] == "uncertain"
    with batch["service"].db.transaction(False) as s:
        assert batch["service"].scoped(s, "usage_ledger", batch["p"])[0].data["status"] == "uncertain"


def test_expired_sending_is_visible_without_retrying_submission(workspace, batch, clock):
    assert submit(workspace, batch).status_code == 503
    row = stored(batch)
    with batch["service"].db.transaction() as s:
        update(
            get(s, "command_outbox", row["id"]),
            status="sending",
            lease_until=None,
            submitted_at=(clock.now - timedelta(hours=1)).isoformat(),
        )
    response = workspace["client"].get("/v1/admin/batches", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["batches"][0]["status"] == "uncertain" and len(batch["sent"]) == 1


def test_cross_seller_batch_is_not_visible_or_recoverable(workspace, batch):
    assert submit(workspace, batch).status_code == 503
    row = stored(batch)
    with batch["service"].db.transaction() as s:
        get(s, "command_outbox", row["id"]).seller_id = "other-seller"
    assert reconcile(workspace, row).status_code == 404
    response = workspace["client"].get("/v1/admin/batches", headers=workspace["headers"])
    assert response.status_code == 200 and response.json()["batches"] == []


@pytest.mark.parametrize(
    "identity", ["../../files/private", "https://untrusted.test/batch", "batch?secret=1"]
)
def test_operator_reference_is_not_an_arbitrary_provider_path(workspace, batch, identity):
    assert submit(workspace, batch).status_code == 503
    result = reconcile(workspace, stored(batch), provider_batch_id=identity)
    assert result.status_code == 422 and not batch["reads"]


@pytest.mark.parametrize("matches", [True, False])
def test_legacy_unknown_reference_requires_original_input_content(workspace, batch, monkeypatch, matches):
    assert submit(workspace, batch).status_code == 503
    row = stored(batch)
    with batch["service"].db.transaction() as s:
        update(get(s, "command_outbox", row["id"]), request_digest=None)

    async def legacy(provider, identity):
        return {"id": identity, "status": "in_progress", "input_file_id": "file-original"}

    async def inputs(provider, identity):
        assert identity == "file-original"
        return batch["sent"][0] if matches else [{"custom_id": "another-request"}]

    monkeypatch.setattr(OpenAIBatch, "retrieve", legacy)
    monkeypatch.setattr(OpenAIBatch, "input_requests", inputs)
    response = reconcile(workspace, stored(batch))
    assert response.status_code == (200 if matches else 409), response.text
    assert bool(stored(batch).get("provider_batch_id")) == matches
    assert len(batch["sent"]) == 1


def test_recovery_heartbeat_keeps_the_owned_operation_live(workspace, batch, clock, monkeypatch):
    import app.preparation.batches as module

    batch["lose"] = False
    assert submit(workspace, batch).status_code == 200
    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.01, raising=False)

    async def slow(provider, identity):
        for _ in range(3):
            clock.now += timedelta(seconds=80)
            await asyncio.sleep(0.15)
        return {"id": identity, "status": "in_progress"}

    monkeypatch.setattr(OpenAIBatch, "retrieve", slow)
    response = reconcile(workspace, stored(batch))
    assert response.status_code == 200, response.text
    assert stored(batch)["lease_owner"] is None


def test_expired_submission_ack_preserves_reference_but_requires_recovery(
    workspace, batch, clock, monkeypatch
):
    async def late(provider, requests):
        batch["sent"].append(requests)
        clock.now += timedelta(seconds=181)
        return {"id": "batch-original", "status": "in_progress"}

    monkeypatch.setattr(OpenAIBatch, "submit", late)
    result = submit(workspace, batch)
    assert result.status_code == 200, result.text
    row = stored(batch)
    assert row["status"] == "uncertain" and row["provider_batch_id"] == "batch-original"


def test_listing_does_not_decrypt_unrelated_command_history(workspace, batch, monkeypatch, clock, app):
    from app.db import put
    from test_context_reads import count_decrypted

    assert submit(workspace, batch).status_code == 503
    with batch["service"].db.transaction() as s:
        for i in range(600):
            put(
                s,
                "command_outbox",
                batch["p"]["tenant_id"],
                batch["p"]["seller_id"],
                {"kind": "call_control", "status": "done"},
                key=f"history-{i}",
                now=clock.now,
            )
    reads = count_decrypted(app, monkeypatch)
    response = workspace["client"].get("/v1/admin/batches", headers=workspace["headers"])
    assert response.status_code == 200 and len(response.json()["batches"]) == 1
    assert len(reads) <= 5 and not batch["reads"]
