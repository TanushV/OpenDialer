"""E11/E14: additive legacy-schema contract and installed acceptance receipt validation."""

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path

import pytest
from app.db import get, put
from app.migrate import migrate_database
from conftest import start
from sqlalchemy import inspect, text
from test_remediation_privacy import RAW, persisted_content, principal


def legacy_upgrade_store(tmp_path):
    from app.config import Settings
    from app.db import Database

    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite:///" + str(tmp_path / "legacy.db"),
        secret_key="synthetic-upgrade",
        local_token="synthetic-token",
        simulation=True,
        worker_enabled=False,
    )
    db = Database(settings.database_url, encryption_key=settings.secret_key)
    with db.transaction() as s:
        for i in range(603):
            put(
                s,
                "transcript_segments",
                "tenant",
                "seller",
                {"segment_id": str(i), "text": RAW},
                object_id=f"source-{i:04}",
            )
        put(
            s, "notes", "tenant", "seller", {"human_text": "Retain this relationship note"}, object_id="human"
        )
    db.engine.dispose()
    return settings


def test_interrupted_upgrade_keeps_original_and_resumes_only_the_working_copy(tmp_path, monkeypatch):
    import sqlite3

    import app.privacy as privacy
    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    with closing(sqlite3.connect(tmp_path / "legacy.db")) as original:
        before = list(original.iterdump())
    sanitize, seen = privacy.persistent_document, []

    def interrupted(kind, data, **kwargs):
        if kind == "transcript_segments":
            seen.append(data.get("segment_id"))
            if len(seen) == 501:
                raise OSError("Synthetic upgrade interrupted before publication")
        return sanitize(kind, data, **kwargs)

    monkeypatch.setattr(privacy, "persistent_document", interrupted)
    with pytest.raises(OSError, match="before publication"):
        create_app(settings)
    with closing(sqlite3.connect(tmp_path / "legacy.db")) as original:
        assert list(original.iterdump()) == before, (
            "Interrupted validation cannot partly sanitize the original"
        )
    seen.clear()
    restored = create_app(settings)
    assert len(seen) == 103, "The protected copy resumes its500-row sanitation cursor"
    with restored.state.db.transaction(False) as s:
        from app.db import MODELS
        from sqlalchemy import select

        assert all("text" not in row.data for row in s.scalars(select(MODELS["transcript_segments"])))
        assert get(s, "notes", "human").data["human_text"] == "Retain this relationship note"
    restored.state.db.engine.dispose()
    assert not (tmp_path / ".opendialer-upgrade-v1").exists()


def test_sqlite_upgrade_publication_failure_rolls_back_in_place(tmp_path, monkeypatch):
    import sqlite3

    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    source_path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(source_path)) as original:
        before, inode = list(original.iterdump()), source_path.stat().st_ino
    original_connect = sqlite3.connect

    class InterruptedBackup(sqlite3.Connection):
        def backup(self, target, **kwargs):
            if Path(target.execute("PRAGMA database_list").fetchone()[2]) == source_path:

                def interrupted(status, remaining, total):
                    if remaining:
                        raise OSError("Synthetic publication interruption")

                kwargs.update(pages=1, progress=interrupted)
            return super().backup(target, **kwargs)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: original_connect(*a, **{**kw, "factory": InterruptedBackup})
    )
    with pytest.raises(OSError, match="publication interruption"):
        create_app(settings)
    with closing(original_connect(source_path)) as original:
        assert list(original.iterdump()) == before
    assert source_path.stat().st_ino == inode, "Publication must use SQLite, not rename an open WAL file"
    monkeypatch.setattr(sqlite3, "connect", original_connect)
    resumed = create_app(settings)
    resumed.state.db.engine.dispose()
    assert source_path.stat().st_ino == inode


def test_current_startup_does_not_repeat_history_authentication(tmp_path, monkeypatch):
    import app.db as storage
    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    upgraded = create_app(settings)
    upgraded.state.db.engine.dispose()

    def forbidden(*args, **kwargs):
        pytest.fail("An authenticated current startup must not scan all historical payloads")

    monkeypatch.setattr(storage, "authenticate_store", forbidden)
    reopened = create_app(settings)
    reopened.state.db.engine.dispose()


