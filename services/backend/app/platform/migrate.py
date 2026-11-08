"""Move the owner's data from `local-2` to `local-3` (R3.7 to R3.9).

The source folder is copied, never opened in place, so it stays byte-identical and remains the rollback. The copy is
transformed, its row counts are checked against the source's, and only then does it become the new store. A failure
removes the partial copy, leaves a note in the new folder and raises `MigrationFailed`; the next launch tries again.

The values written here are a snapshot of this one transform. Later shapes belong to the code that owns them.
"""

import json
import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from app.platform.config import TEST_LINES
from app.platform.db import RETIRED_TABLES

log = logging.getLogger(__name__)

FORMAT = 3  # SQLite `user_version` of a migrated store.
DB = "opendialer.db"
DB_FILES = (DB, DB + "-wal", DB + "-shm")
FAILURE_NOTE = "migration-failed.txt"
# R3.11: every lead before this version came from a CSV file.
LEAD_FIELDS = {"source": "csv", "external_id": None, "signals": [], "number_status": "not_checked"}


class MigrationFailed(RuntimeError):
    pass


def needed(source, target):
    return (Path(source) / DB).exists() and not (Path(target) / DB).exists()


def counts(connection):
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    # Table names come from sqlite_master of the copied store, not from input.
    query = 'SELECT count(*) FROM "{}"'
    return {name: connection.execute(query.format(name)).fetchone()[0] for name in sorted(names)}


def transform(connection):
    """Drop retired tables, add the R3.11 lead fields and mark test calls. Returns the number of test calls."""
    for table in sorted(RETIRED_TABLES):
        connection.execute(f'DROP TABLE IF EXISTS "{table}"')
    phones = {}
    for lead_id, data in connection.execute("SELECT id, data FROM leads").fetchall():
        lead = json.loads(data)
        phones[lead_id] = lead.get("phone")
        lead = {**LEAD_FIELDS, **lead}
        connection.execute("UPDATE leads SET data = ? WHERE id = ?", (json.dumps(lead), lead_id))
    tests = 0
    for call_id, data in connection.execute("SELECT id, data FROM call_attempts").fetchall():
        call = json.loads(data)
        call["is_test"] = bool((call.get("disposition_input") or {}).get("delivery_test")) or (
            phones.get(call.get("lead_id")) in TEST_LINES
        )
        tests += call["is_test"]
        connection.execute("UPDATE call_attempts SET data = ? WHERE id = ?", (json.dumps(call), call_id))
    connection.execute(f"PRAGMA user_version = {FORMAT}")
    return tests


def migrate(source, target):
    source, target = Path(source), Path(target)
    partial = target.with_name(target.name + ".partial")
    moved = []
    try:
        if partial.exists():
            shutil.rmtree(partial)
        shutil.copytree(source, partial, ignore=shutil.ignore_patterns("authority.lock"))
        # Opening the copy applies its copied write-ahead log to the copy only.
        with closing(sqlite3.connect(partial / DB, isolation_level=None)) as connection:
            before = counts(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                tests = transform(connection)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            after = counts(connection)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("the copied database failed its integrity check")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        expected = {name: n for name, n in before.items() if name not in RETIRED_TABLES}
        if after != expected:
            changed = sorted(
                name for name in expected.keys() | after.keys() if expected.get(name) != after.get(name)
            )
            raise RuntimeError(f"row counts changed in {', '.join(changed)}")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The database moves last, so a store exists only once everything else is in place.
        entries = sorted(partial.iterdir(), key=lambda p: (p.name in DB_FILES, p.name == DB))
        for entry in entries:
            destination = target / entry.name
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            entry.replace(destination)
            moved.append(destination)
        partial.rmdir()
        (target / FAILURE_NOTE).unlink(missing_ok=True)
    except Exception as error:
        for path in moved:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        shutil.rmtree(partial, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        (target / FAILURE_NOTE).write_text(f"Moving {source} to {target} failed: {error}\n")
        log.exception("Data migration from %s failed", source)
        raise MigrationFailed(
            f"OpenDialer could not move your data to this version, so it did not start. Your data in {source.name} "
            f"is unchanged; reinstall the previous version to keep calling. Reason: {error}"
        ) from error
    report = {"from": str(source), "format": FORMAT, "counts": after, "test_calls": tests}
    log.info("Migrated data from %s: %s", source, json.dumps(report))
    return report
