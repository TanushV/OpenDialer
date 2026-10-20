"""The ChatGPT-plan OAuth adapter is removed; OpenAI API and compatible endpoints remain.

A store configured by an earlier release for plan usage degrades to unconfigured inference
with a named reason. It never crashes, never echoes the old tokens and never silently
switches to the metered API key that may also be stored.
"""

import pytest
from app.administration import capability_statuses

LEGACY_TOKEN = "legacy-plan-access-token-fixture"


def legacy_plan(workspace, app):
    service = app.state.service
    service.vault.write(
        workspace["tenant_id"], {"inference_mode": "chatgpt_plan", "openai_api_key": "metered-fixture"}
    )
    service.vault.write(
        "plan-" + workspace["user_id"], {"access_token": LEGACY_TOKEN, "refresh_token": LEGACY_TOKEN}
    )
    service.capability_cache.clear()
    return service


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/inference/chatgpt/start"),
        ("get", "/auth/callback?state=fixture&code=fixture"),
        ("post", "/v1/inference/chatgpt/import"),
        ("get", "/v1/inference/chatgpt/models"),
    ],
)
def test_plan_oauth_routes_are_gone(workspace, method, path):
    client, headers = workspace["client"], workspace["headers"]
    response = getattr(client, method)(path, headers=headers, **({"json": {}} if method == "post" else {}))
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("body", [{"inference_mode": "chatgpt_plan"}, {"chatgpt_access_token": "x"}])
def test_plan_settings_are_rejected(workspace, body):
    response = workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body)
    assert response.status_code == 422, response.text


def test_supported_routes_remain(workspace):
    for mode in ("api", "litellm"):
        response = workspace["client"].put(
            "/v1/admin/settings", headers=workspace["headers"], json={"inference_mode": mode}
        )
        assert response.status_code == 200, response.text
        assert response.json()["effective"]["inference_mode"] == mode


def test_legacy_plan_mode_degrades_to_unconfigured_inference(workspace, app):
    service = legacy_plan(workspace, app)
    client, headers = workspace["client"], workspace["headers"]
    for role in ("research_extract", "script_write", "scorecard_review"):
        assert service.inference(workspace["tenant_id"], role, workspace["user_id"]) is None

    response = client.get("/v1/admin/settings", headers=headers)
    assert response.status_code == 200, response.text
    settings = response.json()
    assert LEGACY_TOKEN not in response.text
    assert "chatgpt_access_token" not in settings["configured"]
    assert "inference_mode" not in settings
    assert settings["effective"]["inference_mode"] is None
    assert settings["retired"] == {"inference_mode": "chatgpt_plan"}

    preflight = client.get("/v1/admin/preflight", headers=headers)
    assert preflight.status_code == 200, preflight.text
    assert any("ChatGPT plan" in warning for warning in preflight.json()["warnings"])

    # Editing another setting keeps working and does not switch routes on the user's behalf.
    saved = client.put("/v1/admin/settings", headers=headers, json={"coach_model": "openai/gpt-oss-120b"})
    assert saved.status_code == 200, saved.text
    assert service.inference(workspace["tenant_id"], "script_write", workspace["user_id"]) is None

    chosen = client.put("/v1/admin/settings", headers=headers, json={"inference_mode": "api"})
    assert chosen.status_code == 200, chosen.text
    assert "retired" not in chosen.json()
    assert service.inference(workspace["tenant_id"], "script_write", workspace["user_id"]) is not None


def test_legacy_plan_mode_has_a_named_capability_reason():
    statuses = capability_statuses(
        {"inference_mode": "chatgpt_plan"}, {}, {}, simulation=False, source_version="fixture"
    )
    preparation = next(item for item in statuses if item["id"] == "preparation")
    assert preparation["state"] == "degraded" and preparation["blocks"] == "none"
    assert preparation["reason_codes"] == ["INFERENCE_MODE_RETIRED"]
    assert "OpenAI" in preparation["remedy"]
