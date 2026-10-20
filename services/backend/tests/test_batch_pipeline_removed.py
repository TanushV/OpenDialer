"""The deferred OpenAI Batch preparation pipeline is removed; its legacy rows close safely.

Immediate preparation is unchanged. Rows and jobs written by earlier releases must never
be resubmitted, and must not keep sign-out or an authority switch blocked forever.
"""

import pytest
from app.db import Job, get, put
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_remediation_privacy import principal

IN_FLIGHT = [
    "sending",
    "uncertain",
    "reconciling",
    "review_required",
    "validating",
    "in_progress",
    "finalizing",
    "cancelling",
    "awaiting_publication",
    "awaiting_reviews",
]


def legacy_batch(s, p, status, key):
    return put(
        s,
        "command_outbox",
        p["tenant_id"],
        p["seller_id"],
        {
            "kind": "script_batch",
            "status": status,
            "lead_ids": ["lead-a"],
            "provider_batch_id": "batch-legacy",
            "request_digest": "digest",
        },
        key=key,
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/v1/admin/batches"),
        ("post", "/v1/admin/batches/scripts"),
        ("post", "/v1/admin/batches/legacy-intent/reconcile"),
    ],
)
def test_batch_routes_are_gone(workspace, method, path):
    client, headers = workspace["client"], workspace["headers"]
    response = getattr(client, method)(path, headers=headers, **({"json": {}} if method == "post" else {}))
    assert response.status_code == 404, response.text


def test_batch_bounds_are_not_published(workspace):
    bounds = workspace["client"].get("/v1/definitions", headers=workspace["headers"]).json()["bounds"]
    assert "deferred_batch" not in bounds
    assert "preparation" in bounds, "Immediate preparation bounds remain"


def test_legacy_in_flight_batches_close_at_startup_without_resubmission(workspace, app, carrier):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        rows = {status: legacy_batch(s, p, status, "script-batch:" + status).id for status in IN_FLIGHT}
        finished = legacy_batch(s, p, "completed", "script-batch:done").id
        hold = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {"kind": "inference", "work_kind": "script_batch", "status": "uncertain", "reserved_usd": 0.1},
            key="script-batch:uncertain",
        ).id
    with service.db.transaction(False) as s:
        assert service.quiescence_in(s, p)["reasons"] == ["EXTERNAL_OPERATION_UNRESOLVED"]

    restarted = create_app(service.settings, clock=service.clock, carrier=carrier)
    try:
        with restarted.state.db.transaction(False) as s:
            for status, identity in rows.items():
                row = get(s, "command_outbox", identity)
                assert row.data["status"] == "cancelled", status
                assert row.data["recovery_reason"] == "batch_pipeline_removed"
                assert row.data["previous_status"] == status
                # The original provider identity and liability stay auditable.
                assert row.data["provider_batch_id"] == "batch-legacy"
            done = get(s, "command_outbox", finished)
            assert done.data["status"] == "completed" and "recovery_reason" not in done.data
            assert get(s, "usage_ledger", hold).data["status"] == "uncertain"
            closed = [
                e
                for e in restarted.state.service.scoped(s, "audit_events", p)
                if e.data.get("action") == "batch.pipeline_removed"
            ]
            assert sorted(e.data["target_id"] for e in closed) == sorted(rows.values())
            assert restarted.state.service.quiescence_in(s, p) == {"can_disconnect": True, "reasons": []}
            versions = {i: get(s, "command_outbox", i).version for i in rows.values()}
        with TestClient(restarted) as client:
            assert client.post("/v1/auth/logout", headers=workspace["headers"]).status_code == 200
    finally:
        restarted.state.db.engine.dispose()

    again = create_app(service.settings, clock=service.clock, carrier=carrier)
    try:
        with again.state.db.transaction(False) as s:
            assert {i: get(s, "command_outbox", i).version for i in rows.values()} == versions
    finally:
        again.state.db.engine.dispose()


@pytest.mark.parametrize("source", ["backup_row", "effect_journal"])
def test_restored_legacy_batch_is_closed_not_held_for_review(workspace, app, source):
    service, p = app.state.service, principal(workspace)
    if source == "effect_journal":
        backup = service.backup(p)
    with service.db.transaction() as s:
        row = legacy_batch(s, p, "sending", "script-batch:restored")
        service.journal_effect_in(s, p, "script_batch", row.external_key, "intent", [row])
        identity = row.id
    if source == "backup_row":
        backup = service.backup(p)
    with service.db.transaction() as s:
        s.delete(get(s, "command_outbox", identity))
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        restored = get(s, "command_outbox", identity)
        assert restored is not None
        assert restored.data["status"] == "cancelled"
        assert restored.data["recovery_reason"] == "batch_pipeline_removed"
        assert "EXTERNAL_OPERATION_UNRESOLVED" not in service.quiescence_in(s, p)["reasons"]


async def test_legacy_batch_review_job_drains_without_any_effect(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        lead_id = service.scoped(s, "leads", p)[0].id
        scripts = len(service.scoped(s, "script_versions", p))
    service.jobs.enqueue(
        "scripts",
        {
            "kind": "batch_review",
            "intent_id": "legacy-intent",
            "custom_id": lead_id + ":legacy",
            "result_fingerprint": "digest",
            "output": {"output": []},
            "seller_id": p["seller_id"],
            "user_id": p["user_id"],
        },
        f"batch-review:legacy-intent:{lead_id}:legacy:digest",
        tenant_id=p["tenant_id"],
    )
    await service.run_jobs(p["tenant_id"], ["scripts"])
    with service.db.transaction(False) as s:
        job = s.scalar(select(Job).where(Job.key.like("batch-review:%")))
        assert job.status == "completed", job.error
        assert len(service.scoped(s, "script_versions", p)) == scripts
        assert lead_id not in service.pending_preparation(s, p)
