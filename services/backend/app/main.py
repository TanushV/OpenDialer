import asyncio
import functools
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.administration import Dialer
from app.coaching import knowledge, library
from app.compliance.consent import ConsentChange
from app.compliance.consent import record as record_consent
from app.compliance.consent import replay as replay_consent
from app.compliance.operating import DNCAuthorization, bind_operating_api
from app.config import Settings
from app.db import Database, Event, audit, doc, put, rows, update
from app.measurement import WorkEdit
from app.providers.telnyx import verify_webhook
from app.rep_workflows import MeetingEdit, OutreachEdit, TaskEdit
from app.scheduling.models import ContactRole, OutcomeCode
from app.security import PASSWORDS, admin, decode_session, dummy_hash, issue_session
from app.service import fail
from app.telephony.callbacks import CallbackRoutingEdit


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoveryEdit(Strict):
    expected_version: int = Field(ge=1)
    action: Literal["reconcile", "end_call", "cancel_unsent", "confirm_not_placed"]
    provider_id: str | None = Field(default=None, min_length=1, max_length=2048)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def attested(self):
        if self.action == "confirm_not_placed" and not (self.reason or "").strip():
            raise ValueError("Record what carrier evidence shows the call was not placed")
        return self


class JobRetry(Strict):
    expected_attempts: int = Field(ge=0)


class IngressCheck(Strict):
    request_id: UUID
    expected_version: int = Field(ge=0)


class Setup(Strict):
    seller_name: str = Field(min_length=1, max_length=200)
    campaign_name: str = Field(default="My campaign", max_length=200)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=200)
    name: str = Field(default="Local operator", max_length=120)


class CoachPreferences(Strict):
    muted: bool | None = None
    pinned: bool | None = None


class CoachQuestion(Strict):
    question: str | None = Field(default=None, max_length=2000)
    urgent: bool = True
    request_id: UUID | None = None


class CoachCancel(Strict):
    request_id: UUID


class PracticeRequest(Strict):
    scenario: str = Field(min_length=1, max_length=2000)
    request_id: UUID | None = None


class Login(Strict):
    email: str
    password: str


class Presence(Strict):
    status: Literal["ready", "paused", "busy"]
    audio_ready: bool = False
    telephony_ready: bool = False
    agent_call_control_id: str | None = None
    pending_optouts: int = Field(default=0, ge=0)


class Heartbeat(Strict):
    activity: Literal["ready", "call", "wrapup", "idle"]


class QueueNext(Strict):
    campaign_id: str


class CallStart(Strict):
    reservation_id: str
    idempotency_key: str = Field(min_length=1, max_length=128)


class KeypadRequest(Strict):
    digits: str = Field(pattern=r"^[0-9*#]{1,30}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class Suppress(Strict):
    phone: str
    reason: Literal["opt_out", "wrong_party", "temporary_hold", "invalid_endpoint"] = "opt_out"
    event_id: str | None = None


class HoldRelease(Strict):
    phone: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500, pattern=r"\S")


class ImportLeads(Strict):
    campaign_id: str
    rows: list[dict] = Field(max_length=10000)


class Transcript(Strict):
    segment_id: str = Field(max_length=200)
    revision: int = Field(ge=1)
    side: Literal["rep", "remote", "unknown"]
    text: str = Field(max_length=20000)
    is_final: bool
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    speaker_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    system_audio: bool = False


class NoteEdit(Strict):
    # Current clients send the human edit version; finalization bookkeeping never moves it.
    expected_edit_version: int | None = Field(default=None, ge=0, strict=True)
    expected_version: int | None = None
    text: str = Field(max_length=20000)

    @model_validator(mode="after")
    def one_version(self):
        if self.expected_edit_version is None and self.expected_version is None:
            raise ValueError("expected_edit_version is required")
        return self


class Disposition(Strict):
    request_id: UUID | None = None
    contact_role: ContactRole = "unknown"
    outcome: OutcomeCode
    callback_at: AwareDatetime | None = None
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    # Rep-visible edit counter: background bookkeeping on the call row never conflicts.
    expected_edit_version: int | None = Field(default=None, ge=0, strict=True)
    callback_note: str = Field(default="", max_length=1000)


class Voicemail(Strict):
    asset_id: str
    detach: bool = False


class ResearchBatch(Strict):
    lead_ids: list[str] = Field(max_length=10000)


class SimulationEvent(Strict):
    event_id: str
    event_type: str
    call_id: str
    payload: dict = Field(default_factory=dict)


class Budget(Strict):
    daily_limit_usd: float = Field(ge=0, le=100000)
    monthly_limit_usd: float = Field(ge=0, le=1000000)
    number_purchase_limit_usd: float = Field(default=0, ge=0, le=10000)


class NumberInput(Strict):
    phone: str
    region: str | None = None
    lifecycle: str = "purchased"
    daily_limit: int = Field(default=5, ge=1, le=10000)
    capacity: int = Field(default=1, ge=1, le=20)


class PolicyUpdate(Strict):
    # The path names the version being edited; it must still be current (see publish).
    expected_version: int | None = Field(default=None, ge=1)
    approved: bool | None = None
    reason: str = Field(default="", max_length=1000)
    review_reference: str | None = None
    # National screening is mandatory and not a publishable setting.
    state_registry_required: bool | None = None
    consent_required: bool | None = None
    permissions: list[Literal["live", "prerecorded", "transcription", "recording", "messaging"]] | None = None
    local_start_hour: int | None = Field(default=None, ge=0, le=23)
    local_end_hour: int | None = Field(default=None, ge=1, le=24)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    min_retry_seconds: int | None = Field(default=None, ge=60)
    allowed_country_codes: list[str] | None = None
    voicemail_enabled: bool | None = None
    voicemail_optout_supported: bool | None = None
    approved_assets: list[str] | None = None
    detached_topology_verified: bool | None = None
    topology_test_reference: str | None = None
    late_human_recovery_destination: str | None = None


class ProviderSettings(Strict):
    request_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0)
    dnc_api_key: str | None = None
    dnc_seller_authorization: DNCAuthorization | None = None
    telnyx_api_key: str | None = None
    telnyx_connection_id: str | None = None
    telnyx_public_key: str | None = None
    telephony_credential_id: str | None = None
    sip_username: str | None = None
    webhook_url: str | None = None
    traffic_approval_reference: str | None = None
    openai_api_key: str | None = None
    inference_mode: Literal["api", "litellm"] | None = None
    inference_base_url: str | None = None
    model: str | None = None
    research_extract_model: str | None = None
    research_search: Literal["disabled", "openai_web_search"] | None = None
    script_write_model: str | None = None
    postcall_extract_model: str | None = None
    scorecard_review_model: str | None = None
    coach_provider: Literal["openrouter", "static"] | None = None
    openrouter_api_key: str | None = None
    coach_model: str | None = Field(default=None, min_length=3, max_length=200)


