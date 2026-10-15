from pathlib import Path

import pytest
from app.db import Database


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
