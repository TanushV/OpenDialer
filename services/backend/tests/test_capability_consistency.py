"""E12/C048: configured is distinct from verified and every missing capability is actionable."""

import pytest


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