SECRET_FIELDS = {
    "dnc_api_key",
    "telnyx_api_key",
    "openai_api_key",
    "openrouter_api_key",
}
# Retired non-ZDR coaching settings stored by older releases: never echoed or reported.
RETIRED_FIELDS = {"groq_api_key", "groq_model"}
_BOUND_KEYS = {
    "minimum": "min",
    "maximum": "max",
    "exclusiveMinimum": "min_exclusive",
    "exclusiveMaximum": "max_exclusive",
    "minLength": "min_length",
    "maxLength": "max_length",
    "minItems": "min_items",
    "maxItems": "max_items",
}


def _bound(schema):
    out = {}
    for option in schema.get("anyOf", [schema]):
        if option.get("type") == "null":
            continue
        if "type" in option:
            out.setdefault("type", option["type"])
        out.update({name: option[key] for key, name in _BOUND_KEYS.items() if key in option})
        if "enum" in option or "const" in option:
            out["enum"] = option.get("enum", [option.get("const")])
        if isinstance(option.get("items"), dict) and (items := _bound(option["items"])):
            out["items"] = items
    return out


def field_bounds(model):
    """UI bounds read from the same pydantic definition that validates the write."""
    result = {}
    for name, schema in model.model_json_schema()["properties"].items():
        if name in {"request_id", "expected_version", "expected_campaign_version"}:
            continue  # Edit-envelope identities are protocol, not rendered settings.
        value = _bound(schema)
        if isinstance(schema.get("default"), (bool, int, float, str, list)):
            value["default"] = schema["default"]
        if value:
            result[name] = value
    return result


@functools.cache
def setting_bounds():
    from app.compliance.operating import DncRefreshPolicy, OperatingPolicy, ScreeningRequest
    from app.optimization.api import (
        Comparison,
        Conversion,
        HealthPolicy,
        ModelTrial,
        NumberReview,
        OptimizationSettings,
    )
    from app.preparation.templates import PreparationPolicy
    from app.scheduling.models import EstimatePolicy
    from app.telephony.callbacks import CallbackRoutingEdit

    return {
        name: field_bounds(model)
        for name, model in {
            "operating": OperatingPolicy,
            "dnc_refresh": DncRefreshPolicy,
            "preparation": PreparationPolicy,
            "optimization": OptimizationSettings,
            "health_policy": HealthPolicy,
            "estimate_policy": EstimatePolicy,
            "policy": PolicyUpdate,
            "budget": Budget,
            "callback_routing": CallbackRoutingEdit,
            "conversion": Conversion,
            "model_trial": ModelTrial,
            "comparison": Comparison,
            "number_review": NumberReview,
            "setup": Setup,
            "screening": ScreeningRequest,
        }.items()
    }