def test_upgrade_refuses_unowned_staging_content(tmp_path):
    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    stage = tmp_path / ".opendialer-upgrade-v1"
    stage.mkdir()
    unrelated = stage / "do-not-remove.txt"
    unrelated.write_text("User-owned diagnostic")
    with pytest.raises(ValueError, match="[Ss]taging"):
        create_app(settings)
    assert unrelated.read_text() == "User-owned diagnostic"


def test_upgrade_restarts_copy_if_original_was_edited_after_interruption(tmp_path, monkeypatch):
    import app.privacy as privacy
    from app.db import Database, update
    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    original, seen = privacy.persistent_document, []

    def interrupt(kind, data, **kwargs):
        if kind == "transcript_segments":
            seen.append(data.get("segment_id"))
            if len(seen) == 501:
                raise OSError("Synthetic interruption")
        return original(kind, data, **kwargs)

    monkeypatch.setattr(privacy, "persistent_document", interrupt)
    with pytest.raises(OSError):
        create_app(settings)
    db = Database(settings.database_url, encryption_key=settings.secret_key)
    with db.transaction() as session:
        update(get(session, "notes", "human"), human_text="A newer human correction must survive")
    db.engine.dispose()
    monkeypatch.setattr(privacy, "persistent_document", original)
    upgraded = create_app(settings)
    with upgraded.state.db.transaction(False) as session:
        assert get(session, "notes", "human").data["human_text"] == "A newer human correction must survive"
    upgraded.state.db.engine.dispose()


def test_upgrade_keeps_other_authority_out_until_copy_is_published(tmp_path, monkeypatch):
    import sqlite3

    import app.privacy as privacy
    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    original, checked = privacy.persistent_document, []

    def attempt_competing_write(kind, data, **kwargs):
        if kind == "transcript_segments" and not checked:
            with closing(sqlite3.connect(tmp_path / "legacy.db", timeout=0.01)) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
            checked.append(True)
        return original(kind, data, **kwargs)

    monkeypatch.setattr(privacy, "persistent_document", attempt_competing_write)
    upgraded = create_app(settings)
    upgraded.state.db.engine.dispose()
    assert checked == [True]


def test_restart_finishes_stage_cleanup_after_committed_publication(tmp_path, monkeypatch):
    import app.migrate as migration
    from app.main import create_app

    settings = legacy_upgrade_store(tmp_path)
    remove, failures = migration.shutil.rmtree, []

    def interrupt(path, *args, **kwargs):
        if Path(path).name == ".opendialer-upgrade-v1" and not failures:
            failures.append(True)
            raise OSError("Synthetic exit after publication")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(migration.shutil, "rmtree", interrupt)
    with pytest.raises(OSError, match="after publication"):
        create_app(settings)
    assert (tmp_path / ".opendialer-upgrade-v1").exists()
    resumed = create_app(settings)
    resumed.state.db.engine.dispose()
    assert not (tmp_path / ".opendialer-upgrade-v1").exists()
    assert len(list((tmp_path / "sanitized-upgrades").glob("*.db"))) == 1


