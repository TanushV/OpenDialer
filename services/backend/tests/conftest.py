"""Acceptance harness written before application implementation (TDD baseline)."""

import os
from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.main import create_app
from app.providers.simulated import SimulatedCarrier
from fastapi.testclient import TestClient

from scripts.disposable_database import validate_database_url

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    return Clock()


@pytest.fixture
def carrier():
    return SimulatedCarrier()


@pytest.fixture
def app(tmp_path, clock, carrier):
    database_url = os.getenv("OPENDIALER_TEST_POSTGRES_URL", f"sqlite:///{tmp_path}/test.db")
    if os.getenv("OPENDIALER_TEST_POSTGRES_URL"):
        validate_database_url(database_url)
    settings = Settings(
        database_url=database_url,
        mode="local",
        local_token="test-local-secret",
        simulation=True,
        data_dir=tmp_path,
        worker_enabled=False,
        bootstrap_token="test-bootstrap-secret",
        secret_key="test-secret-" + "a" * 48,
    )
    application = create_app(settings, clock=clock, carrier=carrier)
    if os.getenv("OPENDIALER_TEST_POSTGRES_URL"):
        application.state.db.reset()
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def workspace(client):
    r = client.post(
        "/v1/setup",
        headers={"X-Local-Token": "test-local-secret"},
        json={
            "seller_name": "Example Seller",
            "campaign_name": "Acceptance campaign",
            "email": "operator@example.test",
            "password": "a-test-only-password-123",
        },
    )
    assert r.status_code == 200, r.text
    credentials = r.json()
    headers = {"Authorization": f"Bearer {credentials['access_token']}"}
    r = client.post("/v1/simulation/seed", headers=headers)
    assert r.status_code == 200, r.text
    return {"client": client, "headers": headers, **r.json(), **credentials}


def ready(workspace, **overrides):
    body = {
        "status": "ready",
        "audio_ready": True,
        "telephony_ready": True,
        "agent_call_control_id": "agent-test-id",
        **overrides,
    }
    r = workspace["client"].post("/v1/rep/presence", headers=workspace["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()


def reserve(workspace, **overrides):
    ready(workspace)
    return workspace["client"].post(
        "/v1/queue/next",
        headers=workspace["headers"],
        json={"campaign_id": workspace["campaign_id"], **overrides},
    )


def start(workspace, key="call-key-001"):
    r = reserve(workspace)
    assert r.status_code == 200, r.text
    packet = r.json()
    r = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": packet["reservation_id"], "idempotency_key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()


def completed_report(workspace, *, headers=None, params=None):
    """Explicitly run the report worker for assertions about its computed evidence."""
    from functools import partial

    client = workspace["client"]
    for _ in range(4):
        response = client.get("/v1/optimization", headers=headers or workspace["headers"], params=params)
        assert response.status_code == 200, response.text
        if response.json()["projection"]["status"] == "ready":
            return response
        client.portal.call(partial(client.app.state.service.run_jobs, queues=["ml"]))
    pytest.fail(f"Background report did not finish: {response.json().get('projection')}")


def completed_home_metrics(workspace):
    """Wait for saved home evidence before asserting corrected historical totals."""
    from functools import partial

    client = workspace["client"]
    for _ in range(4):
        response = client.get("/v1/workspace", headers=workspace["headers"])
        assert response.status_code == 200, response.text
        metrics = response.json()["metrics"]
        if metrics["projection"]["status"] == "ready":
            return metrics
        client.portal.call(partial(client.app.state.service.run_jobs, queues=["ml"]))
    pytest.fail("Background workspace totals did not finish")
