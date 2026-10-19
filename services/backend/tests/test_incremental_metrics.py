"""E09/C035: operational reports use replaceable source contributions."""

from datetime import timedelta

import pytest
from app.db import MODELS, doc, get, put, update
from conftest import completed_report
from test_remediation_privacy import principal


@pytest.mark.asyncio
async def test_contribution_replay_oracle(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    # Independent set arithmetic: three imported leads, duplicate evidence for one.
    ids = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"]
    events = []
    for lead in ids[:2]:
        with service.db.transaction() as s:
            for i in range(2):
                row = put(
                    s,
                    "conversion_events",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "lead_id": lead["id"],
                        "kind": "meeting_held",
                        "occurred_at": clock.now.isoformat(),
                        "observed_at": clock.now.isoformat(),
                        "label_available_at": clock.now.isoformat(),
                        "status": "confirmed",
                        "retracted": False,
                    },
                    key=f"oracle:{lead['id']}:{i}",
                    now=clock.now,
                )
                events.append(doc(row))
    first = completed_report(workspace).json()
    expected = len({event["lead_id"] for event in events if not event["retracted"]})
    assert first["metrics"]["unique_conversions"] == expected == 2
    with service.db.transaction() as s:
        for event in events:
            if event["lead_id"] == ids[0]["id"]:
                update(
                    get(s, "conversion_events", event["id"]),
                    retracted=True,
                    retracted_at=clock.now.isoformat(),
                )
                event["retracted"] = True
    second = completed_report(workspace).json()
    assert (
        second["metrics"]["unique_conversions"]
        == len({e["lead_id"] for e in events if not e["retracted"]})
        == 1
    )
    with service.db.transaction() as s:
        for event in events:
            if event["lead_id"] == ids[1]["id"]:
                s.delete(get(s, "conversion_events", event["id"]))
    third = completed_report(workspace).json()
    assert third["metrics"]["unique_conversions"] == 0
    assert {"metric_contributions", "metric_buckets"} <= MODELS.keys(), (
        "A saved whole report is not an incremental contribution ledger"
    )
    with service.db.transaction(False) as s:
        before = [(r.id, r.version, r.data) for r in service.scoped(s, "metric_contributions", p)]
    await service.run_jobs(p["tenant_id"], queues=["ml"])
    with service.db.transaction(False) as s:
        assert [(r.id, r.version, r.data) for r in service.scoped(s, "metric_contributions", p)] == before


@pytest.mark.asyncio
async def test_normal_correction_does_not_rebuild_entire_operational_dataset(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    completed_report(workspace)
    rebuilds = []
    original = service.operational_dataset

    def counted(*args, **kwargs):
        rebuilds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "operational_dataset", counted)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        update(lead, next_attempt_at=(clock.now + timedelta(hours=1)).isoformat())
    completed_report(workspace)
    assert not rebuilds, "Changing one due time cannot reconstruct historical training/report rows"
