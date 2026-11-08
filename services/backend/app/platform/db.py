import hashlib
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    literal,
    select,
    text,
    tuple_,
    union_all,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    make_transient_to_detached,
    mapped_column,
    sessionmaker,
)

from app.operational_schema import (
    CONTEXT_FIELDS,
    HOT_TABLES,
    columns,
    context_columns,
    context_indexes,
    indexes,
    page_indexes,
    sync_columns,
)


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Document:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    seller_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    external_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    data: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @declared_attr.directive
    def __mapper_args__(cls):
        # update() owns the visible source version; SQL also compares it on writes.
        return {"version_id_col": cls.version, "version_id_generator": False}


# Explicit tables retain the specification's data ownership and lifecycle boundaries.
# Tables older versions created and this one no longer uses (R2.9). Restore skips them; the
# migration in Phase 3 (R3.7) drops them from the copied store.
# R8.4 retired the saved report views (`report_projections`, `report_revisions`): estimates come from calls.
RETIRED_TABLES = frozenset(
    {
        "companies",
        "consent_evidence",
        "evaluation_reports",
        "work_intervals",
        "report_projections",
        "report_revisions",
    }
)

TABLES = [
    "tenants",
    "sellers",
    "users",
    "sessions",
    "campaigns",
    "policy_versions",
    "leads",
    "phone_endpoints",
    "suppressions",
    "registry_snapshots",
    "compliance_decisions",
    "number_inventory",
    "number_health",
    "lead_number_bindings",
    "number_leases",
    "reservations",
    "call_attempts",
    "call_legs",
    "provider_events",
    "webhook_inbox",
    "command_outbox",
    "transcript_segments",
    "research_sources",
    "research_facts",
    "playbooks",
    "script_versions",
    "coach_events",
    "conversation_states",
    "notes",
    "scorecards",
    "reviews",
    "followup_tasks",
    "commitments",
    "feature_snapshots",
    "model_versions",
    "experiment_assignments",
    "policy_decisions",
    "outcomes",
    "usage_ledger",
    "budgets",
    "audit_events",
    "trackers",
    "snippets",
    "comments",
    "learning_datasets",
    "conversion_events",
    "coaching_feedback",
    "script_memories",
]
MODELS = {}
for _name in TABLES:
    MODELS[_name] = type(
        "".join(x.title() for x in _name.split("_")),
        (Document, Base),
        {
            "__tablename__": _name,
            **(columns(_name) if _name in HOT_TABLES else {}),
            **({"op_kind": Column(String(40), nullable=True)} if _name == "command_outbox" else {}),
            **(context_columns(_name) if _name in CONTEXT_FIELDS else {}),
            "__table_args__": (
                UniqueConstraint("tenant_id", "seller_id", "external_key", name=f"uq_{_name}_key"),
                *(indexes(_name) if _name in HOT_TABLES else ()),
                *page_indexes(_name),
                *(context_indexes(_name) if _name in CONTEXT_FIELDS else ()),
                *(
                    (
                        Index(
                            "ix_command_outbox_kind",
                            "tenant_id",
                            "seller_id",
                            "op_kind",
                            "op_status",
                            "op_due_at",
                        ),
                    )
                    if _name == "command_outbox"
                    else ()
                ),
            ),
            "__module__": __name__,
        },
    )


class ActiveClaim(Base):
    __tablename__ = "active_claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    seller_id: Mapped[str] = mapped_column(String(36), index=True)
    endpoint_id: Mapped[str] = mapped_column(String(36))
    lead_id: Mapped[str] = mapped_column(String(36))
    rep_id: Mapped[str] = mapped_column(String(36))
    number_id: Mapped[str] = mapped_column(String(36), index=True)
    reservation_id: Mapped[str] = mapped_column(String(36), unique=True)
    call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("seller_id", "endpoint_id"),
        UniqueConstraint("seller_id", "lead_id"),
        UniqueConstraint("tenant_id", "rep_id"),
    )


