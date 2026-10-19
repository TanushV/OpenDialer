"""E12/C048: configured is distinct from verified and every missing capability is actionable."""

import pytest
from uuid import uuid4


def test_versioned_provider_edit_replays_without_overwriting_newer_config(workspace):
    c, h = workspace["client"], workspace["headers"]
    first = {"request_id": str(uuid4()), "expected_version": 0,
             "openrouter_api_key": "synthetic-first-secret"}
    saved = c.put("/v1/admin/settings", headers=h, json=first)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1 and "synthetic-first-secret" not in saved.text
    second = c.put("/v1/admin/settings", headers=h, json={
        "request_id": str(uuid4()), "expected_version": 1, "coach_provider": "static"})
    assert second.status_code == 200
    replay = c.put("/v1/admin/settings", headers=h, json=first)
    assert replay.status_code == 200 and replay.json()["version"] == 2
    assert replay.json()["coach_provider"] == "static"
    assert "request_id" not in replay.text and "synthetic-first-secret" not in replay.text
    conflict = c.put("/v1/admin/settings", headers=h, json={**first, "openrouter_api_key": "other-secret"})
    assert conflict.status_code == 409
    stale = c.put("/v1/admin/settings", headers=h, json={**first, "request_id": str(uuid4())})
    assert stale.status_code == 409


def test_provider_edit_requires_identity_and_version_together(workspace):
    c, h = workspace["client"], workspace["headers"]
    for body in [{"expected_version": 0}, {"request_id": str(uuid4())}]:
        assert c.put("/v1/admin/settings", headers=h, json=body).status_code == 422


def test_settings_serialize_domain_defaults_and_never_secret_values(workspace):
    from app.config import RuntimeLimits
    from app.compliance.operating import OperatingPolicy
    from app.preparation.templates import PreparationPolicy

    c, h = workspace["client"], workspace["headers"]
    secret = "synthetic-provider-secret-no-real-credential"
    saved = c.put("/v1/admin/settings", headers=h, json={"openrouter_api_key": secret})
    assert saved.status_code == 200
    value = c.get("/v1/admin/settings", headers=h).json()
    assert secret not in str(value)
    assert value.get("version") == 1
    assert value.get("defaults") == {
        "runtime": RuntimeLimits().model_dump(mode="json"),
        "operating": OperatingPolicy().model_dump(mode="json"),
        "preparation": PreparationPolicy().model_dump(mode="json"),
    }


def test_lightweight_readiness_reuses_saved_no_charge_capabilities(workspace, app):
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    full = service.preflight(p)
    small = service.preflight(p, full=False)
    assert small.get("capabilities") == full.get("capabilities") and small.get("capabilities")
    assert small.get("capabilities_as_of") == full.get("capabilities_as_of")


def test_capability_cache_is_bounded_across_sellers(workspace, app):
    from test_remediation_privacy import principal

    service = app.state.service
    service.capability_cache.update({("fixture", str(i)): {} for i in range(128)})
    service.preflight(principal(workspace))
    assert len(service.capability_cache) <= 128


@pytest.mark.parametrize("simulation", [True, False])
def test_configured_vs_verified_capabilities(workspace, app, carrier, monkeypatch, simulation):
    monkeypatch.setattr(app.state.service.settings, "simulation", simulation)
    c, h = workspace["client"], workspace["headers"]
    first = c.get("/v1/admin/preflight", headers=h)
    assert first.status_code == 200, first.text
    capabilities = first.json().get("capabilities", [])
    assert capabilities, "Preflight must expose per-capability evidence instead of one broad readiness flag"
    by_id = {item["id"]: item for item in capabilities}
    assert {
        "telephony",
        "transcription",
        "live_helper",
        "national_dnc",
        "local_ingress",
        "offline_callback",
    } <= by_id.keys()
    for item in capabilities:
        assert item["state"] in {
            "unconfigured",
            "configured",
            "software_verified",
            "live_verified",
            "degraded",
            "unavailable",
        }
        assert item["state"] != "live_verified", (
            "Synthetic evidence or an absent key cannot certify a live provider"
        )
        assert item.get("source_version") is not None
        if item["state"] in {"unconfigured", "degraded", "unavailable"}:
            assert item["reason_codes"] and item["remedy"]
    if not simulation:
        assert not first.json()["ready_to_call"]
        assert by_id["offline_callback"]["state"] == "unavailable"
    assert c.get("/v1/admin/preflight", headers=h).status_code == 200
    assert not carrier.dials and not carrier.actions and not carrier.purchases
