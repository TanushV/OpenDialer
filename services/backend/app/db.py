import base64
import contextvars
import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
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
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.types import TypeDecorator

from app.operational_schema import (
    CONTEXT_FIELDS,
    HOT_TABLES,
    METRIC_TABLES,
    columns,
    context_columns,
    context_indexes,
    ensure_columns,
    indexes,
    metric_columns,
    metric_indexes,
    page_indexes,
    sync_columns,
)

_DATABASE_CIPHER = contextvars.ContextVar("opendialer_database_cipher", default=None)
_DATABASE_LOOKUP_KEY = contextvars.ContextVar("opendialer_database_lookup_key", default=None)
STARTUP_CONTRACT = "completion-0017-raw2-v1"


def database_cipher(key):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256((key + ":database:v1").encode()).digest()))


def startup_contract_current(engine, cipher):
    """Authenticate the completed cutover once, without rescanning historical payloads."""
    with engine.connect() as connection:
        existing = set(inspect(connection).get_table_names())
        if not {*TABLES, "events", "jobs", "alembic_version"}.issubset(existing):
            return False
        if (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            != "0017_metric_projections"
        ):
            return False
        value = connection.execute(
            text(
                "SELECT data FROM audit_events WHERE tenant_id='' AND seller_id='' "
                "AND external_key='completion-startup'"
            )
        ).scalar()
        if not value:
            return False
        try:
            value = json.loads(value) if isinstance(value, str) else value
            value = json.loads(cipher.decrypt(value["token"].encode()))
        except (InvalidToken, KeyError, ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Stored data cannot be authenticated with this authority key") from exc
        return value.get("contract") == STARTUP_CONTRACT


def authenticate_store(engine, cipher):
    """Read all existing encrypted payloads before any upgrade DDL or backfill.

    Raw SQL intentionally does not require the *new* mapped columns to exist.
    Fetch pages stay bounded; no plaintext content leaves this check.
    """
    with engine.connect() as connection:
        existing = set(inspect(connection).get_table_names())
        for name in [*TABLES, "events", "jobs"]:
            if name not in existing:
                continue
            field = "payload" if name in {"events", "jobs"} else "data"
            rows = connection.execution_options(stream_results=True).execute(
                text(f'SELECT "{field}" FROM "{name}"')  # noqa: S608 - fixed table/field allowlists
            )
            for page in rows.partitions(500):
                for (value,) in page:
                    value = json.loads(value) if isinstance(value, str) else value
                    if isinstance(value, dict) and value.get("od_encrypted") == 1:
                        try:
                            json.loads(cipher.decrypt(value["token"].encode()))
                        except (InvalidToken, KeyError, ValueError, AttributeError, TypeError) as exc:
                            raise ValueError(
                                "Stored data cannot be authenticated with this authority key"
                            ) from exc


class EncryptedJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def coerce_compared_value(self, op, value):
        return self.impl.coerce_compared_value(op, value)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        cipher = _DATABASE_CIPHER.get()
        if cipher is None:
            raise ValueError("A database encryption context is required")
        return {
            "od_encrypted": 1,
            "token": cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode(),
        }

    def process_result_value(self, value, dialect):
        if isinstance(value, dict) and value.get("od_encrypted") == 1:
            cipher = _DATABASE_CIPHER.get()
            if cipher is None:
                raise ValueError("A database encryption context is required")
            try:
                return json.loads(cipher.decrypt(value["token"].encode()))
            except (InvalidToken, KeyError, ValueError) as exc:
                raise ValueError("Stored data cannot be authenticated with this authority key") from exc
        return value


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
    data: Mapped[dict] = mapped_column(MutableDict.as_mutable(EncryptedJSON), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @declared_attr.directive
    def __mapper_args__(cls):
        # update() owns the visible source version; SQL also compares it on writes.
        return {"version_id_col": cls.version, "version_id_generator": False}


# Explicit tables retain the specification's data ownership and lifecycle boundaries.
TABLES = [
    "tenants",
    "sellers",
    "users",
    "memberships",
    "sessions",
    "campaigns",
    "policy_versions",
    "companies",
    "leads",
    "phone_endpoints",
    "lead_phone_links",
    "consent_evidence",
    "suppressions",
    "registry_subscriptions",
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
    "transcript_revisions",
    "research_sources",
    "research_facts",
    "playbooks",
    "script_versions",
    "treatment_variants",
    "coach_events",
    "conversation_states",
    "notes",
    "scorecards",
    "scorecard_items",
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
    "batch_requests",
    "rnd_queries",
    "learning_datasets",
    "conversion_events",
    "allocation_decisions",
    "scheduling_plans",
    "evaluation_reports",
    "coaching_feedback",
    "work_intervals",
    "report_projections",
    "metric_contributions",
    "metric_buckets",
]
DERIVED_TABLES = frozenset({"report_projections", *METRIC_TABLES})
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
            **(metric_columns(_name) if _name in METRIC_TABLES else {}),
            **(
                {
                    "op_owner_id": Column(String(36), nullable=False),
                    "op_at": Column(DateTime(timezone=True), nullable=True),
                    "op_due_at": Column(DateTime(timezone=True), nullable=True),
                }
                if _name == "work_intervals"
                else {}
            ),
            "__table_args__": (
                UniqueConstraint("tenant_id", "seller_id", "external_key", name=f"uq_{_name}_key"),
                *(indexes(_name) if _name in HOT_TABLES else ()),
                *page_indexes(_name),
                *(context_indexes(_name) if _name in CONTEXT_FIELDS else ()),
                *(metric_indexes(_name) if _name in METRIC_TABLES else ()),
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
                *(
                    (
                        Index(
                            "ix_work_intervals_owner", "tenant_id", "seller_id", "op_owner_id", "created_at"
                        ),
                        Index(
                            "ix_work_intervals_overlap",
                            "tenant_id",
                            "seller_id",
                            "op_owner_id",
                            "op_due_at",
                            "op_at",
                        ),
                    )
                    if _name == "work_intervals"
                    else ()
                ),
            ),
            "__module__": __name__,
        },
    )


