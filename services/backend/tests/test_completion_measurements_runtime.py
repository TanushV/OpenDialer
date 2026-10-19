"""Measurement harness uses actual indexed encrypted records and application paths."""

import hashlib
import hmac

from app.db import MODELS
from sqlalchemy import func, select
from test_control_completion import principal

from scripts import measure_completion as measure


def test_measurement_fixture_rebuilds_real_sources_and_counts_actual_requests(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    counts = measure.populate(service, p, workspace["campaign_id"], 8, 12, 20261018)
    assert counts == {"leads": 8, "call_attempts": 12}
    with service.db.transaction(False) as s:
        assert s.scalar(select(func.count()).select_from(MODELS["phone_endpoints"])) == 8
        endpoint = s.scalar(
            select(MODELS["phone_endpoints"]).where(
                MODELS["phone_endpoints"].id
                == str(measure.uuid5(measure.NAMESPACE_URL, "opendialer-measure:20261018:phone_endpoints:0"))
            )
        )
        key = hmac.new(service.db.identity_key, endpoint.data["phone"].encode(), hashlib.sha256).hexdigest()
        assert service._by_key(s, p, "phone_endpoints", key)
    rebuilt = measure.rebuild(service, p, timeout=20)
    assert rebuilt["published"]
    service.rebuild_home_metrics(p)
    service.rebuild_optimization_report(p)
    monkeypatch.setattr(measure, "WARMUPS", 1)
    monkeypatch.setattr(measure, "SAMPLES", 3)
    instrumentation = measure.Measurements(service)
    with instrumentation.instrument():
        result = measure.run_requests(
            service, p, workspace["campaign_id"], 1, instrumentation, lambda _: None
        )
    assert len(result["warmups"]) == 1 and len(result["samples"]) == 3
    assert all(r["ok"] and r["sql"] > 0 and r["decrypted"] > 0 for r in result["samples"])
    assert instrumentation.writes


def test_failed_transaction_is_not_recorded_as_success(workspace, app):
    import pytest

    measured = measure.Measurements(app.state.service)
    with measured.instrument(), pytest.raises(RuntimeError, match="synthetic rollback"):
        with app.state.service.db.transaction():
            raise RuntimeError("synthetic rollback")
    assert len(measured.writes) == 1
    assert measured.writes[0]["ok"] is False


def test_historical_profile_imports_the_preserved_public_fixture(workspace, app):
    result = measure.populate_historical(workspace["client"], workspace["headers"], workspace["campaign_id"])
    assert result == {"leads": 503, "call_attempts": 0}
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        records = service.scoped(s, "leads", p)
        imported = [r for r in records if r.data.get("name", "").startswith("Load ")]
        assert len(imported) == 500
        assert all(r.data["timezone"] == "America/Los_Angeles" for r in imported)
        assert all(r.data["company"] == "Load fixture" for r in imported)