class Event(Base):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(36))
    entity_version: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    seller_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    queue: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        UniqueConstraint("tenant_id", "key"),
        Index("ix_jobs_scope_page", "tenant_id", "seller_id", "id"),
        Index("ix_jobs_owner_page", "tenant_id", "seller_id", "owner_id", "id"),
        Index("ix_jobs_due", "queue", "status", "available_at", "priority"),
    )


PLANNED_ROWS = 1_000_000


def plan_statistics(connection):
    """Fixed SQLite planner statistics (sqlite_stat1), written when the authority opens.

    With no statistics SQLite takes an equality on tenant_id/seller_id as selective, so a scoped
    `id IN (...)` lookup scanned the seller's whole unique-key range (650 ms for 32 of 100k
    endpoints). A real ANALYZE costs seconds per index on a large store while holding the
    writer. One local authority holds one tenant and normally one seller, so record exactly
    that: scope-column prefixes match every row of a large table; other columns keep SQLite's
    default narrowing. Partial indexes hold a small live subset (open calls, due work), so they
    estimate fewer rows and win ties, which otherwise follow index creation order.
    See https://www.sqlite.org/optoverview.html#manctrl."""
    connection.exec_driver_sql("ANALYZE sqlite_schema")  # Creates the statistics tables.
    rows = []
    tables = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    for (table,) in list(tables):
        for _, index, unique, _, partial in list(connection.exec_driver_sql(f'PRAGMA index_list("{table}")')):
            columns = [row[2] for row in connection.exec_driver_sql(f'PRAGMA index_info("{index}")')]
            if not columns or None in columns:
                continue  # Expression indexes keep SQLite's defaults.
            stat, narrowing = [PLANNED_ROWS], 1 if partial else 10
            for i in range(len(columns)):
                if set(columns[: i + 1]) <= {"tenant_id", "seller_id"}:
                    stat.append(PLANNED_ROWS)
                else:
                    stat.append(1 if unique and i == len(columns) - 1 else narrowing)
                    narrowing = max(1, narrowing - 1)
            rows.append((table, index, " ".join(map(str, stat))))
    connection.exec_driver_sql("DELETE FROM sqlite_stat1")
    if connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat4'").first():
        connection.exec_driver_sql("DELETE FROM sqlite_stat4")
    if rows:
        connection.exec_driver_sql("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (?, ?, ?)", rows)
    connection.exec_driver_sql("ANALYZE sqlite_schema")  # Reload for this connection.


class OldStore(RuntimeError):
    """A 0.2/0.3 store (Alembic-migrated, encrypted); this build starts only from fresh stores."""


