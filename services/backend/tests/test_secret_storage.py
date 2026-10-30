"""Credential confidentiality, upgrade preservation and HTTP boundary regressions."""

import json
import os
import sys
from uuid import uuid4

import pytest
from app.security import ConfigStore


def test_settings_are_encrypted_and_survive_restart(tmp_path):
    store = ConfigStore(tmp_path)
    store.write("tenant", {"telnyx_api_key": "private-carrier-fixture", "version": 3})
    path = tmp_path / "config/tenant.json"
    assert b"private-carrier-fixture" not in path.read_bytes()
    assert ConfigStore(tmp_path).read("tenant") == {"telnyx_api_key": "private-carrier-fixture", "version": 3}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_upgrade_preserves_and_encrypts_all_old_secret_files(tmp_path):
    paths = [
        "config/tenant.json",
        "codex/person/auth.json",
        "managed-session.json",
        "managed-connection.json",
    ]
    value = {"credential": "old-secret-fixture", "version": 17}
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    ConfigStore(tmp_path)
    for name in paths:
        assert b"old-secret-fixture" not in (tmp_path / name).read_bytes()
    assert ConfigStore(tmp_path).read("tenant") == value


async def test_managed_refresh_session_is_encrypted(tmp_path):
    from app.managed_webhooks import SessionStorage

    path = tmp_path / "managed-session.json"
    await SessionStorage(path).set_item("session", "private-refresh-fixture")
    assert b"private-refresh-fixture" not in path.read_bytes()
    assert await SessionStorage(path).get_item("session") == "private-refresh-fixture"


def test_codex_persistent_login_is_encrypted(tmp_path):
    from app.providers.codex import save_login

    save_login(tmp_path, {"tokens": {"refresh_token": "private-codex-fixture"}})
    assert b"private-codex-fixture" not in (tmp_path / "auth.json").read_bytes()


def test_settings_never_echo_secrets_and_reject_foreign_origins(workspace):
    client, headers = workspace["client"], workspace["headers"]
    url = "/v1/admin/settings"
    saved = client.put(url, headers=headers, json={"telnyx_api_key": "private-api-fixture"})
    assert saved.status_code == 200
    assert "private-api-fixture" not in saved.text
    rejected = client.put(
        url,
        headers={**headers, "Origin": "https://untrusted.example"},
        json={"telnyx_api_key": "replacement"},
    )
    assert rejected.status_code == 403
    assert (
        client.app.state.service.configs.read(workspace["tenant_id"])["telnyx_api_key"]
        == "private-api-fixture"
    )


def test_validation_errors_do_not_echo_credentials(workspace):
    response = workspace["client"].put(
        "/v1/admin/settings",
        headers=workspace["headers"],
        json={"telnyx_api_key": {"secret": "private-invalid-fixture"}},
    )
    assert response.status_code == 422
    assert "private-invalid-fixture" not in response.text


def test_password_guessing_is_throttled(client):
    for _ in range(10):
        assert (
            client.post(
                "/v1/auth/session", json={"email": "nobody@example.test", "password": "invalid-password"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/v1/auth/session", json={"email": "nobody@example.test", "password": "invalid-password"}
        ).status_code
        == 429
    )


def test_successful_signins_do_not_exhaust_the_failed_login_limit(workspace):
    client = workspace["client"]
    for _ in range(12):
        response = client.post(
            "/v1/auth/session",
            json={"email": "operator@example.test", "password": "a-test-only-password-123"},
        )
        assert response.status_code == 200


def test_successful_other_account_cannot_clear_a_failed_account_limit(workspace):
    client = workspace["client"]
    invalid = {"email": "nobody@example.test", "password": "invalid-password"}
    for _ in range(9):
        assert client.post("/v1/auth/session", json=invalid).status_code == 401
    assert (
        client.post(
            "/v1/auth/session",
            json={"email": "operator@example.test", "password": "a-test-only-password-123"},
        ).status_code
        == 200
    )
    assert client.post("/v1/auth/session", json=invalid).status_code == 401
    assert client.post("/v1/auth/session", json=invalid).status_code == 429


def test_lost_key_and_modified_ciphertext_never_overwrite_saved_credentials(tmp_path, monkeypatch):
    from app.secret_files import SecretStorageError, read_private, write_private
    from cryptography.fernet import Fernet

    path = tmp_path / "secret.json"
    write_private(path, {"key": "preserve-this"})
    original = path.read_bytes()
    monkeypatch.setenv("OPENDIALER_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(SecretStorageError):
        read_private(path)
    assert path.read_bytes() == original
    path.write_bytes(original[:-10] + b"tampered!!")
    with pytest.raises(SecretStorageError):
        read_private(path)
    assert path.read_bytes() == original[:-10] + b"tampered!!"


def test_unavailable_key_store_never_converts_plaintext_or_falls_back(tmp_path, monkeypatch):
    from app import secret_files

    path = tmp_path / "secret.json"
    path.write_text('{"key":"old-private-value"}')
    monkeypatch.delenv("OPENDIALER_SECRET_KEY")

    def unavailable():
        raise RuntimeError("private-provider-error")

    monkeypatch.setattr(secret_files, "native_key", unavailable)
    with pytest.raises(secret_files.SecretStorageError) as error:
        secret_files.read_private(path)
    assert "private-provider-error" not in str(error.value)
    assert path.read_text() == '{"key":"old-private-value"}'


@pytest.mark.skipif(
    not os.getenv("OPENDIALER_TEST_NATIVE_SECRETS") or sys.platform not in {"darwin", "win32"},
    reason="Explicit disposable native credential-store test",
)
def test_native_credential_store_roundtrip(tmp_path, monkeypatch):
    from app import secret_files

    service = "OpenDialer-test-" + str(uuid4())
    backend = secret_files.native_backend()
    monkeypatch.delenv("OPENDIALER_SECRET_KEY")
    monkeypatch.setattr(secret_files, "KEY_SERVICE", service)
    secret_files.native_key.cache_clear()
    try:
        path = tmp_path / "credentials.json"
        secret_files.write_private(path, {"secret": "disposable-native-fixture"})
        secret_files.native_key.cache_clear()
        assert secret_files.read_private(path) == {"secret": "disposable-native-fixture"}
        assert b"disposable-native-fixture" not in path.read_bytes()
    finally:
        secret_files.native_key.cache_clear()
        if backend.get_password(service, secret_files.KEY_ACCOUNT) is not None:
            backend.delete_password(service, secret_files.KEY_ACCOUNT)
