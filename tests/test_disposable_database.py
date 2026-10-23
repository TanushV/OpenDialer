"""E00: a test typo must never reset an installed or user database."""

import os

import pytest

from scripts.disposable_database import validate_database_url

pytestmark = pytest.mark.database

LOCAL = "postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/"


@pytest.mark.parametrize(
    "purpose,name", [("unit", "opendialer_remediation_test"), ("browser", "opendialer_completion_e2e")]
)
def test_exact_disposable_identity_is_admitted(purpose, name):
    assert validate_database_url(LOCAL + name, purpose=purpose) == LOCAL + name


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:////Users/example/Library/Application Support/OpenDialer/local.db",
        LOCAL + "production",
        LOCAL + "opendialer_completion_e2e",
        LOCAL.replace("127.0.0.1", "example.com") + "opendialer_remediation_test",
        LOCAL.replace("55439", "54322") + "opendialer_remediation_test",
        LOCAL.replace("remediation-disposable-test", "real-secret") + "opendialer_remediation_test",
        LOCAL + "opendialer_remediation_test?options=-csearch_path%3Dproduction",
        "not-a-database",
    ],
)
def test_other_databases_are_refused_without_credentials_in_error(url):
    with pytest.raises(ValueError) as error:
        validate_database_url(url)
    assert "real-secret" not in str(error.value)


def test_ci_identity_requires_actual_ci_and_distinct_browser_database(monkeypatch):
    url = "postgresql+psycopg://postgres:disposable_ci_test@localhost:5432/opendialer_test"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError):
        validate_database_url(url)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert validate_database_url(url) == url
    with pytest.raises(ValueError):
        validate_database_url(url, purpose="browser")


def test_unit_and_browser_stores_cannot_alias():
    with pytest.raises(ValueError):
        validate_database_url(LOCAL + "opendialer_remediation_test", purpose="browser")


def test_reset_refuses_foreign_store_before_opening_connection(monkeypatch):
    import app.db  # noqa: F401 - bind the real engine factory before it is patched below
    import sqlalchemy

    from scripts.disposable_database import reset_database

    opened = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: opened.append(a))
    with pytest.raises(ValueError):
        reset_database(LOCAL + "production")
    assert opened == []


@pytest.mark.parametrize("name", ["opendialer_remediation_test_gw0", "opendialer_remediation_test_gw17"])
def test_local_xdist_worker_stores_are_admitted_for_unit_tests_only(name):
    assert validate_database_url(LOCAL + name) == LOCAL + name
    for purpose in ("browser", "performance"):
        with pytest.raises(ValueError):
            validate_database_url(LOCAL + name, purpose=purpose)


@pytest.mark.parametrize(
    "name",
    [
        "opendialer_remediation_test_",
        "opendialer_remediation_test_gw",
        "opendialer_remediation_test_gwx",
        "opendialer_remediation_test_gw1_gw2",
        "opendialer_remediation_test_gw1_production",
        "opendialer_remediation_test_prod",
        "opendialer_remediation_testgw1",
        "opendialer_remediation_test_gw١",
        "opendialer_remediation_test_gw1\n",
        "xopendialer_remediation_test_gw1",
        "opendialer_test_gw1",
    ],
)
def test_near_miss_worker_names_are_refused(name, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError):
        validate_database_url(LOCAL + name)


def test_ci_worker_stores_require_actual_ci_and_exact_shape(monkeypatch):
    base = "postgresql+psycopg://postgres:disposable_ci_test@localhost:5432/"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError):
        validate_database_url(base + "opendialer_test_gw3")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert validate_database_url(base + "opendialer_test_gw3") == base + "opendialer_test_gw3"
    for name in ("opendialer_test_gw", "opendialer_test_gw3_x", "opendialer_remediation_test_gw3"):
        with pytest.raises(ValueError):
            validate_database_url(base + name)


