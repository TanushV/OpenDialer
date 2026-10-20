"""Destructive test-store admission. This module is never packaged in the app."""

import os
import re

from sqlalchemy.engine import make_url

# A unit store is the exact disposable base name, or one pytest-xdist worker's copy of it.
_UNIT_NAMES = {
    "local": re.compile(r"opendialer_remediation_test(_gw[0-9]+)?"),
    "ci": re.compile(r"opendialer_test(_gw[0-9]+)?"),
}
_SINGLE_NAMES = {"browser": "opendialer_completion_e2e", "performance": "opendialer_completion_performance"}
_WORKER = re.compile(r"gw[0-9]+")


def _admitted_name(name, purpose, identity):
    if purpose == "unit":
        return isinstance(name, str) and _UNIT_NAMES[identity].fullmatch(name) is not None
    return name == _SINGLE_NAMES[purpose]


def validate_database_url(value: str, *, purpose: str = "unit") -> str:
    """Reject everything except the exact, separately named disposable identities."""
    if purpose not in {"unit", "browser", "performance"}:
        raise ValueError("Unknown disposable database purpose")
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError("Invalid disposable database URL") from exc
    local = (
        url.drivername == "postgresql+psycopg"
        and url.host == "127.0.0.1"
        and url.port == 55439
        and url.username == "opendialer"
        and url.password == "remediation-disposable-test"  # noqa: S105 - disposable fixture identity
        and _admitted_name(url.database, purpose, "local")
        and not url.query
    )
    ci = (
        os.getenv("GITHUB_ACTIONS") == "true"
        and url.drivername == "postgresql+psycopg"
        and url.host in {"localhost", "127.0.0.1"}
        and url.port == 5432
        and url.username == "postgres"
        and url.password == "disposable_ci_test"  # noqa: S105 - disposable CI identity
        and _admitted_name(url.database, purpose, "ci")
        and not url.query
    )
    if not (local or ci):
        # Do not leak possibly real credentials in the refusal.
        raise ValueError(f"Refusing non-disposable {purpose} database")
    return value


def worker_database_url(value: str, worker: str | None = None) -> str:
    """Derive one pytest-xdist worker's own unit store from the admitted base store.

    Without a worker identity this is the base store itself, so serial runs are unchanged.
    """
    validate_database_url(value)
    worker = worker if worker is not None else os.getenv("PYTEST_XDIST_WORKER")
    if not worker:
        return value
    if not _WORKER.fullmatch(worker):
        raise ValueError("Unknown pytest-xdist worker identity")
    url = make_url(value)
    # Deriving from an already-derived name yields "..._gw1_gw2", which the guard refuses.
    derived = url.set(database=f"{url.database}_{worker}").render_as_string(hide_password=False)
    return validate_database_url(derived)


def _worker_store(value):
    """Return (name, base store URL) for an admitted worker store, or None for the base store."""
    url = make_url(validate_database_url(value))
    base = re.sub(r"_gw[0-9]+$", "", url.database)
    if base == url.database:
        return None
    return url.database, validate_database_url(url.set(database=base).render_as_string(hide_password=False))


def _libpq(value):
    return make_url(value).set(drivername="postgresql").render_as_string(hide_password=False)


