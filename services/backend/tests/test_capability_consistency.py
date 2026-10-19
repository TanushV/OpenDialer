"""E12/C048: configured is distinct from verified and every missing capability is actionable."""

import pytest


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