class Database:
    def __init__(self, url):
        if not url.startswith("sqlite"):
            raise ValueError("OpenDialer stores its data in one SQLite file on this Mac")
        self.url = url
        # One SQLite writer at a time, admitted before SQLite's own busy handler.
        self._sqlite_writer = threading.Lock()
        # In-process runners subscribe to committed job changes (queue names).
        self.job_listeners = []
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        tables = set(inspect(self.engine).get_table_names())
        if "alembic_version" in tables:
            raise OldStore(
                "This database was created by OpenDialer 0.2 or 0.3, which this version cannot read. "
                "Use a new database or data folder; the old one is left unchanged."
            )
        if not set(Base.metadata.tables) <= tables:
            Base.metadata.create_all(self.engine)
        self.plan()

    def plan(self):
        with self.engine.begin() as connection:
            plan_statistics(connection)
        self.engine.dispose()  # Pooled connections reload the statistics on next use.

    @contextmanager
    def transaction(self, write=True, *, session=None):
        # An explicitly supplied session belongs to the caller's transaction and thread.
        # Helpers may flush, but only that outer boundary commits or rolls back.
        if session is not None:
            if session.bind is not self.engine or (write and not session.info.get("write")):
                raise ValueError("An owned writable transaction is required")
            yield session
            return
        # Admit one writer per SQLite authority before its busy-handler backoff.
        # WAL readers and other stores never wait on this writer-only admission.
        with self.sessions() as session:
            session.info["write"] = write
            session.info["database"] = self
            admitted = committed = False
            try:
                if write:
                    if not self._sqlite_writer.acquire(timeout=30):
                        raise TimeoutError("SQLite writer admission timed out")
                    admitted = True
                    session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
                committed = True
            except Exception:
                for callback in session.info.pop("before_rollback", []):
                    callback()
                session.rollback()
                raise
            finally:
                if admitted:
                    self._sqlite_writer.release()
                # A journal failure inside a caller-owned transaction must persist
                # its hold only after that transaction releases its write lock.
                try:
                    for callback in session.info.pop("after_transaction", []):
                        callback()
                finally:
                    woken = session.info.pop("job_wakeups", None) if committed else None
                    # SQLAlchemy closes connections/identity maps, but keeps custom info.
                    # Query/transaction cycles must not retain decrypted request caches.
                    session.info.clear()
                    if woken:
                        # Only committed work, after the writer is released.
                        for listener in tuple(self.job_listeners):
                            listener(frozenset(woken))

    @contextmanager
    def snapshot(self):
        """One consistent read across many statements, never a writer.

        SQLite holds a WAL read snapshot from its first read until commit. Writers are never blocked.
        """
        with self.transaction(False) as session:
            session.execute(text("BEGIN DEFERRED"))
            yield session

    def authorize_stream(self, session, principal, *, shared=False):
        """No-op: SQLite admits one writer at a time, so authorization changes are already ordered.

        Kept for its callers until Phase 3 (R3.2) moves them into plain module functions.
        """

    def reset(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def integrity(self):
        with self.transaction(False) as s:
            return s.execute(text("PRAGMA integrity_check")).scalar() == "ok"


def rows(s, kind, tenant_id=None, seller_id=None):
    model = MODELS[kind]
    query = select(model)
    if tenant_id is not None:
        query = query.where(model.tenant_id == tenant_id)
    if seller_id is not None:
        query = query.where(model.seller_id == seller_id)
    cache_key = (kind, tenant_id, seller_id)
    cache = s.info.get("row_cache")
    if cache is not None and cache_key in cache:
        return [r for r in cache[cache_key] if r not in s.deleted]
    result = list(s.scalars(query))
    if cache is not None:
        cache[cache_key] = result
    return result


def prefetch(s, p, specifications):
    """One round trip for small policy/context tables and bounded related rows.

    Specifications explicitly filter large tables. Loaded objects enter this session's
    identity map without a second SELECT. No cache survives the transaction.
    """
    common = ("id", "tenant_id", "seller_id", "external_key", "data", "version", "created_at")
    queries = []
    cache = s.info.setdefault("row_cache", {})
    for kind, conditions in specifications.items():
        model = MODELS[kind]
        queries.append(
            select(literal(kind).label("kind"), *(getattr(model, c) for c in common)).where(
                model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], *conditions
            )
        )
        cache[(kind, p["tenant_id"], p["seller_id"])] = []
    for value in s.execute(union_all(*queries)).mappings():
        obj = MODELS[value["kind"]](**{name: value[name] for name in common})
        make_transient_to_detached(obj)
        obj = s.merge(obj, load=False)
        cache[(value["kind"], p["tenant_id"], p["seller_id"])].append(obj)
    return cache


@event.listens_for(Session, "before_flush")
def index_pending_rows(session, *_):
    for job in session.new:
        if isinstance(job, Job):
            job.seller_id = job.payload.get("seller_id") or ""
            job.owner_id = job.payload.get("user_id") or ""
    for row in session.new | session.dirty:
        if isinstance(row, Document):
            sync_columns(row)


@event.listens_for(Session, "after_flush")
def announce_job_changes(session, _):
    # Still the pre-flush view, including jobs added by before_flush hooks. Enqueued,
    # requeued or released work wakes that queue's in-process lane after commit.
    for row in session.new | session.dirty:
        if isinstance(row, Job):
            session.info.setdefault("job_wakeups", set()).add(row.queue)


def insert_jobs(session, rows):
    """Insert new job identities, skipping any a concurrent writer is adding (see insert_absent).

    Core inserts bypass the ORM hooks above, so this applies them: scope columns come
    from the payload and inserted work wakes its lane after commit."""
    for row in rows:
        row["seller_id"] = row["payload"].get("seller_id") or ""
        row["owner_id"] = row["payload"].get("user_id") or ""
    inserted = insert_absent(session, Job, rows, ("tenant_id", "key"))
    for row in rows:
        if (row["tenant_id"], row["key"]) in inserted:
            session.info.setdefault("job_wakeups", set()).add(row["queue"])
    return inserted