@pytest.mark.parametrize("entry", ["application", "cli_migrate"])
@pytest.mark.parametrize("damage", ["wrong_key", "late_corrupt_row"])
def test_failed_upgrade_authentication_preserves_original_store(tmp_path, monkeypatch, entry, damage):
    import sqlite3
    import sys

    from app.config import Settings
    from app.db import Database
    from app.main import create_app

    path = tmp_path / "original.db"
    url = "sqlite:///" + str(path)
    secret = "synthetic-migration-authority"
    db = Database(url, encryption_key=secret)
    with db.transaction() as s:
        for i in range(603):
            put(
                s,
                "transcript_segments",
                "t",
                "s",
                {"segment_id": str(i), "text": RAW},
                object_id=f"legacy-{i:04}",
            )
        put(s, "notes", "t", "s", {"human_text": "Keep the original relationship note"}, object_id="note")
    db.engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE metric_buckets")
        if damage == "late_corrupt_row":
            connection.execute(
                "UPDATE transcript_segments SET data=? WHERE id='legacy-0602'",
                (json.dumps({"od_encrypted": 1, "token": "damaged-synthetic-ciphertext"}),),
            )
        connection.commit()
        before = list(connection.iterdump())
    candidate_key = "wrong-key" if damage == "wrong_key" else secret
    with pytest.raises(ValueError, match="authenticated"):
        if entry == "application":
            create_app(
                Settings(
                    data_dir=tmp_path,
                    database_url=url,
                    secret_key=candidate_key,
                    local_token="synthetic-local",
                    worker_enabled=False,
                    simulation=True,
                )
            )
        else:
            from app.cli import main

            monkeypatch.setenv("OPENDIALER_DATABASE_URL", url)
            monkeypatch.setenv("OPENDIALER_SECRET_KEY", candidate_key)
            monkeypatch.setenv("OPENDIALER_LOCAL_TOKEN", "synthetic-local")
            monkeypatch.setattr(sys, "argv", ["opendialer", "migrate", "--data-dir", str(tmp_path)])
            main()
    with closing(sqlite3.connect(path)) as connection:
        assert list(connection.iterdump()) == before, (
            "No DDL, backfill or partial sanitation may precede authentication"
        )


def test_empty_business_authority_restores_without_resurrecting_auth(workspace, app, tmp_path):
    import shutil

    from app.db import MODELS
    from app.main import create_app
    from sqlalchemy import select

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    backup = service.backup(p)
    service.suppress(p, "+14155550177", "opt_out")
    root = tmp_path / "separate-recovery-authority"
    root.mkdir(mode=0o700)
    (root / "backups").mkdir(mode=0o700)
    name = backup["backup_id"] + ".backup"
    shutil.copyfile(service.settings.data_dir / "backups" / name, root / "backups" / name)
    for source in service.settings.data_dir.glob("*-journal*"):
        if source.suffix in {".enc", ".head"}:
            shutil.copyfile(source, root / source.name)
    target_app = create_app(
        service.settings.model_copy(
            update={
                "data_dir": root,
                "database_url": "sqlite:///" + str(root / "restored.db"),
            }
        ),
        clock=service.clock,
    )
    target = target_app.state.service
    with target.db.transaction() as s:
        put(s, "tenants", p["tenant_id"], "", {"name": "Current authority"}, object_id=p["tenant_id"])
        put(
            s,
            "users",
            p["tenant_id"],
            p["seller_id"],
            {"role": "admin", "password_hash": "current-not-backup-password"},
            object_id=p["user_id"],
        )
        put(
            s,
            "sessions",
            p["tenant_id"],
            p["seller_id"],
            {"user_id": p["user_id"], "revoked": False},
            object_id="new-approved-session",
        )
    result = target.restore(p, backup["backup_id"])
    assert result["outbound_paused"] and result["recovered_records"] > 0
    with target.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["provider_id"] == call["provider_id"]
        assert get(s, "users", p["user_id"]).data["password_hash"] == "current-not-backup-password"
        assert [r.id for r in s.scalars(select(MODELS["sessions"]))] == ["new-approved-session"]
        assert any(
            r.data.get("phone") == "+14155550177" and r.data.get("active")
            for r in target.scoped(s, "suppressions", p)
        )
        assert s.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert s.execute(text("PRAGMA foreign_key_check")).all() == []
    target.db.engine.dispose()


def test_sanitation_commits_bounded_pages_and_resumes(workspace, app, monkeypatch):
    import app.privacy as privacy
    from app.db import MODELS
    from sqlalchemy import select

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(603):
            put(
                s,
                "transcript_segments",
                p["tenant_id"],
                p["seller_id"],
                {"segment_id": str(i), "text": RAW},
                object_id=f"legacy-{i:04}",
            )
    original = privacy.persistent_document
    seen = []

    def crash(kind, data, **kw):
        if kind == "transcript_segments":
            seen.append(data.get("segment_id"))
            if len(seen) == 501:
                raise OSError("Synthetic interrupted migration")
        return original(kind, data, **kw)

    monkeypatch.setattr(privacy, "persistent_document", crash)
    with pytest.raises(OSError):
        service.purge_legacy_raw_content()
    with service.db.transaction(False) as s:
        records = list(s.scalars(select(MODELS["transcript_segments"])))
        assert sum("text" not in r.data for r in records) == 500
    seen.clear()
    monkeypatch.setattr(privacy, "persistent_document", original)
    service.privacy_cutover()
    with service.db.transaction(False) as s:
        assert all("text" not in r.data for r in s.scalars(select(MODELS["transcript_segments"])))


