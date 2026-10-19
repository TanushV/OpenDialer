import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from app.db import (
    STARTUP_CONTRACT,
    Base,
    Database,
    authenticate_store,
    database_cipher,
    put,
    startup_contract_current,
)


def migrate_business_state(service):
    # The legacy journal is a separate file, including after a DB-only copy cutover.
    service.vault.sanitize_legacy_authorization()
    if startup_contract_current(service.db.engine, service.db.cipher):
        return
    from app.compliance.registry import migrate_registry_chunks

    service.privacy_cutover()
    migrate_registry_chunks(service)
    service.migrate_conversation_states()
    service.migrate_followthrough()
    with service.db.transaction() as session:
        put(
            session,
            "audit_events",
            "",
            "",
            {"contract": STARTUP_CONTRACT},
            key="completion-startup",
            now=service.clock(),
        )


def _save_manifest(path, value, cipher):
    from app.security import Vault

    temporary = path.with_suffix(".tmp")
    if temporary.is_symlink():
        raise ValueError("Staging manifest is not protected")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(cipher.encrypt(json.dumps(value, sort_keys=True).encode()))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    Vault._sync_directory(path.parent)


def _stage_manifest(source, stage, cipher):
    """Reject unrelated or redirected files before resetting/removing owned work."""
    if stage.is_symlink():
        raise ValueError("Staging directory is not protected")
    stage.mkdir(mode=0o700, exist_ok=True)
    manifest = stage / "manifest.enc"
    identity = {"source": str(source), "contract": STARTUP_CONTRACT}
    if any(p.is_symlink() for p in stage.rglob("*")):
        raise ValueError("Staging content is not protected")
    saved = None
    if manifest.exists():
        try:
            saved = json.loads(cipher.decrypt(manifest.read_bytes()))
        except Exception as exc:
            raise ValueError("Staging manifest cannot be authenticated") from exc
        if any(saved.get(k) != v for k, v in identity.items()):
            raise ValueError("Staging manifest belongs to a different authority or contract")
    elif any(stage.iterdir()):
        raise ValueError("Staging directory contains unowned content")
    allowed = {
        "manifest.enc",
        "manifest.tmp",
        "runtime",
        "candidate.db",
        "candidate.db-wal",
        "candidate.db-shm",
        "candidate.db-journal",
    }
    if any(p.name not in allowed for p in stage.iterdir()):
        raise ValueError("Staging directory contains unowned content")
    return saved


def _stage_copy(original, source, stage, cipher):
    """Only an authenticated stage owned by this exact database can be resumed/reset."""
    saved = _stage_manifest(source, stage, cipher)
    manifest, candidate = stage / "manifest.enc", stage / "candidate.db"
    identity = {"source": str(source), "contract": STARTUP_CONTRACT}
    with source.open("rb") as handle:
        fingerprint = hashlib.file_digest(handle, "sha256").hexdigest()
    if not saved or saved.get("source_sha256") != fingerprint or saved.get("phase") != "copied":
        saved = {**identity, "source_sha256": fingerprint, "phase": "copying"}
        _save_manifest(manifest, saved, cipher)
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate.with_name(candidate.name + suffix).unlink(missing_ok=True)
        os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with closing(sqlite3.connect(candidate)) as target:
            original.backup(target)
        candidate.chmod(0o600)
        saved["phase"] = "copied"
        _save_manifest(manifest, saved, cipher)
    if not candidate.is_file():
        raise ValueError("Staging candidate is missing; original store is unchanged")
    return candidate, fingerprint


def prepare_database(settings, clock):
    """Validate an existing SQLite upgrade on a copy, then publish with SQLite's atomic backup.

    Exclusive locking survives COMMIT until this connection closes; no other authority
    may write between the snapshot and publication. Never rename a database/WAL file.
    PostgreSQL retains transactional DDL and the existing resumable business backfills.
    """
    url, cipher = settings.database_url, database_cipher(settings.secret_key)
    parsed = make_url(url)
    source = (
        Path(parsed.database).absolute()
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:")
        else None
    )
    existed = source is not None and source.exists() and source.stat().st_size > 0
    if source and source.is_symlink():
        raise ValueError("Authority database must not be a symbolic link")
    engine = create_engine(url)
    stage = settings.data_dir / ".opendialer-upgrade-v1"
    try:
        if startup_contract_current(engine, cipher):
            if source and (stage.exists() or stage.is_symlink()):
                _stage_manifest(source, stage, cipher)
                shutil.rmtree(stage)
            return
    finally:
        engine.dispose()
    if not existed:
        migrate_database(url, encryption_key=settings.secret_key)
        return
    with closing(sqlite3.connect(source, timeout=1)) as original:
        try:
            original.execute("PRAGMA locking_mode=EXCLUSIVE")
            original.execute("BEGIN EXCLUSIVE")
            original.commit()
            original.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Close other OpenDialer authorities before upgrading this store") from exc
        candidate, fingerprint = _stage_copy(original, source, stage, cipher)
        candidate_url = "sqlite:///" + str(candidate)
        migrate_database(candidate_url, encryption_key=settings.secret_key)
        candidate_db = Database(candidate_url, encryption_key=settings.secret_key)
        try:
            from app.administration import Dialer

            working = settings.model_copy(
                update={"database_url": candidate_url, "data_dir": stage / "runtime"}
            )
            migrate_business_state(Dialer(candidate_db, working, clock))
        finally:
            candidate_db.engine.dispose()
        with closing(sqlite3.connect(candidate)) as clean:
            clean.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            clean.execute("VACUUM")
            if (
                clean.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or clean.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise ValueError("Staging integrity validation failed; original store is unchanged")
            # Retain only the sanitized encrypted store, before publishing the live one.
            recovery = settings.data_dir / "sanitized-upgrades"
            if recovery.is_symlink():
                raise ValueError("Upgrade recovery directory is not protected")
            recovery.mkdir(mode=0o700, exist_ok=True)
            saved = recovery / (fingerprint + ".db")
            if saved.is_symlink():
                raise ValueError("Upgrade recovery file is not protected")
            os.close(os.open(saved, os.O_CREAT | os.O_WRONLY, 0o600))
            with closing(sqlite3.connect(saved)) as destination:
                clean.backup(destination)
            saved.chmod(0o600)
            with saved.open("rb") as retained:
                os.fsync(retained.fileno())
            from app.security import Vault

            Vault._sync_directory(recovery)
            Vault._sync_directory(settings.data_dir)
            clean.backup(original, pages=256)
        original.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.rmtree(stage)


def migrate_database(url, *, encryption_key=None):
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    engine = create_engine(url)
    try:
        if encryption_key:
            authenticate_store(engine, database_cipher(encryption_key))
        existing = set(inspect(engine).get_table_names())
        if "tenants" in existing and "alembic_version" not in existing:
            # Adopt only the pre-migration development schema after checking every existing column.
            for table in Base.metadata.sorted_tables:
                if table.name not in existing:
                    continue
                actual = {c["name"] for c in inspect(engine).get_columns(table.name)}
                bootstrap_columns = {
                    name for name in table.columns.keys() if not name.startswith(("op_", "ctx_"))
                }
                if table.name == "jobs":
                    bootstrap_columns -= {"seller_id", "owner_id"}
                if actual not in (
                    set(table.columns.keys()),
                    bootstrap_columns,
                ):
                    raise RuntimeError(
                        "Unversioned schema differs from the known bootstrap schema; restore/review before migration"
                    )
            Base.metadata.create_all(engine)
            command.stamp(config, "0001_initial")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
