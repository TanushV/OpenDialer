"""R3.7 to R3.9 and R3.11: the `local-2` store is copied to `local-3`, transformed, checked, and never modified.

The real-data test runs only when `OPENDIALER_REAL_STORE` points at a copy of the installed `local-2` folder; it is the
R3.8 evidence and never reads the installed folder itself.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from app.platform import migrate
from app.platform.config import Settings
from app.platform.db import RETIRED_TABLES, Database, put


def digest(folder):
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(folder).rglob("*"))
        if p.is_file()
    }


def make_source(folder):
    """A format-2 store: today's schema, one retired table with a row, a test call and a real call."""
    folder.mkdir()
    (folder / "config").mkdir()
    (folder / "config" / "setup.json").write_text('{"kept": true}')
    db = Database("sqlite:///" + str(folder / "opendialer.db"))
    with db.transaction() as s:
        test_lead = put(s, "leads", "t", "s", {"phone": "+12038211101", "name": "Owner"})
        real_lead = put(s, "leads", "t", "s", {"phone": "+16175550100", "name": "Prospect"})
        put(s, "call_attempts", "t", "s", {"lead_id": test_lead.id, "state": "COMPLETED"})
        put(
            s,
            "call_attempts",
            "t",
            "s",
            {"lead_id": real_lead.id, "disposition_input": {"delivery_test": True}, "state": "COMPLETED"},
        )
        put(s, "call_attempts", "t", "s", {"lead_id": real_lead.id, "state": "COMPLETED"})
    db.engine.dispose()
    with sqlite3.connect(folder / "opendialer.db") as raw:
        raw.execute("CREATE TABLE consent_evidence (id TEXT PRIMARY KEY, data TEXT)")
        raw.execute("INSERT INTO consent_evidence VALUES ('old', '{}')")
    return folder


def read(target, sql):
    with sqlite3.connect(target / "opendialer.db") as raw:
        return raw.execute(sql).fetchall()


def test_a_store_is_copied_transformed_and_the_source_is_untouched(tmp_path):
    source = make_source(tmp_path / "local-2")
    before = digest(source)
    target = tmp_path / "local-3"
    report = migrate.migrate(source, target)
    assert digest(source) == before
    assert (target / "config" / "setup.json").read_text() == '{"kept": true}'
    assert report["counts"]["leads"] == 2 and report["counts"]["call_attempts"] == 3
    tables = {row[0] for row in read(target, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & RETIRED_TABLES
    assert read(target, "PRAGMA user_version") == [(migrate.FORMAT,)]
    tests = sorted(
        row[0] for row in read(target, "SELECT json_extract(data, '$.is_test') FROM call_attempts")
    )
    assert tests == [0, 1, 1]
    for (data,) in read(target, "SELECT data FROM leads"):
        lead = json.loads(data)
        assert lead["source"] == "csv" and lead["external_id"] is None
        assert lead["signals"] == [] and lead["number_status"] == "not_checked"
    assert not (tmp_path / "local-3.partial").exists()
    # The migrated store opens in this version.
    Database("sqlite:///" + str(target / "opendialer.db")).engine.dispose()


def test_a_migrated_store_is_not_migrated_again(tmp_path):
    source = make_source(tmp_path / "local-2")
    target = tmp_path / "local-3"
    migrate.migrate(source, target)
    assert migrate.needed(source, target) is False
    assert migrate.needed(tmp_path / "missing", tmp_path / "fresh") is False
    assert migrate.needed(source, tmp_path / "fresh") is True


def test_a_failed_migration_leaves_no_store_and_says_so(tmp_path, monkeypatch):
    source = make_source(tmp_path / "local-2")
    before = digest(source)
    target = tmp_path / "local-3"
    target.mkdir()
    (target / "authority.lock").write_bytes(b"")

    def broken(connection):
        raise RuntimeError("disk full")

    monkeypatch.setattr(migrate, "transform", broken)
    with pytest.raises(migrate.MigrationFailed) as failure:
        migrate.migrate(source, target)
    assert "unchanged" in str(failure.value)
    assert digest(source) == before
    assert sorted(p.name for p in target.iterdir()) == ["authority.lock", migrate.FAILURE_NOTE]
    assert "disk full" in (target / migrate.FAILURE_NOTE).read_text()
    assert not (tmp_path / "local-3.partial").exists()
    # A later launch tries again from the untouched source.
    monkeypatch.undo()
    migrate.migrate(source, target)
    assert not (target / migrate.FAILURE_NOTE).exists()


def test_counts_that_do_not_match_fail_the_migration(tmp_path, monkeypatch):
    source = make_source(tmp_path / "local-2")
    original = migrate.transform

    def lossy(connection):
        original(connection)
        connection.execute("DELETE FROM leads WHERE rowid = (SELECT min(rowid) FROM leads)")

    monkeypatch.setattr(migrate, "transform", lossy)
    with pytest.raises(migrate.MigrationFailed, match="leads"):
        migrate.migrate(source, tmp_path / "local-3")


def test_the_backend_migrates_at_startup_and_refuses_on_failure(tmp_path, monkeypatch):
    from app import cli

    source = make_source(tmp_path / "local-2")
    target = tmp_path / "local-3"
    settings = Settings(data_dir=target, local_token="x" * 32)
    cli.prepare_store(settings, source)
    assert (target / "opendialer.db").exists()
    monkeypatch.setattr(migrate, "transform", lambda connection: (_ for _ in ()).throw(RuntimeError("bad")))
    other = Settings(data_dir=tmp_path / "other", local_token="x" * 32)
    with pytest.raises(migrate.MigrationFailed):
        cli.prepare_store(other, source)


@pytest.mark.slow
@pytest.mark.skipif(
    not os.getenv("OPENDIALER_REAL_STORE"), reason="needs a copy of the installed local-2 folder"
)
def test_r3_8_the_owners_store_migrates_with_every_count_intact(tmp_path):
    source = Path(os.environ["OPENDIALER_REAL_STORE"])
    before = digest(source)
    report = migrate.migrate(source, tmp_path / "local-3")
    assert digest(source) == before
    counts = report["counts"]
    assert (
        counts["leads"],
        counts["research_facts"],
        counts["script_versions"],
        counts["number_inventory"],
        counts["call_attempts"],
        counts["followup_tasks"],
    ) == (296, 823, 384, 6, 7, 13)
    assert report["test_calls"] == 4
