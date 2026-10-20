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

    from app.priority import BACKGROUND_YIELD_SECONDS

    runtime = AuthorityRuntime(Service(), queues=["webhook", "live_coach", "postcall", "maintenance"])
    await runtime.start()
    try:
        # Background lanes yield to in-flight live work, but only up to the bounded ceiling:
        # a stuck live job never stops post-call work.
        deadline = asyncio.get_running_loop().time() + BACKGROUND_YIELD_SECONDS + 1.5
        while asyncio.get_running_loop().time() < deadline:
            if {"maintenance", "webhook", "live_coach", "postcall"} <= seen:
                break
            await asyncio.sleep(0.01)
        assert {"maintenance", "webhook", "live_coach", "postcall"} <= seen
        assert not gate.is_set(), "Post-call ran while the live job was still blocked"
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


@pytest.mark.asyncio
async def test_idle_disconnected_websocket_stops_without_waiting_for_an_event(workspace, app):
    from app.db import Event
    from sqlalchemy import func, select

    with app.state.db.transaction(False) as s:
        cursor = s.scalar(select(func.max(Event.sequence))) or 0

    class IdleDisconnected:
        headers = {}
        closed = False

        async def accept(self):
            pass

        async def receive_json(self):
            return {"token": workspace["access_token"], "after": cursor}

        async def receive(self):
            return {"type": "websocket.disconnect", "code": 1000}

        async def send_json(self, value):
            raise AssertionError("No new event should be needed to observe disconnect")

        async def close(self, code):
            self.closed = True

    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/v1/live")
    await asyncio.wait_for(endpoint(IdleDisconnected()), timeout=0.5)