def get(s, kind, object_id, tenant_id=None, *, lock=False):
    obj = s.get(MODELS[kind], object_id, with_for_update=True if lock else None)
    return obj if obj and (tenant_id is None or obj.tenant_id == tenant_id) else None


def put(s, kind, tenant_id, seller_id, data, *, object_id=None, key=None, now=None):
    obj = MODELS[kind](
        id=object_id or uid(),
        tenant_id=tenant_id,
        seller_id=seller_id,
        external_key=key,
        data=data,
        created_at=now or utcnow(),
        version=1,
    )
    s.add(obj)
    cache = s.info.get("row_cache", {})
    for cache_key, values in cache.items():
        if cache_key[0] == kind and cache_key[1] in (None, tenant_id) and cache_key[2] in (None, seller_id):
            values.append(obj)
    if not s.info.get("defer_flush"):
        s.flush()
    return obj


def after_key(time, identity, at, anchor, *, descending=False):
    """Rows strictly after (or before) a (time, id) cursor, as one row-value comparison.

    `time > at OR (time = at AND id > anchor)` makes SQLite scan from the start of the
    index range; the row value seeks the same (..., created_at, id) index to the cursor."""
    key, cursor = tuple_(time, identity), tuple_(at, anchor)
    return key < cursor if descending else key > cursor


def insert_absent(session, model, rows, keys):
    """INSERT ... ON CONFLICT DO NOTHING; returns the key tuples this statement inserted.

    A concurrent transaction adding the same new identity makes this wait for it and
    skip the row instead of aborting the caller's whole transaction."""
    if not rows:
        return set()
    from sqlalchemy.dialects.sqlite import insert

    columns = [getattr(model, k) for k in keys]
    statement = insert(model).values(rows).on_conflict_do_nothing(index_elements=columns).returning(*columns)
    return {tuple(row) for row in session.execute(statement)}


def update(obj, **data):
    obj.data = {**obj.data, **data}
    obj.version += 1
    return obj


def doc(obj):
    return {"id": obj.id, "version": obj.version, **obj.data}


def emit(s, tenant_id, kind, obj, now, payload=None):
    from app.privacy import bus_metadata

    s.add(
        Event(
            tenant_id=tenant_id,
            type=kind,
            entity_id=obj.id,
            entity_version=obj.version,
            occurred_at=now,
            payload={
                **bus_metadata(payload or {}),
                "_seller_id": getattr(obj, "seller_id", ""),
                # Rep-scoped feeds show a row only to its rep or owner (calls, legs, tasks).
                "_rep_id": (obj.data.get("rep_id") or obj.data.get("owner_id"))
                if hasattr(obj, "data")
                else None,
            },
        )
    )


def audit(s, tenant_id, seller_id, actor, action, target, now, detail=None):
    put(
        s,
        "audit_events",
        tenant_id,
        seller_id,
        {"actor_id": actor, "action": action, "target_id": target, "detail": detail or {}},
        now=now,
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# Scope helpers for the section 4 packages. `scope` is the request's workspace (today the principal dict); new code
# passes it through without naming its columns, so removing tenants (R3.4) changes only these functions.
SCOPE_KEY = "tenant_id"  # Also the field name inside carrier client_state, which older legs still carry.


def workspace(scope):
    return scope[SCOPE_KEY]


def scope_of(workspace_id):
    return {SCOPE_KEY: workspace_id}


def scoped_get(s, scope, kind, object_id, *, lock=False):
    return get(s, kind, object_id, workspace(scope), lock=lock)


def scoped_put(s, scope, kind, data, **options):
    return put(s, kind, workspace(scope), scope.get("seller_id"), data, **options)


def scoped_where(model, scope):
    conditions = [model.tenant_id == workspace(scope)]
    if scope.get("seller_id"):
        conditions.append(model.seller_id == scope["seller_id"])
    return conditions