class ReportRevision(Base):
    __tablename__ = "report_revisions"
    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seller_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    payload: Mapped[dict] = mapped_column(EncryptedJSON, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    seller_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    queue: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(EncryptedJSON)
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
    )


class Database:
    def __init__(self, url, encryption_key=None):
        if not encryption_key:
            raise ValueError("A persistent authority encryption key is required")
        self.cipher = database_cipher(encryption_key)
        self.identity_key = hashlib.sha256((encryption_key + ":lookup:v1").encode()).digest()
        self.url = url
        self.registry_lru = OrderedDict()
        self.registry_lru_lock = threading.Lock()
        self.registry_lru_bytes = 0
        self.sqlite = url.startswith("sqlite")
        self._sqlite_writer = threading.Lock() if self.sqlite else None
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30} if self.sqlite else {},
        )
        try:
            current = startup_contract_current(self.engine, self.cipher)
            if not current:
                authenticate_store(self.engine, self.cipher)
        finally:
            # Authentication connections must not skip the runtime pragmas below.
            self.engine.dispose()
        if self.sqlite:

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA secure_delete=ON")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if current:
            return
        # create_all is additive for new tables; migrate metadata columns before mapped reads.
        with self.engine.begin() as connection:
            ensure_columns(connection, Base.metadata)
        Base.metadata.create_all(self.engine)
        self.encrypt_legacy_records()
        self.backfill_operational_columns()

    def encrypt_legacy_records(self):
        # A one-time transactional codec migration preserves IDs and human edits during an app update.
        changed = False
        with self.transaction() as session:
            for model in MODELS.values():
                session.scalar(
                    select(model).limit(1)
                )  # Authenticate an existing encrypted store before any rewrite.
                for obj in session.scalars(
                    select(model).where(model.data["od_encrypted"].as_integer().is_(None))
                ):
                    if (
                        obj.external_key
                        and model.__tablename__ in {"phone_endpoints", "number_inventory", "suppressions"}
                        and obj.external_key.startswith("+")
                    ):
                        obj.external_key = hmac.new(
                            self.identity_key, obj.external_key.encode(), hashlib.sha256
                        ).hexdigest()
                    flag_modified(obj, "data")
                    changed = True
            for model in (Event, Job):
                for obj in session.scalars(
                    select(model).where(model.payload["od_encrypted"].as_integer().is_(None))
                ):
                    flag_modified(obj, "payload")
                    changed = True
        if changed and self.sqlite:
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("VACUUM")
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    def backfill_operational_columns(self):
        while True:
            with self.transaction() as s:
                jobs = list(s.scalars(select(Job).where(Job.seller_id.is_(None)).limit(500)))
                if not jobs:
                    break
                for job in jobs:
                    job.seller_id = job.payload.get("seller_id") or ""
                    job.owner_id = job.payload.get("user_id") or ""
        for kind in sorted(HOT_TABLES | CONTEXT_FIELDS.keys()):
            model = MODELS[kind]
            version = model.op_index_version if kind in HOT_TABLES else model.ctx_index_version
            outdated = version.is_(None)
            if kind in {
                "command_outbox",
                "usage_ledger",
                "registry_snapshots",
                "call_attempts",
                "followup_tasks",
            }:
                outdated = outdated | (
                    version < (5 if kind == "call_attempts" else 4 if kind == "usage_ledger" else 2)
                )
            while True:
                with self.transaction() as s:
                    batch = list(s.scalars(select(model).where(outdated).limit(500)))
                    if not batch:
                        break
                    for row in batch:
                        if (
                            kind == "usage_ledger"
                            and row.data.get("kind") == "call_authorization"
                            and row.data.get("reconciled_to_provider_records")
                            and row.data.get("status") != "settled"
                        ):
                            # Older imports cleared the entire 5-cent call estimate on the
                            # first CDR. No complete-statement proof existed in that version.
                            update(
                                row,
                                status="uncertain",
                                cost_usd=None,
                                reserved_usd=row.data.get("estimate_usd", 0.05),
                                billing_verified=False,
                                reconciled_to_provider_records=False,
                                review_reason="Legacy partial receipt did not prove complete call charges",
                            )
                            audit(
                                s,
                                row.tenant_id,
                                row.seller_id,
                                "system-migration",
                                "billing.legacy_partial_reopened",
                                row.id,
                                utcnow(),
                            )
                        sync_columns(
                            row, self.identity_key
                        )  # Other index repairs preserve source content/version.
        self.backfill_work_dates()

    def backfill_work_dates(self):
        """A saved keyset cursor also terminates for legacy intervals with unknown dates."""
        sellers, work = MODELS["sellers"], MODELS["work_intervals"]
        last_seller = ""
        while True:
            with self.transaction(False) as session:
                identities = list(
                    session.scalars(
                        select(sellers.id).where(sellers.id > last_seller).order_by(sellers.id).limit(100)
                    )
                )
            if not identities:
                return
            for identity in identities:
                while True:
                    with self.transaction() as session:
                        seller = session.get(sellers, identity)
                        contracts = dict(seller.data.get("completion_contracts", {}))
                        marker = contracts.get("work_index_v1", {})
                        if marker.get("completed_at"):
                            break
                        rows = list(
                            session.scalars(
                                select(work)
                                .where(
                                    work.tenant_id == seller.tenant_id,
                                    work.seller_id == seller.id,
                                    work.id > marker.get("last_id", ""),
                                )
                                .order_by(work.id)
                                .limit(500)
                            )
                        )
                        for row in rows:
                            sync_columns(row, self.identity_key)
                        contracts["work_index_v1"] = {
                            "schema_version": 1,
                            "last_id": rows[-1].id if rows else marker.get("last_id", ""),
                            "completed_at": utcnow().isoformat() if len(rows) < 500 else None,
                        }
                        seller.data = {**seller.data, "completion_contracts": contracts}
                    if len(rows) < 500:
                        break
            last_seller = identities[-1]

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
        # PostgreSQL row/unique constraints and explicit seller authorization fences protect conflicts.
        with self.sessions() as session:
            context_token = _DATABASE_CIPHER.set(self.cipher)
            lookup_token = _DATABASE_LOOKUP_KEY.set(self.identity_key)
            session.info["write"] = write
            session.info["database"] = self
            admitted = False
            try:
                if write and self.sqlite:
                    if not self._sqlite_writer.acquire(timeout=30):
                        raise TimeoutError("SQLite writer admission timed out")
                    admitted = True
                    session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
            except Exception:
                for callback in session.info.pop("before_rollback", []):
                    callback()
                session.rollback()
                raise
            finally:
                _DATABASE_CIPHER.reset(context_token)
                _DATABASE_LOOKUP_KEY.reset(lookup_token)
                if admitted:
                    self._sqlite_writer.release()
                # A journal failure inside a caller-owned transaction must persist
                # its hold only after that transaction releases its write lock.
                for callback in session.info.pop("after_transaction", []):
                    callback()

    def authorize_stream(self, session, principal):
        if not self.sqlite:
            scope = principal["tenant_id"] + ":" + principal["seller_id"]
            key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:scope)"), {"scope": key})

    def reset(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def integrity(self):
        with self.transaction(False) as s:
            return (
                s.execute(text("PRAGMA integrity_check") if self.sqlite else text("SELECT 'ok'")).scalar()
                == "ok"
            )


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
            sync_columns(row, _DATABASE_LOOKUP_KEY.get())
    from app.reporting import invalidate_reports

    invalidate_reports(session)


def get(s, kind, object_id, tenant_id=None):
    obj = s.get(MODELS[kind], object_id)
    return obj if obj and (tenant_id is None or obj.tenant_id == tenant_id) else None


def put(s, kind, tenant_id, seller_id, data, *, object_id=None, key=None, now=None):
    if key and kind in {"phone_endpoints", "number_inventory", "suppressions"} and key.startswith("+"):
        # Lookup keys expose no phone digits at rest. The authoritative endpoint lives in encrypted JSON.
        cipher = _DATABASE_CIPHER.get()
        if cipher is None:
            raise ValueError("Encryption context missing")
        key = hmac.new(_DATABASE_LOOKUP_KEY.get(), key.encode(), hashlib.sha256).hexdigest()
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
                "_rep_id": obj.data.get("rep_id") if hasattr(obj, "data") else None,
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
