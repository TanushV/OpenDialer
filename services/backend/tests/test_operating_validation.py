"""Configuration is one source of truth and invalid authority data cannot be saved."""

import pytest


def test_runtime_limits_have_one_validated_nested_configuration(monkeypatch):
    from app.config import Settings

    defaults = Settings().runtime_limits
    assert (defaults.live_active, defaults.live_pending, defaults.live_per_rep) == (8, 32, 2)
    assert (defaults.prospect_debounce_ms, defaults.automatic_per_call_minute) == (300, 30)
    assert (defaults.projection_batch, defaults.rebuild_batch) == (100, 500)
    assert defaults.registry_lru_bytes == 16777216
    monkeypatch.setenv("OPENDIALER_RUNTIME_LIMITS__LIVE_ACTIVE", "4")
    assert Settings().runtime_limits.live_active == 4
    with pytest.raises(ValueError):
        defaults.live_active = 2


@pytest.mark.parametrize(
    "override",
    [
        {"live_active": 0},
        {"live_active": 33},
        {"live_pending": 257},
        {"prospect_debounce_ms": 501},
        {"automatic_per_call_minute": 61},
        {"manual_connect_ms": 3001, "manual_first_ms": 3000},
        {"manual_first_ms": 8001, "manual_full_ms": 8000},
        {"automatic_deadline_ms": 5001},
        {"conversation_bytes": 262145},
        {"research_pages": 4},
        {"planning_operations": 200001},
        {"projection_batch": 101},
        {"rebuild_batch": 501},
        {"registry_lru_bytes": 16777217},
        {"invented_limit": 1},
    ],
)
def test_invalid_runtime_limits_fail_startup(override):
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(runtime_limits=override)


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
