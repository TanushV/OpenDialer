from pathlib import Path

import pytest
from app.db import Database


def test_independent_provider_writers_preserve_each_others_fields(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from app.security import Vault

    writers = [Vault(tmp_path, "synthetic-vault-secret") for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda i: writers[i % 4].write("tenant", {f"field{i}": str(i)}), range(40)))
    assert writers[0].read("tenant") == {f"field{i}": str(i) for i in range(40)}


def test_provider_source_fence_does_not_publish_over_a_new_config(tmp_path):
    from app.security import Vault
    from fastapi import HTTPException

    vault = Vault(tmp_path, "synthetic-vault-secret")
    vault.write("tenant", {"version": 2, "webhook_url": "https://new.example.test/webhooks/telnyx"})
    with pytest.raises(HTTPException) as error:
        vault.write("tenant", {"ingress_evidence": {"routing": "software_verified"}}, expected={"version": 1})
    assert error.value.status_code == 409
    assert "ingress_evidence" not in vault.read("tenant")


def test_persistent_database_and_wal_do_not_expose_phone_email_or_notes(workspace):
    app = workspace["client"].app
    app.state.db.engine.dispose()
    path = Path(app.state.db.url.removeprefix("sqlite:///"))
    if not path.exists():
        pytest.skip("Byte-level storage check applies to SQLite; PostgreSQL uses the same encrypted codec")
    blobs = b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*") if p.is_file())
    assert b"+14155550101" not in blobs
    assert b"operator@example.test" not in blobs
    assert b"Northstar Example" not in blobs


def test_database_encryption_key_is_required_and_wrong_key_cannot_decode(workspace):
    db = workspace["client"].app.state.db
    with pytest.raises(ValueError):
        Database(db.url, encryption_key="incorrect-key-for-this-store")