def test_worker_store_derivation_is_guarded(monkeypatch):
    from scripts.disposable_database import worker_database_url

    base = LOCAL + "opendialer_remediation_test"
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert worker_database_url(base, worker="") == base
    assert worker_database_url(base, worker="gw4") == base + "_gw4"
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw9")
    assert worker_database_url(base) == base + "_gw9"
    for worker in ("master", "gw", "gw1;drop", "../gw1"):
        with pytest.raises(ValueError):
            worker_database_url(base, worker=worker)
    with pytest.raises(ValueError):
        worker_database_url(base + "_gw1", worker="gw2")
    with pytest.raises(ValueError):
        worker_database_url(LOCAL + "production", worker="gw1")


def test_worker_store_lifecycle_refuses_foreign_or_base_stores_before_connecting(monkeypatch):
    import psycopg

    from scripts.disposable_database import drop_worker_database, ensure_database

    opened = []
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: opened.append(a))
    for operation in (ensure_database, drop_worker_database):
        with pytest.raises(ValueError):
            operation(LOCAL + "production_gw1")
    with pytest.raises(ValueError):
        drop_worker_database(LOCAL + "opendialer_remediation_test")
    ensure_database(LOCAL + "opendialer_remediation_test")  # The base store is provisioned externally.
    assert opened == []


def test_store_reset_refuses_foreign_store_before_opening_connection(monkeypatch):
    import sqlalchemy

    from scripts.disposable_database import PostgresTestStore

    opened = []
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: opened.append(a))
    with pytest.raises(ValueError):
        PostgresTestStore(LOCAL + "opendialer_completion_e2e")
    assert opened == []


def _store_contents(url):
    from sqlalchemy import MetaData, create_engine, select, text

    engine = create_engine(url)
    try:
        metadata = MetaData()
        with engine.connect() as connection:
            metadata.reflect(connection)
            rows = {
                table.name: sorted(repr(tuple(row)) for row in connection.execute(select(table)))
                for table in metadata.sorted_tables
            }
            sequences = connection.execute(
                text("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'")
            ).all()
        return rows, sorted(sequences)
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not os.getenv("OPENDIALER_TEST_POSTGRES_URL"), reason="Requires the guarded store")
def test_cheap_store_reset_restores_the_exact_post_startup_state(tmp_path):
    from app.config import Settings
    from app.main import create_app
    from app.providers.simulated import SimulatedCarrier
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from scripts.disposable_database import (
        PostgresTestStore,
        drop_worker_database,
        ensure_database,
        worker_database_url,
    )

    url = worker_database_url(validate_database_url(os.environ["OPENDIALER_TEST_POSTGRES_URL"]), "gw9999")
    settings = Settings(
        database_url=url,
        mode="local",
        local_token="reset-local-secret",
        simulation=True,
        data_dir=tmp_path / "runtime",
        worker_enabled=False,
        bootstrap_token="reset-bootstrap-secret",
    )

    def initialize(value):
        create_app(settings.model_copy(update={"data_dir": tmp_path / "startup"})).state.db.engine.dispose()

    ensure_database(url)
    store = PostgresTestStore(url)
    try:
        store.reset(initialize)
        started = _store_contents(url)
        assert store.rebuilds == 1
        app = create_app(settings, carrier=SimulatedCarrier())
        with TestClient(app) as client:
            created = client.post(
                "/v1/setup",
                headers={"X-Local-Token": "reset-local-secret"},
                json={
                    "seller_name": "Reset Seller",
                    "campaign_name": "Reset campaign",
                    "email": "reset@example.test",
                    "password": "a-test-only-password-123",
                },
            )
            assert created.status_code == 200, created.text
            token = created.json()["access_token"]
            seeded = client.post("/v1/simulation/seed", headers={"Authorization": f"Bearer {token}"})
            assert seeded.status_code == 200, seeded.text
        app.state.db.engine.dispose()
        assert _store_contents(url) != started
        store.reset(initialize)
        assert store.rebuilds == 1, "An unchanged schema is reset without a rebuild"
        assert _store_contents(url) == started
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenants ADD COLUMN reset_probe integer"))
        engine.dispose()
        store.reset(initialize)
        assert store.rebuilds == 2, "A changed schema is rebuilt, never reset in place"
        assert "reset_probe" not in repr(_store_contents(url))
    finally:
        store.dispose()
        drop_worker_database(url)
