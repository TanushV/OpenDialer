"""Acceptance harness written before application implementation (TDD baseline)."""

import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.main import create_app
from app.providers.simulated import SimulatedCarrier
from fastapi.testclient import TestClient

from scripts.disposable_database import (
    PostgresTestStore,
    drop_worker_database,
    ensure_database,
    validate_database_url,
    worker_database_url,
)

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
POSTGRES_URL = os.getenv("OPENDIALER_TEST_POSTGRES_URL")


def pytest_collection_modifyitems(config, items):
    """Database-specific tests are selected by marker and skipped before any store is built."""
    for item in items:
        if item.get_closest_marker("postgres") and not POSTGRES_URL:
            item.add_marker(
                pytest.mark.skip(reason="Requires the guarded OPENDIALER_TEST_POSTGRES_URL store")
            )
        if item.get_closest_marker("sqlite_only") and POSTGRES_URL:
            item.add_marker(pytest.mark.skip(reason="SQLite-specific oracle; not applicable to PostgreSQL"))


class LoopResponsiveness:
    """Deterministic proof that blocking work leaves the event loop serving other tasks.

    Call ``wait_for_loop`` from inside the slow (blocking) section. Off the loop thread it
    asks the loop to run a callback and waits until it has: a loop blocked by the section
    (for example a synchronous wait on its result) can never serve it, so the regression
    fails by construction. On the loop thread it records the violation immediately. No
    latency bound is involved, so machine load cannot turn a correct implementation red;
    ``limit`` only bounds how long a broken implementation takes to fail.
    """

    def __init__(self, limit=30.0):
        import asyncio
        import threading

        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.limit = limit
        self.observations = []

    def wait_for_loop(self):
        import threading

        if threading.get_ident() == self.loop_thread:
            self.observations.append("ran on the event loop thread")
            return
        served = threading.Event()
        self.loop.call_soon_threadsafe(served.set)
        self.observations.append("loop served" if served.wait(self.limit) else "loop blocked")

    def assert_responsive(self):
        assert self.observations, "Fixture must exercise the blocking section"
        assert set(self.observations) == {"loop served"}, self.observations


class JobClaims:
    """Track job claims in flight so a concurrency test can wait for the scheduler to settle.

    ``settle`` returns once no claim is running in a worker thread and every ready task has
    had several loop turns to reach its next suspension. Work held open until then has given
    every admissible sibling the chance to start, so a measured peak is the real admission
    limit rather than whatever overlap a fixed sleep happened to produce.
    """

    def __init__(self, monkeypatch, *queues):
        import threading

        self.lock = threading.Lock()
        self.in_flight = 0
        for jobs in queues:
            monkeypatch.setattr(jobs, "claim", self._tracked(jobs.claim))

    def _tracked(self, claim):
        def tracked(*args, **kwargs):
            with self.lock:
                self.in_flight += 1
            try:
                return claim(*args, **kwargs)
            finally:
                with self.lock:
                    self.in_flight -= 1

        return tracked

    async def settle(self, turns=5, limit=60.0):
        import asyncio
        import time

        deadline, quiet = time.monotonic() + limit, 0
        while quiet < turns:
            assert time.monotonic() < deadline, "Job scheduler did not settle"
            await asyncio.sleep(0 if self.in_flight == 0 else 0.001)
            quiet = quiet + 1 if self.in_flight == 0 else 0


def fixture_settings(database_url, data_dir):
    return Settings(
        database_url=database_url,
        mode="local",
        local_token="test-local-secret",
        simulation=True,
        data_dir=data_dir,
        worker_enabled=False,
        bootstrap_token="test-bootstrap-secret",
        secret_key="test-secret-" + "a" * 48,
    )


def _initialize_store(database_url, data_dir):
    """The complete fresh-store startup: migrations, business-state cutover and journal replays."""
    started = create_app(
        fixture_settings(database_url, data_dir), clock=lambda: NOW, carrier=SimulatedCarrier()
    )
    started.state.db.engine.dispose()


@pytest.fixture(scope="session")
def sqlite_store_template(tmp_path_factory):
    """One migrated SQLite store per session (per xdist worker), copied for every ``app``.

    The copy satisfies the startup contract, so each test still runs ``create_app`` but skips
    rebuilding the same empty schema. Tests that need a pristine, unmigrated or legacy store
    create their own file and never use this template.
    """
    directory = tmp_path_factory.mktemp("migrated-store")
    _initialize_store(f"sqlite:///{directory}/source.db", directory / "runtime")
    template = directory / "template.db"
    with (
        closing(sqlite3.connect(directory / "source.db")) as source,
        closing(sqlite3.connect(template)) as copy,
    ):
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.backup(copy)
    with closing(sqlite3.connect(template)) as copy:
        copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    for suffix in ("-wal", "-shm"):
        assert (
            not (directory / f"template.db{suffix}").exists()
            or not (directory / f"template.db{suffix}").stat().st_size
        ), "Template must be a single checkpointed file"
    return template


@pytest.fixture(scope="session")
def postgres_store(tmp_path_factory):
    """This worker's own guarded PostgreSQL store, reset between tests without a schema rebuild."""
    url = worker_database_url(validate_database_url(POSTGRES_URL))
    ensure_database(url)
    store = PostgresTestStore(url)
    store.initialize = lambda value: _initialize_store(value, tmp_path_factory.mktemp("postgres-startup"))
    yield store
    store.dispose()
    if url != POSTGRES_URL:
        drop_worker_database(url)


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
def app(request, tmp_path, clock, carrier):
    if POSTGRES_URL:
        store = request.getfixturevalue("postgres_store")
        store.reset(store.initialize)
        database_url = store.url
    else:
        shutil.copyfile(request.getfixturevalue("sqlite_store_template"), tmp_path / "test.db")
        database_url = f"sqlite:///{tmp_path}/test.db"
    application = create_app(fixture_settings(database_url, tmp_path), clock=clock, carrier=carrier)
    yield application
    application.state.db.engine.dispose()


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


def disposition_body(client, headers, call_id, **values):
    """Explicit modern edit envelope for fixtures, never an HTTP-client interceptor."""
    from uuid import uuid4

    current = client.get(f"/v1/calls/{call_id}", headers=headers)
    assert current.status_code == 200, current.text
    return {"request_id": str(uuid4()), "expected_version": current.json()["version"], **values}


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


def completed_metrics(workspace):
    """Read the public saved-metrics endpoint and explicitly advance its worker."""
    from functools import partial

    client = workspace["client"]
    for _ in range(8):
        response = client.get("/v1/metrics", headers=workspace["headers"])
        assert response.status_code == 200, response.text
        metrics = response.json()
        if metrics["projection"]["status"] == "ready":
            return metrics
        client.portal.call(partial(client.app.state.service.run_jobs, queues=["ml"]))
    pytest.fail(f"Background metric projection did not finish: {metrics['projection']}")


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


def save_operating_policy(client, path, *, headers, json):
    """Explicit modern-client fixture, never an application or transport monkeypatch."""
    from uuid import uuid4

    version = client.get(path, headers=headers).json()["version"]
    return client.put(
        path, headers=headers, json={"request_id": str(uuid4()), "expected_version": version, **json}
    )
