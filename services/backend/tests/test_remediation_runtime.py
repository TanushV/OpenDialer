"""Runtime contracts authored before consolidating the worker processes."""

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_critical_lanes_and_maintenance_run_independently_without_training():
    from app.runtime import AuthorityRuntime

    seen = set()
    gate = asyncio.Event()

    class Service:
        settings = SimpleNamespace(worker_enabled=True)

        async def maintenance_tick(self):
            seen.add("maintenance")

        async def run_jobs(self, *, queues):
            seen.update(queues)
            if "live_coach" in queues:
                await gate.wait()

    runtime = AuthorityRuntime(Service(), queues=["webhook", "live_coach", "postcall", "maintenance"])
    await runtime.start()
    try:
        for _ in range(20):
            if {"maintenance", "webhook", "live_coach", "postcall"} <= seen:
                break
            await asyncio.sleep(0.01)
        assert {"maintenance", "webhook", "live_coach", "postcall"} <= seen
    finally:
        await runtime.close()
    assert all(task.done() for task in runtime.tasks)


@pytest.mark.asyncio
async def test_normal_maintenance_schedules_health_and_retry_work(workspace, app, monkeypatch):
    seen = []
    monkeypatch.setattr(app.state.service, "schedule_optimization", lambda tenant_id=None: seen.append(True))
    await app.state.service.maintenance_tick()
    await app.state.service.maintenance_tick()
    assert seen == [True], "Periodic maintenance runs once per tick interval, independently of model training"


def test_connected_default_has_one_authority_and_one_background_worker():
    from pathlib import Path

    import yaml

    document = yaml.safe_load((Path(__file__).resolve().parents[3] / "deploy/compose.yml").read_text())
    services = document["services"]
    workers = [v for name, v in services.items() if name.startswith("worker") and not v.get("profiles")]
    assert len(workers) == 1
    api = services["api"]["environment"]
    assert str(api["OPENDIALER_WORKER_ENABLED"]).lower() == "true"
    assert "webhook" in api["OPENDIALER_WORKER_QUEUES"]
    assert "postcall" in api["OPENDIALER_WORKER_QUEUES"], "Raw live content stays in the authority process"
    assert "maintenance" in api["OPENDIALER_WORKER_QUEUES"]
