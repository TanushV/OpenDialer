import base64
import contextvars
import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, create_engine, event, select, text
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.types import TypeDecorator

_DATABASE_CIPHER = contextvars.ContextVar("opendialer_database_cipher", default=None)
_DATABASE_LOOKUP_KEY = contextvars.ContextVar("opendialer_database_lookup_key", default=None)


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
]
MODELS = {}
for _name in TABLES:
    MODELS[_name] = type(
        "".join(x.title() for x in _name.split("_")),
        (Document, Base),
        {
            "__tablename__": _name,
            "__table_args__": (
                UniqueConstraint("tenant_id", "seller_id", "external_key", name=f"uq_{_name}_key"),
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
    payload: Mapped[dict] = mapped_column(EncryptedJSON, default=dict)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
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
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)


class Database:
    def __init__(self, url, encryption_key=None):
        if not encryption_key:
            raise ValueError("A persistent authority encryption key is required")
        self.cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256((encryption_key + ":database:v1").encode()).digest())
        )
        self.identity_key = hashlib.sha256((encryption_key + ":lookup:v1").encode()).digest()
        self.url = url
        self.sqlite = url.startswith("sqlite")
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30} if self.sqlite else {},
        )
        if self.sqlite:

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA secure_delete=ON")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = threading.RLock()
        Base.metadata.create_all(self.engine)
        self.encrypt_legacy_records()

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

    @contextmanager
    def transaction(self, write=True):
        # SQLite has one writer; PostgreSQL uses a transaction-scoped fence. No network I/O in here.
        with self.lock, self.sessions() as session:
            context_token = _DATABASE_CIPHER.set(self.cipher)
            lookup_token = _DATABASE_LOOKUP_KEY.set(self.identity_key)
            try:
                if write:
                    session.execute(
                        text("BEGIN IMMEDIATE")
                        if self.sqlite
                        else text("SELECT pg_advisory_xact_lock(721025917)")
                    )
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                _DATABASE_CIPHER.reset(context_token)
                _DATABASE_LOOKUP_KEY.reset(lookup_token)

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
    return list(s.scalars(query))


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
    )
    s.add(obj)
    s.flush()
    return obj


def update(obj, **data):
    obj.data = {**obj.data, **data}
    obj.version += 1
    return obj


def doc(obj):
    return {"id": obj.id, "version": obj.version, **obj.data}


def emit(s, tenant_id, kind, obj, now, payload=None):
    s.add(
        Event(
            tenant_id=tenant_id,
            type=kind,
            entity_id=obj.id,
            entity_version=obj.version,
            occurred_at=now,
            payload=payload or {},
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