def ensure_database(value: str) -> None:
    """Create an admitted worker store when absent. The base store is provisioned externally."""
    import psycopg
    from psycopg import sql

    worker = _worker_store(value)
    if worker is None:
        return
    name, maintenance = worker
    with psycopg.connect(_libpq(maintenance), autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return
        try:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            pass


def drop_worker_database(value: str) -> None:
    """Drop only a derived worker store; the externally provisioned base store is never dropped."""
    import psycopg
    from psycopg import sql

    worker = _worker_store(value)
    if worker is None:
        raise ValueError("Refusing to drop the provisioned base store")
    name, maintenance = worker
    with psycopg.connect(_libpq(maintenance), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def reset_database(value: str, *, purpose: str = "unit") -> None:
    """Reset only the explicitly admitted store, before application startup runs."""
    from app.db import Base
    from sqlalchemy import create_engine

    engine = create_engine(validate_database_url(value, purpose=purpose))
    try:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


_FINGERPRINT = """
SELECT md5(coalesce(string_agg(item, '|' ORDER BY item), '')) FROM (
    SELECT 'c:' || c.relname || '.' || a.attname || ':' || format_type(a.atttypid, a.atttypmod)
        || ':' || a.attnotnull::text || ':' || coalesce(pg_get_expr(d.adbin, d.adrelid), '')
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm')
        AND a.attnum > 0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'i:' || indexname || ':' || indexdef FROM pg_indexes WHERE schemaname = 'public'
    UNION ALL
    SELECT 'k:' || conrelid::regclass::text || ':' || conname || ':' || pg_get_constraintdef(oid)
    FROM pg_constraint WHERE connamespace = 'public'::regnamespace
    UNION ALL
    SELECT 't:' || tgrelid::regclass::text || ':' || tgname FROM pg_trigger WHERE NOT tgisinternal
    UNION ALL
    SELECT 's:' || sequencename FROM pg_sequences WHERE schemaname = 'public'
) AS schema_items(item)
"""
_SEQUENCES = (
    "SELECT quote_ident(schemaname) || '.' || quote_ident(sequencename), start_value, last_value "
    "FROM pg_sequences WHERE schemaname = 'public'"
)


class PostgresTestStore:
    """One admitted unit store, reset between tests without rebuilding its schema.

    The first reset (and any reset after a test changed the schema) performs the original
    full rebuild: ``reset_database`` plus the caller's application startup. The resulting
    rows, sequence positions and schema fingerprint are captured. Later resets verify the
    fingerprint, delete every row a test added (all tables except ``alembic_version``),
    restore the captured startup rows and rewind sequences, which leaves exactly the
    post-startup state without recreating ~70 tables and their indexes.
    """

    def __init__(self, value: str):
        from sqlalchemy import create_engine

        self.url = validate_database_url(value)
        self.engine = create_engine(self.url)
        self.rebuilds = 0
        self._state = None

    @staticmethod
    def _quiesce(connection):
        # Only this store's leftover test connections; their locks must not block the reset.
        from sqlalchemy import text

        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )

    def _capture(self):
        from sqlalchemy import MetaData, select, text

        metadata = MetaData()
        with self.engine.connect() as connection:
            metadata.reflect(connection)
            tables = [t for t in metadata.sorted_tables if t.name != "alembic_version"]
            rows = {
                table.name: [dict(row._mapping) for row in connection.execute(select(table))]
                for table in tables
            }
            sequences = {name: (start, last) for name, start, last in connection.execute(text(_SEQUENCES))}
            fingerprint = connection.execute(text(_FINGERPRINT)).scalar()
        occupied = " UNION ALL ".join(
            f'SELECT {index} WHERE EXISTS (SELECT 1 FROM "{table.name}")'  # noqa: S608 - reflected names
            for index, table in enumerate(tables)
        )
        return {
            "fingerprint": fingerprint,
            "tables": tables,
            "occupied": occupied,
            "rows": {table.name: rows[table.name] for table in tables if rows[table.name]},
            "sequences": sequences,
        }

    def _restore(self):
        from sqlalchemy import text

        state = self._state
        with self.engine.begin() as connection:
            self._quiesce(connection)
            # A disposable store: durability of the reset itself is irrelevant.
            connection.execute(text("SET LOCAL synchronous_commit = off"))
            if connection.execute(text(_FINGERPRINT)).scalar() != state["fingerprint"]:
                return False
            occupied = {
                state["tables"][index].name for (index,) in connection.execute(text(state["occupied"]))
            }
            for table in reversed(state["tables"]):  # Dependents before their parents.
                if table.name in occupied:
                    connection.execute(table.delete())
            for table in state["tables"]:
                if table.name in state["rows"]:
                    connection.execute(table.insert(), state["rows"][table.name])
            for name, start, last in connection.execute(text(_SEQUENCES)).all():
                if (start, last) != state["sequences"][name]:
                    _, saved = state["sequences"][name]
                    connection.execute(
                        text("SELECT setval(CAST(:name AS regclass), :value, :called)"),
                        {
                            "name": name,
                            "value": start if saved is None else saved,
                            "called": saved is not None,
                        },
                    )
        return True

    def reset(self, initialize) -> None:
        """Leave the store exactly as ``reset_database`` plus ``initialize(url)`` would."""
        if self._state is not None and self._restore():
            return
        with self.engine.begin() as connection:
            self._quiesce(connection)
        self.engine.dispose()
        reset_database(self.url)
        initialize(self.url)
        self._state = self._capture()
        self.rebuilds += 1

    def dispose(self) -> None:
        self.engine.dispose()