def create_app(settings=None, *, clock=None, carrier=None):
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not settings.secret_key:
        raise ValueError("A persistent OPENDIALER_SECRET_KEY is required; use the desktop or CLI bootstrap")
    clock = clock or (lambda: datetime.now(UTC))
    from app.migrate import migrate_business_state, prepare_database

    prepare_database(settings, clock)
    db = Database(settings.database_url, encryption_key=settings.secret_key)
    db.registry_lru_bytes = settings.runtime_limits.registry_lru_bytes
    service = Dialer(db, settings, clock, carrier)
    try:
        migrate_business_state(service)
        from app.compliance.suppressions import replay as replay_suppressions

        replay_suppressions(service)
        replay_consent(service)
        service.replay_effects()
    except Exception:
        db.engine.dispose()
        raise

    @asynccontextmanager
    async def lifespan(app):
        from app.runtime import authority_lifetime

        async with authority_lifetime(
            service, queues=settings.worker_queues, enabled=settings.worker_enabled
        ):
            yield

    app = FastAPI(title="OpenDialer authority", version=settings.version, lifespan=lifespan)
    app.state.db, app.state.service, app.state.settings = db, service, settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Local-Token", "X-Bootstrap-Token"],
        allow_credentials=False,
    )
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def boundaries(request, call_next):
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", 0)) > 5_000_000
        ):
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        host = request.headers.get("host", "").split(":")[0]
        if (
            settings.mode == "local"
            and request.url.path != "/webhooks/telnyx"
            and not (
                request.method == "GET"
                and re.fullmatch(r"/webhooks/ingress-check/[0-9a-f]{32}", request.url.path)
            )
            and host not in {"127.0.0.1", "localhost", "testserver", "["}
        ):
            return JSONResponse({"detail": "Local authority host rejected"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse({"detail": "Origin rejected"}, status_code=403)
        from app.priority import GATE

        if request.url.path == "/v1/admin/jobs/run":
            # An explicit drain of every lane is background work, not a calling request.
            response = await call_next(request)
        else:
            with GATE.work():  # Interactive requests run ahead of background lanes.
                response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(StaleDataError)
    @app.exception_handler(IntegrityError)
    async def constraint_error(request, error):
        return JSONResponse(
            {"detail": "Concurrent operation conflicts with an existing reservation or identity"},
            status_code=409,
        )

    def principal(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not credentials:
            fail(401, "Authentication required")
        return decode_session(db, settings, credentials.credentials, clock())

    def administrator(p=Depends(principal)):
        admin(p)
        return p

    bind_operating_api(app, service, administrator)
    from app.billing import bind_api as bind_billing_api

    bind_billing_api(app, service, administrator)
    from app.postcall.review import bind_api as bind_review_api
    from app.postcall.rubrics import bind_api as bind_rubrics_api

    bind_rubrics_api(app, service, principal, administrator)
    bind_review_api(app, service, principal)

    def local_nonce(value):
        if settings.mode != "local":
            fail(403, "Local session is unavailable in connected mode")
        if not settings.local_token or not secrets.compare_digest(value or "", settings.local_token):
            fail(401, "Local bootstrap nonce required")

    @app.get("/health")
    def health():
        with db.transaction(False) as s:
            initialized = bool(rows(s, "tenants"))
        return {
            "status": "ok",
            "version": settings.version,
            "mode": settings.mode,
            "simulation": settings.simulation,
            "initialized": initialized,
        }

    @app.post("/v1/setup")
    def setup(
        data: Setup,
        x_local_token: str | None = Header(default=None),
        x_bootstrap_token: str | None = Header(default=None),
    ):
        if settings.mode == "local":
            local_nonce(x_local_token)
        elif not settings.bootstrap_token or not secrets.compare_digest(
            x_bootstrap_token or "", settings.bootstrap_token
        ):
            fail(401, "Server bootstrap token required")
        return service.setup(data.model_dump())

    @app.post("/v1/local/session")
    def local_session(x_local_token: str | None = Header(default=None)):
        local_nonce(x_local_token)
        with db.transaction() as s:
            users = rows(s, "users")
            if not users:
                fail(409, "Workspace setup required")
            user = next(u for u in users if u.data["role"] == "owner")
            return issue_session(s, settings, user, clock())

    login_failures = {}

    @app.post("/v1/auth/session")
    def login(data: Login, request: Request):
        key = hashlib.sha256(
            (data.email.casefold() + ":" + (request.client.host if request.client else "unknown")).encode()
        ).hexdigest()
        failures = [at for at in login_failures.get(key, []) if time.monotonic() - at < 300]
        if len(failures) >= 10:
            fail(429, "Too many login attempts; retry later")
        with db.transaction() as s:
            user = next((u for u in rows(s, "users") if u.data["email"] == data.email.casefold()), None)
            fallback = dummy_hash()  # Requested for every login, known account or not.
            verified = PASSWORDS.verify(data.password, user.data["password_hash"] if user else fallback)
            if not verified or not user or user.data.get("disabled"):
                login_failures[key] = [*failures, time.monotonic()]
                fail(401, "Email or password is invalid")
            login_failures.pop(key, None)
            return issue_session(s, settings, user, clock())

    def logout_principal(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not credentials:
            fail(401, "Authentication required")
        # This dependency is exclusive to logout, never a general authentication
        # bypass. The signed original session must own a committed logout receipt.
        return decode_session(db, settings, credentials.credentials, clock(), logout_receipt_only=True)

    @app.post("/v1/auth/logout")
    def logout(p=Depends(logout_principal)):
        with db.transaction() as s:
            db.authorize_stream(s, p)
            status = service.quiescence_in(s, p)
            if not status["can_disconnect"]:
                fail(409, {"code": "AUTHORITY_HAS_OBLIGATIONS", "reasons": status["reasons"]})
            service.release_unused_reservation_in(s, p)
            service.record_work_in(s, p, False)
            session = service.required(s, "sessions", p["session_id"], p)
            if not session.data.get("logout_completed_at"):
                update(session, revoked=True, logout_completed_at=clock().isoformat())
        return {"logged_out": True}

    @app.get("/v1/auth/quiescence")
    def quiescence(p=Depends(principal)):
        with db.transaction(False) as s:
            return service.quiescence_in(s, p)

    @app.get("/v1/workspace")
    def workspace(p=Depends(principal)):
        return service.snapshot(p)

    @app.get("/v1/workspace/{section}")
    def workspace_page(
        section: str, cursor: str | None = None, q: str = "", filter: str = "all", p=Depends(principal)
    ):
        return service.workspace_page(p, section, cursor=cursor, q=q, filter=filter)

    @app.get("/v1/metrics")
    def metrics(p=Depends(principal)):
        return service.cached_home_metrics(p)

    @app.post("/v1/simulation/seed")
    def seed(p=Depends(administrator)):
        return service.seed(p)

    @app.post("/v1/rep/presence")
    def presence(data: Presence, p=Depends(principal)):
        return service.presence(p, data.model_dump())

    @app.post("/v1/rep/heartbeat")
    def heartbeat(data: Heartbeat, p=Depends(principal)):
        return service.heartbeat(p, data.activity)

    @app.get("/v1/rep/work")
    def work_intervals(p=Depends(principal)):
        return service.work_intervals(p)

    @app.patch("/v1/rep/work/{interval_id}")
    def edit_work(interval_id: str, data: WorkEdit, p=Depends(principal)):
        return service.edit_work(p, interval_id, data.model_dump())

    @app.post("/v1/queue/next")
    def next_lead(data: QueueNext, p=Depends(principal)):
        return service.reserve(p, data.campaign_id)

    @app.post("/v1/calls/start")
    async def start_call(data: CallStart, p=Depends(principal)):
        return await service.start_call(p, data.model_dump())

    @app.get("/v1/calls/{call_id}")
    def get_call(call_id: str, p=Depends(principal)):
        from app.coaching.context import upgrade_state
        from app.postcall.analysis import suggested_outcome
        from app.postcall.rubrics import effective

        with db.transaction(False) as s:
            call = service.required(s, "call_attempts", call_id, p)
            if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                fail(404, "Object not found")
            state = service._by_key(s, p, "conversation_states", call_id)
            state = state.data if state else {}
            derived = upgrade_state(state, call_id=call_id, rubric=effective(call.data))
            coach = service.visible_help(s, p, call, state)
            segments = service.transcript_rows(s, p, call_id)
            return {
                **{k: v for k, v in doc(call).items() if k != "recording_url"},
                "transcript": segments,
                "raw_evidence_status": service.raw_status(call, segments),
                "coach": coach,
                "conversation_state": state,
                "rundown_status": derived["finalization"]["status"],
                "suggested_outcome": suggested_outcome(derived),
            }

    @app.post("/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, p=Depends(principal)):
        return await service.hangup(p, call_id)

    @app.post("/v1/calls/{call_id}/disposition")
    def disposition(call_id: str, data: Disposition, p=Depends(principal)):
        return service.save_disposition(p, call_id, data.model_dump(mode="json"))

    @app.post("/v1/leads/{lead_id}/meeting")
    def report_meeting(lead_id: str, data: MeetingEdit, p=Depends(principal)):
        return service.report_meeting(p, lead_id, data.model_dump(mode="json", exclude_unset=True))

    @app.post("/v1/suppressions")
    def suppress(data: Suppress, p=Depends(principal)):
        return service.suppress(p, data.phone, data.reason, event_id=data.event_id)

    @app.post("/v1/admin/suppressions/release-hold")
    def release_hold(data: HoldRelease, p=Depends(administrator)):
        from app.compliance.suppressions import release_hold

        return release_hold(service, p, data.phone, data.reason)

    @app.post("/v1/leads/import")
    def import_leads(data: ImportLeads, p=Depends(administrator)):
        return service.import_leads(p, data.model_dump())

    @app.get("/v1/leads/{lead_id}/packet")
    def packet(lead_id: str, p=Depends(principal)):
        return service.packet(p, lead_id)

    @app.get("/v1/preparation")
    def preparation(p=Depends(principal)):
        return service.preparation_status(p)

    @app.post("/v1/research/batches")
    def research(data: ResearchBatch, p=Depends(administrator)):
        return service.queue_research(p, data.lead_ids)

    @app.post("/v1/calls/{call_id}/transcript")
    async def transcript(call_id: str, data: Transcript, p=Depends(principal)):
        # Synthetic/manual corrections only. Live machine ingestion is signature-verified via webhooks.
        if not settings.simulation and p["role"] not in {"owner", "admin"}:
            fail(403, "Transcript correction requires administrator review")
        return await service.transcript(p, call_id, data.model_dump())

    @app.patch("/v1/calls/{call_id}/coach")
    def coach_preferences(call_id: str, data: CoachPreferences, p=Depends(principal)):
        with db.transaction() as s:
            call = service.required(s, "call_attempts", call_id, p)
            preferences = {**call.data.get("coach_preferences", {}), **data.model_dump(exclude_none=True)}
            if data.pinned is not None:
                state = service._by_key(s, p, "conversation_states", call_id)
                visible = service.visible_help(s, p, call, state.data if state else {})
                preferences["pinned_event_id"] = visible["id"] if data.pinned and visible else None
            update(call, coach_preferences=preferences)
            return preferences

    @app.post("/v1/calls/{call_id}/coach")
    async def coach(call_id: str, data: CoachQuestion | None = None, p=Depends(principal)):
        return await service.coach_turn(
            p,
            call_id,
            urgent=True,
            question=data.question if data else None,
            request_id=str(data.request_id) if data and data.request_id else None,
        )

    @app.post("/v1/calls/{call_id}/coach/cancel")
    def cancel_coach(call_id: str, data: CoachCancel, p=Depends(principal)):
        return service.cancel_help(p, call_id, str(data.request_id))

    @app.post("/v1/coaching/practice")
    async def practice(data: PracticeRequest, p=Depends(principal)):
        return await service.practice(
            p, data.scenario, request_id=str(data.request_id) if data.request_id else None
        )

    @app.get("/v1/coaching")
    def coaching(p=Depends(principal)):
        return library.overview(service, p)

    @app.put("/v1/coaching/goals")
    def coaching_goals(data: library.GoalsEdit, p=Depends(principal)):
        return library.save_goals(service, p, data.model_dump(mode="json"))

    @app.get("/v1/calls/{call_id}/analysis")
    def analysis(call_id: str, p=Depends(principal)):
        return service.analysis(p, call_id)

    @app.patch("/v1/calls/{call_id}/notes")
    def edit_notes(call_id: str, data: NoteEdit, p=Depends(principal)):
        return service.edit_notes(
            p, call_id, data.expected_version, data.text, expected_edit_version=data.expected_edit_version
        )

    @app.post("/v1/calls/{call_id}/comments")
    def comment(call_id: str, data: dict, p=Depends(principal)):
        with db.transaction() as s:
            service.required(s, "call_attempts", call_id, p)
            return doc(
                put(
                    s,
                    "comments",
                    p["tenant_id"],
                    p["seller_id"],
                    {"call_id": call_id, "author_id": p["user_id"], "text": str(data.get("text", ""))[:4000]},
                    now=clock(),
                )
            )

    @app.get("/v1/events")
    def events(after: int = 0, p=Depends(principal)):
        with db.transaction(False) as s:
            found = list(
                s.scalars(
                    select(Event)
                    .where(Event.tenant_id == p["tenant_id"], Event.sequence > after)
                    .order_by(Event.sequence)
                    .limit(500)
                )
            )

            def visible(e):
                from app.db import MODELS

                seller = e.payload.get("_seller_id")
                rep = e.payload.get("_rep_id")
                if seller is None:
                    # Old release events lack scope metadata. Resolve their entity, otherwise fail closed.
                    for model in MODELS.values():
                        target = s.get(model, e.entity_id)
                        if target and target.tenant_id == p["tenant_id"]:
                            seller, rep = target.seller_id, target.data.get("rep_id")
                            break
                return seller == p["seller_id"] and (p["role"] != "rep" or not rep or rep == p["user_id"])

            result = [
                {
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "tenant_id": e.tenant_id,
                    "type": e.type,
                    "entity_id": e.entity_id,
                    "entity_version": e.entity_version,
                    "occurred_at": e.occurred_at.isoformat(),
                    "payload": {k: v for k, v in e.payload.items() if not k.startswith("_")},
                }
                for e in found
                if visible(e)
            ]
            return {
                "events": result,
                "cursor": found[-1].sequence if found else after,
                "server_time": clock().isoformat(),
            }

    @app.websocket("/v1/live")
    async def live(ws: WebSocket):
        if ws.headers.get("origin") and ws.headers["origin"] not in settings.allowed_origins:
            await ws.close(code=4403)
            return
        await ws.accept()
        incoming = None
        try:
            auth = await asyncio.wait_for(ws.receive_json(), 10)
            p = decode_session(db, settings, auth.get("token", ""), clock())
            cursor = int(auth.get("after", 0))
            while True:
                await asyncio.to_thread(decode_session, db, settings, auth["token"], clock())
                batch = await asyncio.to_thread(events, cursor, p)
                if batch["events"] or batch["cursor"] != cursor:
                    await ws.send_json(batch)
                cursor = batch["cursor"]
                # Receive disconnect even when no new event is available to send.
                # Otherwise quiet closed sockets keep polling and prevent shutdown.
                incoming = incoming or asyncio.create_task(ws.receive())
                done, _ = await asyncio.wait({incoming}, timeout=0.1)
                if done:
                    message = incoming.result()
                    incoming = None
                    if message["type"] != "websocket.disconnect":
                        await ws.close(code=1008)
                    return
        except (WebSocketDisconnect, HTTPException, TimeoutError, ValueError):
            try:
                await ws.close(code=4401)
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if incoming is not None:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)

    @app.post("/v1/simulation/event")
    def simulation_event(data: SimulationEvent, p=Depends(principal)):
        if not settings.simulation:
            fail(403, "Simulation is disabled")
        return service.event(p, data.event_id, data.event_type, data.call_id, data.payload)

    @app.post("/v1/telephony/token")
    async def telephony_token(p=Depends(principal)):
        def context():
            provider = service.carrier(p["tenant_id"])
            conf = service.vault.read(p["tenant_id"])
            admin = p["role"] in {"owner", "admin"}
            with db.transaction(False) as s:
                user = service.required(s, "users", p["user_id"], p)
                credential = user.data.get("telephony_credential_id") or (
                    conf.get("telephony_credential_id") if admin else None
                )
                sip = user.data.get("sip_username") or (conf.get("sip_username") if admin else None)
            return provider, credential, sip

        provider, credential, sip = await asyncio.to_thread(context)
        if not credential and not settings.simulation:
            fail(503, "Scoped rep telephony credential is not configured")
        if not settings.simulation:
            await service.verified_credential(provider, credential, sip)
        return {"token": await provider.token(credential or "simulated"), "simulation": settings.simulation}

    @app.post("/v1/admin/policies/{policy_id}/publish")
    def publish(policy_id: str, data: PolicyUpdate, p=Depends(administrator)):
        body = data.model_dump(exclude_none=True)
        expected = body.pop("expected_version", None)
        # Versions are immutable: an edit of anything but the current one would silently
        # replace a newer publication. Concurrent publishes are also fenced by the
        # campaign row version inside the publication transaction.
        with db.transaction(False) as s:
            policy = service.required(s, "policy_versions", policy_id, p)
            campaign = service.required(s, "campaigns", policy.data["campaign_id"], p)
            if campaign.data["policy_id"] != policy.id or expected not in {None, policy.version}:
                fail(
                    409,
                    "POLICY_VERSION_CONFLICT: a newer calling policy is current; reload it before publishing",
                )
        return service.publish(p, policy_id, body)

    @app.get("/v1/admin/preflight")
    def preflight(deep: bool = False, p=Depends(administrator)):
        return service.preflight(p, deep=deep)

    @app.post("/v1/admin/ingress-check")
    async def ingress_check(data: IngressCheck, p=Depends(administrator)):
        return await service.check_ingress(p, data.model_dump(mode="json"))

    @app.get("/webhooks/ingress-check/{nonce}", response_class=PlainTextResponse)
    def ingress_challenge(nonce: str):
        return service.ingress_challenge(nonce)

    @app.get("/v1/admin/callback-routing")
    def callback_routing(p=Depends(administrator)):
        return service.read_callback_routing(p)

    @app.put("/v1/admin/callback-routing")
    def write_callback_routing(data: CallbackRoutingEdit, p=Depends(administrator)):
        return service.edit_callback_routing(p, data.model_dump(exclude_none=True))

    @app.post("/v1/callbacks/{task_id}/hangup")
    async def end_callback(task_id: str, p=Depends(principal)):
        result = await asyncio.to_thread(service.end_inbound, p, task_id)
        await service.dispatch_controls(p)
        return result

    @app.get("/v1/definitions")
    def read_definitions(p=Depends(principal)):
        # Schema bounds and enums only: nothing seller-specific, so shared forms read them too.
        return {"bounds": setting_bounds()}

    @app.get("/v1/admin/settings")
    def read_settings(p=Depends(administrator)):
        from app.compliance.operating import OperatingPolicy
        from app.config import PROVIDER_DEFAULTS, inference_mode, inference_model
        from app.optimization.api import HealthPolicy
        from app.optimization.workflows import DEFAULTS as OPTIMIZATION_DEFAULTS
        from app.preparation.templates import PreparationPolicy
        from app.scheduling.models import EstimatePolicy

        config = service.vault.read(p["tenant_id"])
        # A route retired by this release (the ChatGPT-plan adapter) is reported, never
        # offered as the current choice: saving the form cannot resubmit an invalid value.
        mode = inference_mode(config)
        retired = {} if mode else {"inference_mode": config.pop("inference_mode")}
        with db.transaction(False) as s:
            budget = service.scoped(s, "budgets", p)[0]
            budget_values = Budget.model_validate(
                {key: budget.data[key] for key in Budget.model_fields}
            ).model_dump()
            # Current campaign goal and weekly schedule for the setup checklist.
            optimization = service.optimization_config(s, p)
        return {
            **{
                k: v
                for k, v in config.items()
                if k not in SECRET_FIELDS | RETIRED_FIELDS and not k.startswith("_")
            },
            "configured": {k: bool(config.get(k)) for k in SECRET_FIELDS},
            "version": config.get("version", 0),
            "budget": budget_values,
            "optimization": optimization,
            **({"retired": retired} if retired else {}),
            "effective": {
                **{
                    key: config.get(key, getattr(PROVIDER_DEFAULTS, key))
                    for key in ("coach_provider", "coach_model", "research_search")
                },
                "inference_mode": mode,
                "model": inference_model(config),
                **{
                    role + "_model": inference_model(config, role)
                    for role in ("research_extract", "script_write", "scorecard_review")
                },
            },
            "defaults": {
                "providers": PROVIDER_DEFAULTS.model_dump(mode="json"),
                "runtime": settings.runtime_limits.model_dump(mode="json"),
                "operating": OperatingPolicy().model_dump(mode="json"),
                "preparation": PreparationPolicy().model_dump(mode="json"),
                "optimization": {
                    **OPTIMIZATION_DEFAULTS,
                    "health_policy": HealthPolicy().model_dump(),
                    "estimate_policy": EstimatePolicy().model_dump(mode="json"),
                },
                "bounds": setting_bounds(),
            },
        }

    @app.put("/v1/admin/settings")
    def write_settings(data: ProviderSettings, p=Depends(administrator)):
        values = data.model_dump(exclude_none=True)
        request_id = values.pop("request_id", None)
        expected_version = values.pop("expected_version", None)
        if (request_id is None) != (expected_version is None):
            fail(422, "Provider edits require request_id and expected_version together")
        if values.get("dnc_seller_authorization"):
            values["dnc_seller_authorization"]["seller_id"] = p["seller_id"]
        from urllib.parse import urlsplit

        for key in ("webhook_url", "inference_base_url"):
            if values.get(key):
                try:
                    parsed = urlsplit(values[key])
                    _ = parsed.port
                except ValueError:
                    fail(422, "Endpoint requires a valid host and port")
                if (
                    parsed.username
                    or parsed.password
                    or parsed.scheme not in {"https", "http"}
                    or not parsed.hostname
                ):
                    fail(422, "Endpoint requires a valid HTTP(S) URL without embedded credentials")
                if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    fail(422, "Remote provider endpoints require HTTPS")
        with db.transaction() as s:
            db.authorize_stream(s, p)
            service._restore_operator(s, p)
            service.vault.write(
                p["tenant_id"],
                values,
                advance_version=True,
                edit=(str(request_id), expected_version) if request_id else None,
            )
            service.capability_cache.clear()
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "providers.configured",
                p["tenant_id"],
                clock(),
                {"fields": sorted(values)},
            )
        return read_settings(p)

    @app.put("/v1/admin/budget")
    def budget(data: Budget, p=Depends(administrator)):
        with db.transaction() as s:
            obj = service.scoped(s, "budgets", p)[0]
            update(obj, **data.model_dump())
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "budget.updated", obj.id, clock())
            return doc(obj)

    @app.post("/v1/admin/numbers")
    def number(data: NumberInput, p=Depends(administrator)):
        return service.add_number(p, data.model_dump(exclude_none=True))

    @app.get("/v1/admin/numbers/summary")
    def numbers_summary(p=Depends(administrator)):
        """Whole-inventory counts and the saved capacity plan. Reading never queues planning."""
        from collections import Counter

        from sqlalchemy import func, or_

        from app.db import MODELS, Job

        with db.transaction(False) as s:
            numbers = service.scoped(s, "number_inventory", p)
            lifecycles = Counter(n.data["lifecycle"] for n in numbers)
            ready = sum(bool(service.number_readiness_in(s, p, n)["outbound_ready"]) for n in numbers)
            binding = MODELS["lead_number_bindings"]
            stable = s.scalar(
                select(func.count())
                .select_from(binding)
                .where(binding.tenant_id == p["tenant_id"], binding.seller_id == p["seller_id"])
            )
            forecast = service.saved_forecast(s, p)
            campaign = service.selected_campaign(s, p)
            job = s.scalar(
                select(Job)
                .where(
                    Job.tenant_id == p["tenant_id"],
                    or_(
                        Job.key.startswith(f"planning:{campaign.id}:configured:", autoescape=True),
                        Job.key == "optimization-work:forecast_refresh:" + campaign.id,
                    ),
                    Job.status.in_(["queued", "running", "dead"]),
                )
                .order_by(Job.available_at.desc(), Job.id)
                .limit(1)
            )
        forecast["projection"].update(
            job_id=job.id if job else None,
            refresh_status=job.status if job else None,
            refresh_error=job.error if job else None,
            attempts=job.attempts if job else None,
        )
        return {
            "inventory": {
                "total": len(numbers),
                "ready": ready,
                "pending_setup": sum(
                    lifecycles[k] for k in ("purchased", "identity_configured", "callback_tested")
                ),
                "limited_use": lifecycles["limited_real_use"],
                "paused_outbound": lifecycles["paused_for_outbound"] + lifecycles["retired_callback_only"],
                "investigating": lifecycles["investigating"],
                "lifecycles": dict(lifecycles),
            },
            "stable_assignments": stable,
            "forecast": forecast,
        }

    @app.patch("/v1/admin/numbers/{number_id}")
    def edit_number(number_id: str, data: dict, p=Depends(administrator)):
        allowed = {
            "lifecycle",
            "reason",
            "daily_limit",
            "capacity",
            "identity_configured",
            "callback_tested",
            "registration_reference",
            "callback_test_reference",
            "real_use_review_reference",
            "request_id",
            "expected_version",
            "callback_task_id",
            "external_reputation",
            "reassignment",
        }
        if set(data) - allowed:
            fail(422, "Unrecognized number settings")
        return service.edit_number(p, number_id, data)

    @app.post("/v1/admin/numbers/proposal")
    def number_proposal(data: dict, p=Depends(administrator)):
        return service.proposal(p, data["campaign_id"], data.get("daily_capacity_per_number"))

    @app.post("/v1/admin/numbers/sync")
    async def sync_numbers(p=Depends(administrator)):
        return await service.sync_numbers(p)

    @app.post("/v1/admin/numbers/{number_id}/configure")
    async def configure_number(number_id: str, data: dict | None = None, p=Depends(administrator)):
        return await service.configure_owned_number(p, number_id, data)

    @app.post("/v1/admin/numbers/search")
    async def search_numbers(data: dict, p=Depends(administrator)):
        from decimal import Decimal, InvalidOperation

        region = str(data.get("region", ""))
        if not re.fullmatch(r"[2-9][0-9]{2}", region):
            fail(422, "A US area code is required")
        found = await service.carrier(p["tenant_id"]).search_numbers(region)

        def save_quotes():
            quotes = []
            with db.transaction() as s:
                for candidate in found[:10]:
                    cost = candidate.get("cost_information", {})
                    try:
                        upfront, monthly = Decimal(cost["upfront_cost"]), Decimal(cost["monthly_cost"])
                        if not upfront.is_finite() or not monthly.is_finite() or min(upfront, monthly) < 0:
                            continue
                    except (KeyError, InvalidOperation, TypeError):
                        continue
                    phone = candidate.get("phone_number", "")
                    if cost.get("currency") != "USD" or not re.fullmatch(r"\+1[2-9][0-9]{9}", phone):
                        continue
                    value = {
                        "phone": phone,
                        "region": region,
                        "monthly_cost_usd": float(monthly),
                        "initial_commitment_usd": float(upfront + monthly),
                        "expires_at": (clock() + timedelta(minutes=5)).isoformat(),
                        "currency": "USD",
                    }
                    put(
                        s,
                        "command_outbox",
                        p["tenant_id"],
                        p["seller_id"],
                        {"kind": "number_quote", "status": "quoted", **value},
                        now=clock(),
                    )
                    quotes.append(value)
            return quotes

        quotes = await asyncio.to_thread(save_quotes)
        return {"numbers": quotes, "simulation": settings.simulation}

    @app.post("/v1/admin/numbers/purchase")
    async def purchase_numbers(data: dict, p=Depends(administrator)):
        fail(
            403,
            "RECURRING_COMMITMENT_DISABLED: new numbers incur rental fees; reuse existing authorized inventory",
        )

    @app.get("/v1/admin/numbers/orders")
    def number_orders(p=Depends(administrator)):
        return service.number_orders(p)

    @app.post("/v1/admin/numbers/orders/{intent_id}/reconcile")
    async def reconcile_number_order(intent_id: str, data: dict, p=Depends(administrator)):
        return await service.reconcile_number_order(p, intent_id, data)

    @app.post("/v1/admin/variants")
    def create_variant(data: dict, p=Depends(administrator)):
        opener = data.get("opener", "")
        with db.transaction() as s:
            service.required(s, "campaigns", data.get("campaign_id", ""), p)
            seller = service.required(s, "sellers", p["seller_id"], p)
            # The administrator's reviewed publication is the approval; no client flag adds trust.
            if (
                not data.get("review_reference")
                or not isinstance(opener, str)
                or not 10 <= len(opener) <= 500
                or seller.data["name"].casefold() not in opener.casefold()
                or any(
                    term in opener.casefold()
                    for term in ["guarantee", "we spoke before", "double your revenue"]
                )
            ):
                fail(422, "Reviewed truthful language with the approved business identity is required")
            variant = put(
                s,
                "treatment_variants",
                p["tenant_id"],
                p["seller_id"],
                {
                    "campaign_id": data["campaign_id"],
                    "name": str(data.get("name", "Variant"))[:100],
                    "opener": opener,
                    "approved": True,
                    "review_reference": str(data["review_reference"])[:1000],
                    "reviewed_by": p["user_id"],
                },
                now=clock(),
            )
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "variant.approved", variant.id, clock())
            return doc(variant)

    @app.post("/v1/admin/recovery")
    async def recovery(p=Depends(administrator)):
        return await service.recover(p)

    @app.get("/v1/admin/recovery")
    def recovery_items(p=Depends(administrator)):
        return service.recovery_items(p)

    @app.post("/v1/admin/recovery/commands/{identity}")
    async def recovery_action(identity: str, data: RecoveryEdit, p=Depends(administrator)):
        return await service.recovery_action(p, identity, **data.model_dump())

    @app.post("/v1/admin/recovery/agents/{identity}")
    async def recover_agent(identity: str, data: RecoveryEdit, p=Depends(administrator)):
        if data.action != "reconcile":
            fail(422, "Agent recovery requires carrier reconciliation")
        return await service.reconcile_agent(p, identity, data.expected_version, data.provider_id)

    @app.post("/v1/admin/recovery/jobs/{identity}/retry")
    def retry_job(identity: str, data: JobRetry, p=Depends(administrator)):
        return service.retry_job(p, identity, data.expected_attempts)

    @app.post("/v1/admin/jobs/run")
    async def run_jobs(p=Depends(administrator)):
        return await service.run_jobs(p["tenant_id"])

    @app.post("/v1/admin/backup")
    def backup(p=Depends(administrator)):
        return service.backup(p)

    @app.post("/v1/admin/restore")
    def restore(data: dict, p=Depends(administrator)):
        return service.restore(p, data.get("backup_id", ""))

    @app.post("/v1/admin/users")
    def create_user(data: dict, p=Depends(administrator)):
        if data.get("role") not in {"rep", "admin"} or len(data.get("password", "")) < 12:
            fail(422, "Role rep/admin and a password of at least 12 characters are required")
        with db.transaction() as s:
            if any(u.data["email"] == data["email"].casefold() for u in rows(s, "users")):
                fail(409, "Email already registered")
            user = put(
                s,
                "users",
                p["tenant_id"],
                p["seller_id"],
                {
                    "email": data["email"].casefold(),
                    "name": data.get("name", "Rep"),
                    "role": data["role"],
                    "password_hash": PASSWORDS.hash(data["password"]),
                    "disabled": False,
                    "presence": {"status": "paused"},
                    "telephony_credential_id": data.get("telephony_credential_id"),
                    "sip_username": data.get("sip_username"),
                },
                now=clock(),
            )
            return {
                "id": user.id,
                "name": user.data["name"],
                "role": user.data["role"],
                "email": user.data["email"],
            }

    @app.post("/v1/admin/workspaces")
    def create_workspace(data: Setup, p=Depends(administrator)):
        if not p.get("platform_admin"):
            fail(403, "Platform administrator required")
        return service.setup(data.model_dump(), first=False)

    @app.post("/v1/admin/models/{model_id}/activate")
    def activate(model_id: str, data: dict, p=Depends(administrator)):
        from app.ml.training import predict_funnel, promotion_allowed, retired_reason

        with db.transaction() as s:
            model = service.required(s, "model_versions", model_id, p)
            # This authenticated administrator request is the manual approval. Every evidence
            # gate comes from the stored, report-derived review; the request body adds nothing.
            candidate = {**model.data, "manual_approved": True}
            if not promotion_allowed(candidate):
                fail(409, "Model has not passed calibration, online trial, coverage and policy gates")
            if not service.model_source_current(
                s, p, model, service.optimization_config(s, p)
            ) or not predict_funnel(model.data, {}, service.settings.data_dir / "models"):
                fail(
                    409,
                    retired_reason(model.data)
                    or "Model source, evaluated baseline or owned artifact changed; evaluate current settings",
                )
            for previous in service.scoped(s, "model_versions", p):
                if previous.data.get("active"):
                    update(previous, active=False, trial_active=False)
            update(model, **{**candidate, "active": True, "trial_active": False, "ever_activated": True})
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "model.activated", model.id, clock())
            return doc(model)

    @app.post("/v1/calls/{call_id}/voicemail")
    async def voicemail(call_id: str, data: Voicemail, p=Depends(principal)):
        def prepare():
            with db.transaction() as s:
                service.db.authorize_stream(s, p)
                call = service.required(s, "call_attempts", call_id, p)
                if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                    fail(403, "Call belongs to another rep")
                if call.data["state"] != "VOICEMAIL_READY":
                    fail(409, "MAILBOX_NOT_READY")
                reasons = service.voicemail_reasons(s, p, call, data.asset_id, data.detach)
                if reasons:
                    fail(403, {"code": "VOICEMAIL_DENIED", "reasons": reasons})
                update(
                    call,
                    state="VM_PENDING",
                    detach_requested=data.detach,
                    voicemail_asset=data.asset_id,
                    voicemail_deadline=(clock() + timedelta(seconds=120)).isoformat(),
                )
                result = doc(call)
                service.control_intent(
                    s,
                    p,
                    call_id,
                    "gather",
                    {
                        "minimum_digits": 1,
                        "maximum_digits": 1,
                        "valid_digits": "9",
                        "timeout_millis": 60000,
                        "initial_timeout_millis": 60000,
                        "gather_id": call_id,
                    },
                    suffix="voicemail-optout",
                )
                service.control_intent(
                    s,
                    p,
                    call_id,
                    "playback_start",
                    {"media_name": data.asset_id, "target_legs": "self"},
                    suffix="voicemail:" + data.asset_id,
                )
            return result

        result = await asyncio.to_thread(prepare)
        await service.dispatch_controls(p)
        return result

    @app.post("/webhooks/telnyx")
    async def webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("telnyx-signature-ed25519", "")
        timestamp = request.headers.get("telnyx-timestamp", "")

        def authenticated_tenant():
            with db.transaction(False) as s:
                tenants = rows(s, "tenants")
            owner = next(
                (
                    t
                    for t in tenants
                    if verify_webhook(
                        body,
                        signature,
                        timestamp,
                        service.vault.read(t.tenant_id).get("telnyx_public_key", ""),
                        now=clock(),
                    )
                ),
                None,
            )
            return owner.tenant_id if owner else None

        tenant_id = await asyncio.to_thread(authenticated_tenant)
        if not tenant_id:
            fail(401, "Webhook signature invalid or stale")
        try:
            event = json.loads(body)["data"]
        except (ValueError, KeyError, TypeError):
            fail(422, "Invalid provider event")
        return await service.accept_webhook(tenant_id, event, hashlib.sha256(body).hexdigest(), verified=True)

    @app.post("/v1/admin/registry/import")
    def registry_import(data: dict, p=Depends(administrator)):
        from app.compliance.registry import publish_registry

        return publish_registry(service, p, data)

    @app.patch("/v1/leads/{lead_id}/endpoint")
    def correct_endpoint(lead_id: str, data: dict, p=Depends(administrator)):
        from app.compliance.phone import normalize_phone

        phone = normalize_phone(data.get("phone", ""))
        if not phone.e164 or not data.get("evidence"):
            fail(422, "A valid replacement endpoint and evidence reference are required")
        with db.transaction() as session:
            lead = service.required(session, "leads", lead_id, p)
            endpoint = next(
                (r for r in service.scoped(session, "phone_endpoints", p) if r.data["phone"] == phone.e164),
                None,
            )
            if not endpoint:
                endpoint = put(
                    session,
                    "phone_endpoints",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "phone": phone.e164,
                        "extension": phone.extension,
                        "format_status": "valid",
                        "assignment_evidence": "unknown",
                        "ownership_status": "likely_match",
                        "reachability_status": "unknown",
                        "evidence_reference": str(data["evidence"])[:1000],
                    },
                    key=phone.e164,
                    now=clock(),
                )
            old_endpoint = lead.data["endpoint_id"]
            update(
                lead,
                endpoint_id=endpoint.id,
                phone=phone.e164,
                status="eligible",
                next_attempt_at=clock().isoformat(),
            )
            lead.external_key = lead.data["campaign_id"] + ":" + endpoint.id
            put(
                session,
                "lead_phone_links",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "endpoint_id": endpoint.id,
                    "previous_endpoint_id": old_endpoint,
                    "evidence": str(data["evidence"])[:1000],
                },
                now=clock(),
            )
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "lead.endpoint_corrected",
                lead.id,
                clock(),
            )
            return doc(lead)

    @app.post("/v1/admin/playbooks")
    def publish_playbook(data: knowledge.Edit, p=Depends(administrator)):
        return knowledge.publish(service, p, data.model_dump())

    @app.patch("/v1/tasks/{task_id}")
    def task_status(task_id: str, data: TaskEdit, p=Depends(principal)):
        return service.edit_task(p, task_id, data.model_dump(mode="json", exclude_unset=True))

    @app.patch("/v1/leads/{lead_id}/outreach")
    def review_outreach(lead_id: str, data: OutreachEdit, p=Depends(principal)):
        return service.resolve_goal_review(p, lead_id, data.model_dump(mode="json", exclude_unset=True))

    @app.post("/v1/calls/{call_id}/dtmf")
    async def dtmf(call_id: str, data: KeypadRequest, p=Depends(principal)):
        return await service.send_keypad(p, call_id, data.digits, data.idempotency_key)

    @app.get("/v1/calls/{call_id}/dtmf/{request_id}")
    def keypad_status(call_id: str, request_id: str, p=Depends(principal)):
        return service.keypad_status(p, call_id, request_id)

    @app.post("/v1/admin/seller/approve")
    def approve_seller(data: dict, p=Depends(administrator)):
        if not data.get("identity_reference"):
            fail(422, "Verified business identity evidence is required")
        with db.transaction() as session:
            seller = service.required(session, "sellers", p["seller_id"], p)
            # Recording reviewed evidence is the approval; only an explicit false revokes it.
            update(
                seller,
                approved=data.get("approved") is not False,
                identity_reference=data["identity_reference"],
            )
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "seller.identity_reviewed",
                seller.id,
                clock(),
            )
            return doc(seller)

    @app.post("/v1/admin/consent")
    def consent(data: ConsentChange, p=Depends(administrator)):
        return record_consent(service, p, data.model_dump(mode="json"))

    @app.post("/v1/telephony/agent")
    async def agent_leg(p=Depends(principal)):
        return await service.create_agent(p)

    @app.get("/v1/telephony/agent/{provider_id:path}")
    def agent_status(provider_id: str, p=Depends(principal)):
        with db.transaction(False) as session:
            leg = service.agent_leg_in(session, p, provider_id=provider_id, rep_id=p["user_id"])
            if not leg:
                fail(404, "Verified agent leg not found")
            return {"state": leg.data["state"], "agent_call_control_id": provider_id}

    @app.post("/v1/admin/control/run")
    async def control_run(p=Depends(administrator)):
        return await service.dispatch_controls(p)

    @app.get("/v1/calls/{call_id}/recording")
    def recording(call_id: str, p=Depends(principal)):
        with db.transaction(False) as s:
            service.required(s, "call_attempts", call_id, p)
        fail(410, "Call recordings are unavailable under the short-retention policy")

    @app.post("/v1/admin/trackers")
    def publish_tracker(data: dict, p=Depends(administrator)):
        if (
            data.get("kind") not in {"keyword", "semantic"}
            or not data.get("name")
            or not data.get("definition")
        ):
            fail(422, "Tracker type, name and definition are required")
        if data["kind"] == "keyword" and not data.get("phrases"):
            fail(422, "Keyword tracker needs phrases")
        if data["kind"] == "semantic" and (
            not data.get("positive_examples") or not data.get("negative_examples")
        ):
            fail(422, "Semantic trackers need reviewed positive and negative examples")
        with db.transaction() as session:
            obj = put(
                session,
                "trackers",
                p["tenant_id"],
                p["seller_id"],
                {
                    "name": str(data["name"])[:200],
                    "kind": data["kind"],
                    "definition": str(data["definition"])[:1000],
                    "phrases": data.get("phrases", []),
                    "positive_examples": data.get("positive_examples", []),
                    "negative_examples": data.get("negative_examples", []),
                    "evaluation_status": "review_required",
                },
                now=clock(),
            )
            return doc(obj)

    @app.post("/v1/admin/models/train")
    def train_model(data: dict, p=Depends(administrator)):
        if data:
            fail(422, "Training accepts only authority-owned call-history snapshots")
        return service.train_operational(p)

    @app.post("/v1/admin/models/{model_id}/rollback")
    def rollback_model(model_id: str, data: dict, p=Depends(administrator)):
        if not data.get("reason"):
            fail(422, "Rollback reason required")
        with db.transaction() as session:
            if model_id == "baseline":
                for item in service.scoped(session, "model_versions", p):
                    update(item, active=False, trial_active=False)
                audit(
                    session,
                    p["tenant_id"],
                    p["seller_id"],
                    p["user_id"],
                    "model.baseline_restored",
                    p["tenant_id"],
                    clock(),
                )
                return {"active": "pooled-funnel-v3"}
            model = service.required(session, "model_versions", model_id, p)
            if not model.data.get("ever_activated") or not service.model_source_current(
                session, p, model, service.optimization_config(session, p)
            ):
                fail(
                    409,
                    "Rollback requires a previously activated model with the current evaluated definition",
                )
            from app.ml.training import predict_funnel, retired_reason

            if not predict_funnel(model.data, {}, service.settings.data_dir / "models"):
                fail(
                    409,
                    retired_reason(model.data)
                    or "Rollback artifact is unavailable or failed verification; use the pooled baseline",
                )
            for item in service.scoped(session, "model_versions", p):
                update(item, active=item.id == model.id, trial_active=False)
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "model.rollback",
                model.id,
                clock(),
                {"reason": data["reason"]},
            )
            return doc(model)

    @app.post("/v1/admin/snippets")
    def publish_snippet(data: library.ExampleReview, p=Depends(administrator)):
        return library.publish(service, p, data.model_dump())

    @app.patch("/v1/admin/snippets/{example_id}")
    def withdraw_snippet(example_id: str, data: library.ExampleWithdraw, p=Depends(administrator)):
        return library.withdraw(service, p, example_id, data.expected_version)

    @app.post("/v1/admin/rnd/results")
    def import_rnd(data: dict, p=Depends(administrator)):
        if (
            not data.get("subscription_reference")
            or not data.get("reference_date")
            or data.get("result") not in {"yes", "no", "no_data"}
        ):
            fail(
                422,
                "Authorized RND subscription, meaningful prior reference date and provider result required",
            )
        with db.transaction() as session:
            endpoint = service.required(session, "phone_endpoints", data.get("endpoint_id"), p)
            result = put(
                session,
                "rnd_queries",
                p["tenant_id"],
                p["seller_id"],
                {
                    "endpoint_id": endpoint.id,
                    "reference_date": data["reference_date"],
                    "query_date": clock().isoformat(),
                    "result": data["result"],
                    "subscription_reference": data["subscription_reference"],
                    "provider_receipt": data.get("provider_receipt"),
                },
                now=clock(),
            )
            if data["result"] in {"yes", "no_data"}:
                update(endpoint, reassignment_review_required=True)
            return doc(result)

    @app.post("/v1/admin/restore/reconcile")
    async def reconcile_restore(data: dict, p=Depends(administrator)):
        return await service.reconcile_restore(p, data.get("review_reference"), data.get("provider_ids"))

    @app.post("/v1/admin/models/{model_id}/trial")
    def save_trial(model_id: str, data: dict, p=Depends(administrator)):
        # The client names the saved report and its review note. Every gate value is read
        # from that report, so client-supplied pass flags, deltas or counts are ignored.
        if not data.get("report_reference"):
            fail(422, "A review reference for the saved controlled-trial report is required")
        report_id = data.get("cohort_report_id")
        with db.transaction() as session:
            model = service.required(session, "model_versions", model_id, p)
            if not report_id:
                fail(409, "A saved controlled cohort report is required to review a candidate")
            report = service.required(session, "evaluation_reports", report_id, p)
            if (
                report.data.get("kind") != "conversion_trial"
                or report.data.get("simulation")
                or not report.data.get("sufficient_evidence")
                or report.data.get("goal") != model.data.get("goal")
                or report.data.get("trial_model_id") != model.id
            ):
                fail(409, "The saved report lacks sufficient independent live conversion evidence")
            update(
                model,
                online_trial_passed=True,
                report_reference=str(data["report_reference"])[:1000],
                coverage_delta=float(report.data.get("coverage_delta", -1)),
                policy_violations=report.data.get("policy_violations", 1),
                trial_reviewed_by=p["user_id"],
                cohort_report_id=report.id,
            )
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "model.trial_reviewed",
                model.id,
                clock(),
            )
            return doc(model)

    from app.optimization.api import register_optimization

    register_optimization(app, service, principal, administrator)
    return app
