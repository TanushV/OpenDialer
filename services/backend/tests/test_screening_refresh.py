"""E05/C019: prepaid refresh is an explicitly budgeted, resumable operation."""

import asyncio
from datetime import timedelta

import pytest
from app.db import Job, update
from sqlalchemy import select
from test_dnc_recovery import PHONE, screening  # noqa: F401 - pytest fixture


@pytest.mark.asyncio
async def test_bounded_paid_refresh(workspace, app, clock, screening):  # noqa: F811 - fixture
    service, p, calls, _, _ = screening
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/suppressions", headers=h, json={"phone": PHONE, "reason": "opt_out"})
    enabled = c.put(
        "/v1/admin/operating-policy",
        headers=h,
        json={
            "dnc_refresh": {
                "enabled": True,
                "interval_hours": 24,
                "batch_size": 500,
                "max_run_usd": 1,
                "max_day_usd": 2,
            }
        },
    )
    assert enabled.status_code == 200, enabled.text
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, published=False)
    await asyncio.gather(service.maintenance_tick(), service.maintenance_tick())
    for _ in range(3):
        await service.run_jobs(p["tenant_id"], queues=["research", "scripts"])
    assert PHONE not in calls
    assert calls and len(calls) == len(set(calls)) <= 500
    count = len(calls)
    await service.maintenance_tick()
    await service.run_jobs(p["tenant_id"], queues=["research", "scripts"])
    assert len(calls) == count, "Repeated ticks in the same daily window cannot recharge the same clearances"
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.tenant_id == p["tenant_id"])))
        refresh = [j for j in jobs if j.payload.get("kind") == "dnc_refresh"]
        assert refresh and all(j.payload.get("cursor") is not None for j in refresh)
    clock.now += timedelta(days=1)
    await service.maintenance_tick()
    await service.run_jobs(p["tenant_id"], queues=["research", "scripts"])
    assert len(calls) == count, "Still-fresh exact cache is reused in a new scheduling window"


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": True, "max_run_usd": 0, "max_day_usd": 1},
        {"enabled": True, "max_run_usd": 1, "max_day_usd": 0},
        {"enabled": True, "max_run_usd": -1, "max_day_usd": 1},
        {"enabled": True, "max_run_usd": 1, "max_day_usd": 1, "batch_size": 501},
    ],
)
def test_screening_refresh_requires_bounded_explicit_funding(workspace, settings):
    result = workspace["client"].put(
        "/v1/admin/operating-policy", headers=workspace["headers"], json={"dnc_refresh": settings}
    )
    assert result.status_code == 422


def test_screening_refresh_disabled_default_is_visible(workspace):
    policy = workspace["client"].get("/v1/admin/operating-policy", headers=workspace["headers"]).json()
    values = policy.get("policy", policy)
    assert values.get("dnc_refresh", {}).get("enabled") is False


@pytest.mark.asyncio
async def test_whole_list_refresh_resumes_after_one_500_endpoint_page(
    workspace, app, clock, carrier, screening, monkeypatch  # noqa: F811 - imported fixture
):
    from app.main import create_app
    from app.providers.dnc import PrepaidDNC
    from fastapi.testclient import TestClient

    service, p, calls, _, _ = screening
    c, h = workspace["client"], workspace["headers"]
    for lead in c.get("/v1/workspace", headers=h).json()["leads"]:
        assert (
            c.post(
                "/v1/suppressions", headers=h, json={"phone": lead["phone"], "reason": "opt_out"}
            ).status_code
            == 200
        )
    numbers = [f"+1415555{i:04}" for i in range(1000, 1501)]
    imported = c.post(
        "/v1/leads/import",
        headers=h,
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [
                {"name": f"Refresh {i}", "phone": phone, "timezone": "America/Los_Angeles"}
                for i, phone in enumerate(numbers)
            ],
        },
    )
    assert imported.status_code == 200
    enabled = c.put(
        "/v1/admin/operating-policy",
        headers=h,
        json={"dnc_refresh": {"enabled": True, "batch_size": 500, "max_run_usd": 1, "max_day_usd": 2}},
    )
    assert enabled.status_code == 200, enabled.text
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, published=False)

    async def funded_check(provider, phone):
        calls.append(phone)
        return {
            "phone": phone,
            "listed": False,
            "checked_at": clock.now.isoformat(),
            "provider": "donotcalldnc",
            "source_authorization_required": True,
            "credits_remaining": 1000 - len(calls),
        }

    monkeypatch.setattr(PrepaidDNC, "check", funded_check)
    await service.maintenance_tick()
    await service.run_jobs(p["tenant_id"], queues=["research"])
    assert 0 < len(calls) <= 500
    first = set(calls)
    restarted = create_app(service.settings, clock=clock, carrier=carrier)
    with TestClient(restarted):
        for _ in range(4):
            await restarted.state.service.maintenance_tick()
            await restarted.state.service.run_jobs(p["tenant_id"], queues=["research"])
    assert set(calls) == set(numbers) and len(calls) == 501
    assert first <= set(calls)