def test_legacy_authorization_journal_drops_raw_and_keeps_recovery_identity(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    entry = {
        "call_id": call["id"],
        "tenant_id": p["tenant_id"],
        "seller_id": p["seller_id"],
        "user_id": p["user_id"],
        "at": service.clock().isoformat(),
        "provider_id": call["provider_id"],
        "command_id": call["id"],
        "command_hash": "f" * 64,
        "intent": {**call, "legacy_model_extension": RAW, "recording_url": "https://example.test/raw"},
        "unknown_model_body": RAW,
    }
    path = service.settings.data_dir / "authorization-journal.enc"
    path.write_bytes(service.vault.cipher.encrypt(json.dumps(entry).encode()) + b"\n")
    service.purge_legacy_raw_content()
    decoded = [json.loads(service.vault.cipher.decrypt(line)) for line in path.read_bytes().splitlines()]
    assert RAW not in str(decoded) and "recording_url" not in str(decoded)
    assert decoded[0]["provider_id"] == call["provider_id"]
    assert service._restore_journal(p)[0][call["id"]]["provider_id"] == call["provider_id"]


def test_legacy_copy_allowlist_continuity(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    human = "Keep the rep-authored relationship correction."
    with service.db.transaction() as s:
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call["id"], "human_text": human, "notes": {"summary": RAW, "transcript_version": 0}},
            key=call["id"],
        )
        identity = note.id
        put(
            s,
            "transcript_segments",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call["id"], "segment_id": "legacy", "text": RAW, "revision": 1, "is_final": True},
            key="legacy",
        )
    backup = service.backup(p)
    path = service.settings.data_dir / "backups" / (backup["backup_id"] + ".backup")
    decoded = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    assert RAW not in str(decoded)
    assert human in str(decoded)
    assert not {"metric_contributions", "metric_buckets"} & decoded["tables"].keys()
    service.purge_legacy_raw_content()
    service.purge_legacy_raw_content()
    service.db.backfill_operational_columns()
    migrate_database(service.db.engine.url.render_as_string(hide_password=False))
    assert RAW not in persisted_content(service, p)
    with service.db.transaction(False) as s:
        assert (
            hashlib.sha256(get(s, "notes", identity).data["human_text"].encode()).digest()
            == hashlib.sha256(human.encode()).digest()
        )
        assert (
            s.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0017_metric_projections"
        )
        assert {"ctx_key", "ctx_status", "ctx_index_version"} <= {
            c["name"] for c in inspect(s.connection()).get_columns("outcomes")
        }


@pytest.mark.parametrize("platform", ["macos-aarch64", "windows-x86_64"])
def test_actual_update_allowed_data(platform):
    """Executed against E14 receipts; never substitute a fresh install for an update."""
    location = os.getenv("OPENDIALER_NATIVE_COMPLETION_RECEIPTS")
    if not location:
        pytest.skip("E14 installed-update receipt pending; this does not accept the native gate")
    receipt = json.loads((Path(location) / f"{platform}.json").read_text())
    assert receipt["installation_method"] == "signed_in_place_update"
    assert receipt["previous_version"] == "0.2.1" and receipt["installed_version"] == "0.3.0"
    assert receipt["updater_exited"] is True
    assert receipt["installed_backend_sha256"] == receipt["verified_package_backend_sha256"]
    assert receipt["before"]["allowed_identity_sha256"] == receipt["after"]["allowed_identity_sha256"]
    assert receipt["before"]["human_notes_sha256"] == receipt["after"]["human_notes_sha256"]
    assert receipt["after"]["integrity"] == "ok" and receipt["after"]["legacy_raw_found"] is False
    assert receipt["session_restored"] is True
    assert {"local", "connected_postgresql"} <= receipt["native_journeys"].keys()
    assert all(row["passed"] for row in receipt["native_journeys"].values())
