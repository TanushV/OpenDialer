import pytest
from app.config import Settings
from app.main import create_app
from app.providers.simulated import SimulatedCarrier
from fastapi.testclient import TestClient


@pytest.fixture
def connected(tmp_path, clock, app):
    settings = Settings(
        mode="connected",
        simulation=True,
        database_url=(
            app.state.service.settings.database_url
            if not app.state.db.sqlite
            else f"sqlite:///{tmp_path}/connected-test.db"
        ),
        data_dir=tmp_path,
        secret_key="a" * 64,
        bootstrap_token="bootstrap-123456789",
        worker_enabled=False,
    )
    with TestClient(create_app(settings, clock=clock, carrier=SimulatedCarrier())) as c:
        response = c.post(
            "/v1/setup",
            headers={"X-Bootstrap-Token": "bootstrap-123456789"},
            json={
                "seller_name": "Seller One",
                "campaign_name": "Team campaign",
                "email": "one@example.test",
                "password": "Strong-test-password-123",
            },
        )
        assert response.status_code == 200, response.text
        one = response.json()
        h = {"Authorization": f"Bearer {one['access_token']}"}
        c.post("/v1/simulation/seed", headers=h)
        yield c, one, h


def test_connected_never_grants_loopback_auth_bypass(connected):
    c, _, _ = connected
    assert c.post("/v1/local/session", headers={"X-Local-Token": "anything"}).status_code == 403
    assert c.get("/v1/workspace").status_code == 401
    assert (
        c.post(
            "/v1/setup",
            headers={"X-Bootstrap-Token": "bootstrap-123456789"},
            json={"seller_name": "Attacker", "email": "two@example.test", "password": "another-password-123"},
        ).status_code
        == 409
    )


def test_login_logout_revocation_and_no_default_password(connected):
    c, one, h = connected
    assert (
        c.post("/v1/auth/session", json={"email": "one@example.test", "password": "wrong"}).status_code == 401
    )
    login = c.post(
        "/v1/auth/session", json={"email": "one@example.test", "password": "Strong-test-password-123"}
    )
    assert login.status_code == 200
    assert c.post("/v1/auth/logout", headers=h).status_code == 200
    assert c.get("/v1/workspace", headers=h).status_code == 401


def test_rep_cannot_publish_policy_see_keys_or_promote_model(connected):
    c, _, owner_headers = connected
    user = c.post(
        "/v1/admin/users",
        headers=owner_headers,
        json={"email": "rep@example.test", "name": "Rep", "role": "rep", "password": "rep-test-password-123"},
    )
    assert user.status_code == 200
    token = c.post(
        "/v1/auth/session", json={"email": "rep@example.test", "password": "rep-test-password-123"}
    ).json()["access_token"]
    rep_headers = {"Authorization": f"Bearer {token}"}
    workspace = c.get("/v1/workspace", headers=rep_headers).json()
    assert (
        c.post(
            f"/v1/admin/policies/{workspace['policy']['id']}/publish",
            headers=rep_headers,
            json={"approved": True},
        ).status_code
        == 403
    )
    assert c.get("/v1/admin/settings", headers=rep_headers).status_code == 403
    assert c.post("/v1/admin/models/model-id/activate", headers=rep_headers, json={}).status_code == 403


def test_tenant_isolation_on_reads_writes_events_and_phone_suppression(connected):
    c, one, h1 = connected
    other = c.post(
        "/v1/admin/workspaces",
        headers=h1,
        json={
            "seller_name": "Seller Two",
            "campaign_name": "Other campaign",
            "email": "two@example.test",
            "password": "Second-strong-password-123",
        },
    )
    assert other.status_code == 200, other.text
    h2 = {"Authorization": f"Bearer {other.json()['access_token']}"}
    c.post("/v1/simulation/seed", headers=h2)
    a, b = c.get("/v1/workspace", headers=h1).json(), c.get("/v1/workspace", headers=h2).json()
    foreign_lead = b["leads"][0]["id"]
    assert c.get(f"/v1/leads/{foreign_lead}/packet", headers=h1).status_code == 404
    assert (
        c.patch(
            f"/v1/admin/numbers/{b['numbers'][0]['id']}",
            headers=h1,
            json={"lifecycle": "paused_for_outbound", "reason": "cross-tenant"},
        ).status_code
        == 404
    )
    c.post("/v1/suppressions", headers=h1, json={"phone": a["leads"][0]["phone"], "reason": "opt_out"})
    assert not c.get("/v1/workspace", headers=h2).json()["leads"][0]["suppressed"]
    events = c.get("/v1/events", headers=h1).json()["events"]
    assert all(e["tenant_id"] == one["tenant_id"] for e in events)


def test_master_keys_are_write_only_and_not_in_snapshot(connected):
    c, _, h = connected
    secret = "sk-test-secret-should-never-echo"
    r = c.put(
        "/v1/admin/settings",
        headers=h,
        json={"telnyx_api_key": secret, "openai_api_key": secret, "groq_api_key": secret},
    )
    assert r.status_code == 200, r.text
    assert secret not in r.text
    for path in ["/v1/admin/settings", "/v1/workspace", "/v1/admin/preflight", "/health"]:
        assert secret not in c.get(path, headers=h).text


def test_live_mode_without_provider_keys_is_fail_closed(tmp_path, clock):
    settings = Settings(
        mode="local",
        simulation=False,
        database_url=f"sqlite:///{tmp_path}/live.db",
        data_dir=tmp_path,
        local_token="local-test",
        secret_key="x" * 64,
        worker_enabled=False,
    )
    with TestClient(create_app(settings, clock=clock)) as c:
        initial = c.post(
            "/v1/setup",
            headers={"X-Local-Token": "local-test"},
            json={
                "seller_name": "Live seller",
                "campaign_name": "Live",
                "email": "live@example.test",
                "password": "live-password-123",
            },
        )
        h = {"Authorization": f"Bearer {initial.json()['access_token']}"}
        assert c.post("/v1/simulation/seed", headers=h).status_code == 403
        assert c.post("/v1/telephony/token", headers=h).status_code == 503
        preflight = c.get("/v1/admin/preflight", headers=h).json()
        assert preflight["ready_to_call"] is False
        assert "TELNYX_NOT_CONFIGURED" in preflight["blockers"]
