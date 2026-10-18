"""Configuration is one source of truth and invalid authority data cannot be saved."""

import pytest


@pytest.mark.parametrize(
    "change",
    [
        {"npas": ["abc"]},
        {"npas": ["000"]},
        {"expires_at": "yesterday"},
        {"expires_at": "2027-01-01T00:00:00"},
        {"source_refreshed_at": "not a date"},
        {"reference": "   "},
    ],
)
def test_dnc_settings_validate_source_shape_before_saving(workspace, change):
    auth = {
        "reference": "seller-access",
        "npas": ["212"],
        "expires_at": "2027-01-01T00:00:00Z",
        "source_refreshed_at": "2026-10-14T00:00:00Z",
        "source_access_verified": True,
        **change,
    }
    r = workspace["client"].put(
        "/v1/admin/settings", headers=workspace["headers"], json={"dnc_seller_authorization": auth}
    )
    assert r.status_code == 422


def test_partial_privacy_update_uses_existing_evidence(workspace):
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.put(
            "/v1/admin/operating-policy",
            headers=h,
            json={"transcription_privacy_reference": "account-reviewed-terms"},
        ).status_code
        == 200
    )
    r = c.put("/v1/admin/operating-policy", headers=h, json={"transcription_privacy_verified": True})
    assert r.status_code == 200, r.text
    assert r.json()["transcription_privacy_reference"] == "account-reviewed-terms"
    assert (
        c.put(
            "/v1/admin/operating-policy", headers=h, json={"transcription_privacy_reference": ""}
        ).status_code
        == 422
    )


def test_legacy_operating_coach_setting_cannot_override_the_actual_provider(workspace):
    c, h = workspace["client"], workspace["headers"]
    assert c.put("/v1/admin/settings", headers=h, json={"coach_provider": "static"}).status_code == 200
    # Provider settings are the sole authority: a second live switch is misleading.
    assert c.get("/v1/admin/operating-policy", headers=h).json()["coach_provider"] == "static"
    assert (
        c.put("/v1/admin/operating-policy", headers=h, json={"coach_provider": "openrouter"}).status_code
        == 422
    )
