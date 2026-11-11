import asyncio
import functools
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app import ops
from app.calling import audio_leg, detection, pool_queue, timeline
from app.compliance.operating import DNCAuthorization, bind_operating_api
from app.lead_lists import memberships
from app.numbers import pool
from app.platform.config import Settings
from app.platform.db import Database, Event, audit, doc, put, rows, update
from app.preparation.templates import PreparationPolicy
from app.rep_workflows import MeetingEdit, OutreachEdit, TaskEdit
from app.scheduling.models import ContactRole, OutcomeCode
from app.service import TEST_LIST, AppState, fail
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
    name: str = Field(default="Local operator", max_length=120)


class CoachPreferences(Strict):
    muted: bool | None = None
    dismissed: str | None = Field(default=None, max_length=80)


class CoachQuestion(Strict):
    question: str | None = Field(default=None, max_length=2000)
    urgent: bool = True
    request_id: UUID | None = None


class CoachCancel(Strict):
    request_id: UUID


class Presence(Strict):
    status: Literal["ready", "paused", "busy"]
    audio_ready: bool = False
    telephony_ready: bool = False
    agent_call_control_id: str | None = None
    pending_optouts: int = Field(default=0, ge=0)


class Reachable(Strict):
    registered: bool


class QueueNext(Strict):
    campaign_id: str
    list_names: list[str] | None = Field(default=None, max_length=200)
    callback_lead_id: str | None = None


class ListEdit(Strict):
    campaign_id: str
    list_name: str = Field(max_length=120)
    action: Literal["pause", "resume", "archive", "restore", "rename"]
    name: str | None = Field(default=None, max_length=120)


class BulkLeadEdit(Strict):
    lead_ids: list[str] = Field(min_length=1, max_length=1000)
    action: Literal["pause", "resume", "archive", "restore", "move", "add", "remove", "location"]
    list_name: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)


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
    list_name: str = Field(default="", max_length=120)
    rows: list[dict] = Field(max_length=10000)
    # The app asks for research and per-lead scripts before calling; API clients opt in.
    prepare: bool = False
    # Role -> {"column", "delimiter"}; omitted, the map is guessed from the headers (section 9).
    column_map: dict[str, dict[str, str | None]] | None = None


class HubSpotSync(Strict):
    list_id: str = Field(min_length=1, max_length=40)
    list_name: str = Field(min_length=1, max_length=120)


class SpendLimit(Strict):
    list_name: str = Field(min_length=1, max_length=120)
    usd: float = Field(ge=0, le=10000, allow_inf_nan=False)


class ColumnMapRequest(Strict):
    headers: list[str] = Field(max_length=500)


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
    text: str = Field(max_length=20000)


class Disposition(Strict):
    audio_result: Literal["unknown", "two_way", "problem"] | None = None
    screened: bool | None = None
    delivery_test: bool | None = None
    request_id: UUID | None = None
    contact_role: ContactRole = "unknown"
    outcome: OutcomeCode
    callback_at: AwareDatetime | None = None
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    # Rep-visible edit counter: background bookkeeping on the call row never conflicts.
    expected_edit_version: int | None = Field(default=None, ge=0, strict=True)
    callback_note: str = Field(default="", max_length=1000)


class ResearchBatch(Strict):
    lead_ids: list[str] = Field(max_length=10000)


class PrepareList(Strict):
    list_name: str = Field(default="", max_length=120)


class RewriteScripts(Strict):
    list_name: str | None = Field(default=None, max_length=120)
    lead_ids: list[str] | None = Field(default=None, max_length=10000)
    refresh_research: bool = False
    sample_size: int | None = Field(default=None, ge=1, le=20)
    remaining_only: bool = False


class SimulationEvent(Strict):
    event_id: str
    event_type: str
    call_id: str
    payload: dict = Field(default_factory=dict)


class Budget(Strict):
    """AI spending per lead list (research, scripts, live help, post-call) and for number purchases."""

    per_list_usd: float = Field(default=25, ge=0, le=100000)
    number_purchase_limit_usd: float = Field(default=0, ge=0, le=10000)


class OwnedNumber(Strict):
    phone: str
    move_from_other_app: bool = Field(default=False, strict=True)


class SpendCap(Strict):
    daily_usd: float = Field(gt=0, le=10000, allow_inf_nan=False)


class NumberPurchase(Strict):
    quote_id: UUID
    approve_recurring: bool = Field(strict=True)


class CallerName(Strict):
    # Telnyx lists up to 15 letters, digits and spaces for outbound caller name. None means no name.
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9 ]{1,15}$")


class PolicyUpdate(Strict):
    # The path names the version being edited; it must still be current (see publish).
    expected_version: int | None = Field(default=None, ge=1)
    approved: bool | None = None
    reason: str = Field(default="", max_length=1000)
    review_reference: str | None = None
    # Optional national do-not-call screening before calls (off by default).
    dnc_screening: bool | None = None
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


class CarrierLookup(Strict):
    approve_charge: Literal[True]


class ProviderSettings(Strict):
    outbound_ring_seconds: int | None = Field(default=None, ge=5, le=600, strict=True)
    # R6.6: while on, only the owner's test line can be dialed (section 10).
    test_session: bool | None = None
    test_phone: str | None = Field(default=None, max_length=32)
    maximum_owned_numbers: int | None = Field(default=None, ge=0, le=1000)
    number_initial_limit_usd: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    number_monthly_limit_usd: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
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
    coach_provider: Literal["openrouter", "static"] | None = None
    openrouter_api_key: str | None = None
    coach_model: str | None = Field(default=None, min_length=3, max_length=200)
    # Phase 9B: the optional CRM integrations (section 7).
    realvalidito_api_key: str | None = Field(default=None, max_length=200)
    realvalidito_api_secret: str | None = Field(default=None, max_length=200)
    hubspot_service_key: str | None = Field(default=None, max_length=500)
    treg_token: str | None = Field(default=None, max_length=500)
    # R9C.10: research on this seat uses the ChatGPT login or the OpenRouter fallback.
    research_route: Literal["chatgpt", "fallback"] | None = None


SECRET_FIELDS = {
    "dnc_api_key",
    "telnyx_api_key",
    "openrouter_api_key",
    "realvalidito_api_key",
    "realvalidito_api_secret",
    "hubspot_service_key",
    "treg_token",
}
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


CONFLICTS = logging.getLogger("app.conflicts")


def write_conflict(error):
    """The conflicting table and constraint, never row values (PostgreSQL detail text carries them)."""
    diag = getattr(getattr(error, "orig", None), "diag", None)
    table, constraint = getattr(diag, "table_name", None), getattr(diag, "constraint_name", None)
    if table is None:
        # StaleDataError names its table; SQLite names table.column only.
        found = re.search(r"table '(\w+)'|constraint failed: (\w+)\.", str(getattr(error, "orig", error)))
        table = found and (found.group(1) or found.group(2))
    return {"type": type(error).__name__, "table": table, "constraint": constraint}


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
    from app.compliance.operating import OperatingPolicy, ScreeningRequest
    from app.optimization.api import (
        Conversion,
        HealthPolicy,
        ModelTrial,
        OptimizationSettings,
    )
    from app.preparation.templates import PreparationPolicy
    from app.scheduling.models import EstimatePolicy
    from app.telephony.callbacks import CallbackRoutingEdit

    return {
        name: field_bounds(model)
        for name, model in {
            "operating": OperatingPolicy,
            "preparation": PreparationPolicy,
            "optimization": OptimizationSettings,
            "health_policy": HealthPolicy,
            "estimate_policy": EstimatePolicy,
            "policy": PolicyUpdate,
            "budget": Budget,
            "callback_routing": CallbackRoutingEdit,
            "conversion": Conversion,
            "model_trial": ModelTrial,
            "setup": Setup,
            "screening": ScreeningRequest,
        }.items()
    }


def create_app(settings=None, *, clock=None, carrier=None):
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    clock = clock or (lambda: datetime.now(UTC))
    db = Database(settings.database_url)
    service = AppState(db, settings, clock, carrier)
    from app.leads.backfill import backfill_leads

    backfill_leads(db)

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
    from fastapi.exceptions import RequestValidationError

    from app.platform.secret_files import SecretStorageError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic's default includes submitted input, including malformed passwords/keys.
        return JSONResponse(
            {"detail": [{k: entry[k] for k in ("type", "loc", "msg")} for entry in error.errors()]},
            status_code=422,
        )

    @app.exception_handler(SecretStorageError)
    async def credential_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.middleware("http")
    async def boundaries(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse({"detail": "Untrusted request origin"}, status_code=403)
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", 0)) > 5_000_000
        ):
            return JSONResponse({"detail": "Request too large"}, status_code=413)
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
        conflict = write_conflict(error)
        CONFLICTS.warning(
            "Write conflict: %s on %s", conflict["type"], conflict["table"], extra={"conflict": conflict}
        )
        return JSONResponse(
            {"detail": "Concurrent operation conflicts with an existing reservation or identity"},
            status_code=409,
        )

    def principal(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not credentials:
            fail(401, "Authentication required")
        return service.sessions.check(db, credentials.credentials, clock())

    # One user (R3.4): every signed-in request may change settings.
    administrator = principal

    bind_operating_api(app, service, administrator)

    from app.providers.codex import bind_api as bind_codex_api

    bind_codex_api(app, service, principal)
    from app.managed_webhooks import bind_api as bind_managed_api

    bind_managed_api(app, service, principal, administrator)
    from app.live_transcription import bind_api as bind_audio_api

    bind_audio_api(app, service, principal)

    def local_nonce(value):
        if not settings.local_token or not secrets.compare_digest(value or "", settings.local_token):
            fail(401, "Local bootstrap nonce required")

    @app.get("/health")
    def health():
        with db.transaction(False) as s:
            initialized = bool(rows(s, "tenants"))
        return {
            "status": "ok",
            "version": settings.version,
            "mode": "local",
            "simulation": settings.simulation,
            "initialized": initialized,
        }

    @app.post("/v1/setup")
    def setup(data: Setup, x_local_token: str | None = Header(default=None)):
        local_nonce(x_local_token)
        return ops.setup(service, data.model_dump())

    @app.post("/v1/local/session")
    def local_session(x_local_token: str | None = Header(default=None)):
        local_nonce(x_local_token)
        with db.transaction(False) as s:
            users = rows(s, "users")
            if not users:
                fail(409, "Workspace setup required")
            # One user (R3.4); an older store may still hold a teammate row, so take the first owner.
            user = min(users, key=lambda u: (u.data.get("role", "owner") != "owner", u.created_at))
            return service.sessions.issue(user, clock())

    @app.get("/v1/quiescence")
    def quiescence(p=Depends(principal)):
        with db.transaction(False) as s:
            return ops.quiescence_in(service, s, p)

    @app.get("/v1/workspace")
    def workspace(p=Depends(principal)):
        return ops.snapshot(service, p)

    @app.get("/v1/workspace/{section}")
    def workspace_page(
        section: str,
        cursor: str | None = None,
        q: str = "",
        filter: str = "all",
        list_name: str | None = None,
        p=Depends(principal),
    ):
        return ops.workspace_page(service, p, section, cursor=cursor, q=q, filter=filter, list_name=list_name)

    @app.post("/v1/simulation/seed")
    def seed(p=Depends(administrator)):
        return ops.seed(service, p)

    @app.post("/v1/rep/reachable")
    async def rep_reachable(data: Reachable, p=Depends(principal)):
        """D29: the open app says every 30 seconds whether its call client can ring; callbacks ring it then."""
        saved = await asyncio.to_thread(ops.reachable, service, p, data.registered)
        service.managed.publish_reachability(p["tenant_id"], data.registered)
        return saved

    @app.post("/v1/rep/presence")
    async def presence(data: Presence, p=Depends(principal)):
        saved, released = await asyncio.to_thread(ops.presence, service, p, data.model_dump())
        if released:
            await pool.cancel(service.managed, released)
        return saved

    @app.post("/v1/queue/release")
    async def release_reservation(p=Depends(principal)):
        def release():
            with db.transaction() as s:
                db.authorize_stream(s, p)
                return ops.release_unused_reservation_in(service, s, p)

        if released := await asyncio.to_thread(release):
            await pool.cancel(service.managed, released)
        return {"released": True}

    @app.post("/v1/queue/next")
    async def next_lead(data: QueueNext, p=Depends(principal)):
        if ops.in_test_session(service, p):
            # A test session offers only the Test line list; dialing refuses any other number too.
            return await pool_queue.reserve(service, p, data.campaign_id, [TEST_LIST], None)
        return await pool_queue.reserve(service, p, data.campaign_id, data.list_names, data.callback_lead_id)

    @app.get("/v1/blockers")
    def calling_blockers(p=Depends(principal)):
        """What stops calling now, first to fix first; empty when the rep can dial (R4.3)."""
        with db.transaction(False) as s:
            return {"blockers": [b.detail() for b in ops.dial_blockers_in(service, s, p, rep=True)]}

    @app.get("/v1/calling/readiness")
    def calling_readiness(p=Depends(administrator)):
        """R9E.5: run the checks a dial would, while idle; the Dialer shows the result before Start."""
        return ops.calling_readiness(service, p)

    @app.get("/v1/review/drop-off")
    def review_drop_off(p=Depends(principal)):
        """D31, R11.6: where answered real calls of the last 30 days ended in the script, by outcome."""
        return ops.drop_off_report(service, p)

    @app.get("/v1/review/issues")
    def review_issues(p=Depends(principal)):
        """What needs the rep, each with the one thing to do: Review opens with this list."""
        from app.review_issues import issues

        with db.transaction(False) as s:
            return {
                "issues": issues(
                    blockers=[b.detail() for b in ops.dial_blockers_in(service, s, p, rep=False)],
                    leads=[m.data for m in ops.scoped(service, s, "leads", p)],
                    calls=[m.data for m in ops.scoped(service, s, "call_attempts", p)],
                    tasks=[m.data for m in ops.scoped(service, s, "followup_tasks", p)],
                    now=service.clock(),
                )
            }

    @app.post("/v1/calls/start")
    async def start_call(data: CallStart, p=Depends(principal)):
        return await ops.start_call(service, p, data.model_dump())

    @app.get("/v1/settings/rows")
    def settings_rows(p=Depends(administrator)):
        return ops.setup_rows(service, p)

    @app.post("/v1/settings/rows/{row_id}/check")
    async def check_settings_row(row_id: str, p=Depends(administrator)):
        return await ops.check_setup_row(service, p, row_id)

    @app.post("/v1/settings/test-call")
    def start_test_call(p=Depends(administrator)):
        """R7.5: limit calling to the test phone; the Dialer then calls it."""
        return ops.start_test_session(service, p)

    @app.delete("/v1/settings/test-call")
    def end_test_call(p=Depends(administrator)):
        ops.end_test_session(service, p)
        return {"test_session": False}

    @app.get("/v1/calling/voicemail-report")
    def voicemail_report(p=Depends(principal)):
        """Voicemail detection against what the rep found when they stayed (R6.4)."""
        with db.transaction(False) as s:
            return detection.report([doc(c) for c in ops.scoped(service, s, "call_attempts", p)])

    @app.get("/v1/calls/{call_id}/timeline")
    def call_timeline(call_id: str, p=Depends(principal)):
        """What happened on one call, in order: its own steps, carrier events and sent commands (R4.12)."""
        with db.transaction(False) as s:
            call = ops.required(service, s, "call_attempts", call_id, p)
            return {"call_id": call_id, "entries": timeline.for_call(s, p, call)}

    @app.get("/v1/calls/{call_id}")
    def get_call(call_id: str, p=Depends(principal)):
        from app import live_help
        from app.aftercall.summary import suggestion

        with db.transaction(False) as s:
            call = ops.required(service, s, "call_attempts", call_id, p)
            tip, helper = live_help.view(service, call)
            segments = ops.transcript_rows(service, s, p, call_id)
            summary = ops.call_summary(service, s, p, call_id)
            return {
                **{k: v for k, v in doc(call).items() if k != "recording_url"},
                "aftercall": summary,
                "transcript": segments,
                "raw_evidence_status": ops.raw_status(service, call, segments),
                "coach": tip,
                "helper_status": helper,
                "suggested_outcome": suggestion(summary),
            }

    @app.post("/v1/calls/{call_id}/stay")
    def stay_on_call(call_id: str, p=Depends(principal)):
        return ops.stay_on_call(service, p, call_id)

    @app.post("/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, p=Depends(principal)):
        return await ops.hangup(service, p, call_id)

    @app.delete("/v1/calls/{call_id}/transcript")
    def delete_transcript(call_id: str, p=Depends(principal)):
        """R10.5: the rep deletes a call's saved transcript now instead of waiting 30 days."""
        with db.transaction() as s:
            ops.required(service, s, "call_attempts", call_id, p)
            ops.forget_transcript(service, s, p, call_id)
        return {"deleted": True}

    @app.post("/v1/calls/{call_id}/disposition")
    def disposition(call_id: str, data: Disposition, p=Depends(principal)):
        saved = ops.save_disposition(service, p, call_id, data.model_dump(mode="json"))
        if saved.get("is_test") and ops.in_test_session(service, p):
            # A test call is one call: once its outcome is saved the session ends and calling pauses, so the
            # loop never moves straight on to a prospect (live check, November 10).
            ops.end_test_session(service, p)
            ops.presence(service, p, {"status": "paused", "telephony_ready": False})
        return saved

    @app.post("/v1/leads/{lead_id}/meeting")
    def report_meeting(lead_id: str, data: MeetingEdit, p=Depends(principal)):
        return ops.report_meeting(service, p, lead_id, data.model_dump(mode="json", exclude_unset=True))

    @app.post("/v1/suppressions")
    def suppress(data: Suppress, p=Depends(principal)):
        return ops.suppress(service, p, data.phone, data.reason, event_id=data.event_id)

    @app.post("/v1/admin/suppressions/release-hold")
    def release_hold(data: HoldRelease, p=Depends(administrator)):
        from app.compliance.suppressions import release_hold

        return release_hold(service, p, data.phone, data.reason)

    @app.get("/v1/lists")
    def lists(
        list_names: list[str] | None = Query(default=None), selected: bool = False, p=Depends(principal)
    ):
        return ops.list_summary(service, p, ([] if selected else None) if list_names is None else list_names)

    @app.patch("/v1/lists")
    def manage_list(data: ListEdit, p=Depends(administrator)):
        return ops.manage_list(service, p, data.model_dump())

    @app.post("/v1/leads/bulk")
    def bulk_leads(data: BulkLeadEdit, p=Depends(administrator)):
        return ops.bulk_leads(service, p, data.model_dump())

    @app.post("/v1/leads/import")
    def import_leads(data: ImportLeads, p=Depends(administrator)):
        return ops.import_leads(service, p, data.model_dump())

    @app.get("/v1/hubspot/lists")
    async def hubspot_lists(p=Depends(administrator)):
        from app import crm

        return await crm.lists(service, p)

    @app.post("/v1/hubspot/sync")
    async def hubspot_sync(data: HubSpotSync, p=Depends(administrator)):
        from app import crm

        return await crm.sync(service, p, data.list_id, data.list_name.strip())

    @app.put("/v1/lists/spend-limit")
    def list_spend_limit(data: SpendLimit, p=Depends(administrator)):
        from app import crm

        return crm.set_spend_limit(service, p, data.list_name.strip(), data.usd)

    @app.post("/v1/leads/column-map")
    def column_map(data: ColumnMapRequest, p=Depends(administrator)):
        from app.leads.intake import ROLES, guess_map

        return {"map": guess_map(data.headers), "roles": sorted(ROLES)}

    @app.get("/v1/leads/{lead_id}/packet")
    def packet(lead_id: str, p=Depends(principal)):
        return ops.packet(service, p, lead_id)

    @app.get("/v1/preparation")
    def preparation(p=Depends(principal)):
        return ops.preparation_status(service, p)

    @app.post("/v1/research/batches")
    def research(data: ResearchBatch, p=Depends(administrator)):
        return ops.prepare_list(service, p, lead_ids=data.lead_ids)

    @app.post("/v1/preparation/lists")
    def prepare_list(data: PrepareList, p=Depends(administrator)):
        return ops.prepare_list(service, p, data.list_name)

    @app.post("/v1/preparation/rewrite")
    def rewrite_scripts(data: RewriteScripts, p=Depends(administrator)):
        return ops.rewrite_scripts(service, p, **data.model_dump())

    @app.get("/v1/preparation/estimate")
    def preparation_estimate(list_name: str | None = None, p=Depends(administrator)):
        from app.model_tokens import estimate

        return estimate(service, p, list_name=list_name)

    @app.get("/v1/usage/tokens")
    def token_usage(
        campaign_id: str | None = None,
        list_name: str | None = None,
        base_version: int | None = None,
        kind: str | None = None,
        lead_id: str | None = None,
        p=Depends(administrator),
    ):
        from app.model_tokens import report

        return report(
            service,
            p,
            campaign_id=campaign_id,
            list_name=list_name,
            base_version=base_version,
            kind=kind,
            lead_id=lead_id,
        )

    @app.post("/v1/preparation/lists/release")
    def release_list(data: PrepareList, p=Depends(administrator)):
        return ops.release_list(service, p, data.list_name)

    @app.post("/v1/preparation/resume")
    def resume_preparation(p=Depends(administrator)):
        return ops.resume_preparation(service, p)

    @app.put("/v1/admin/preparation")
    def preparation_settings(data: PreparationPolicy, p=Depends(administrator)):
        with db.transaction() as s:
            db.authorize_stream(s, p)
            seller = ops.required(service, s, "sellers", p["seller_id"], p)
            policy = dict(seller.data.get("operating_policy", {}))
            policy["preparation"] = data.model_dump()
            update(seller, operating_policy=policy)
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "preparation.updated", seller.id, clock())
        ops.prepare_tick(service, p)
        return data.model_dump()

    @app.post("/v1/calls/{call_id}/transcript")
    async def transcript(call_id: str, data: Transcript, p=Depends(principal)):
        # Synthetic/manual corrections only. Live machine ingestion is signature-verified via webhooks.
        return await ops.transcript(service, p, call_id, data.model_dump())

    @app.patch("/v1/calls/{call_id}/coach")
    def coach_preferences(call_id: str, data: CoachPreferences, p=Depends(principal)):
        """Mute automatic tips on this call, or dismiss the tip shown."""
        with db.transaction() as s:
            call = ops.required(service, s, "call_attempts", call_id, p)
            preferences = dict(call.data.get("coach_preferences", {}))
            if data.muted is not None:
                preferences["muted"] = data.muted
                update(call, coach_preferences=preferences)
            if data.dismissed:
                service.tips.dismiss(call_id, data.dismissed)
            return preferences

    @app.post("/v1/calls/{call_id}/coach")
    async def coach(call_id: str, data: CoachQuestion | None = None, p=Depends(principal)):
        from app import live_help

        with db.transaction(False) as s:
            ops.required(service, s, "call_attempts", call_id, p)
        return await live_help.turn(
            service,
            p,
            call_id,
            question=data.question if data else None,
            request_id=str(data.request_id) if data and data.request_id else None,
        )

    @app.post("/v1/calls/{call_id}/coach/cancel")
    def cancel_coach(call_id: str, data: CoachCancel, p=Depends(principal)):
        from app import live_help

        return live_help.cancel(service, p, call_id, str(data.request_id))

    @app.post("/v1/calls/{call_id}/summary")
    def retry_summary(call_id: str, p=Depends(principal)):
        """R10.4: a summary that failed is written again on request; nothing else is redone."""
        with db.transaction() as s:
            call = ops.required(service, s, "call_attempts", call_id, p)
            ops.enqueue_aftercall(service, s, p, call, retry=True)
        return {"queued": True}

    @app.get("/v1/calls/{call_id}/review")
    def call_review(call_id: str, p=Depends(principal)):
        return ops.call_review(service, p, call_id)

    @app.patch("/v1/calls/{call_id}/notes")
    def edit_notes(call_id: str, data: NoteEdit, p=Depends(principal)):
        return ops.edit_notes(service, p, call_id, data.text)

    # The live stream's read, also served over HTTP for catch-up and tests.
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
                from app.platform.db import MODELS

                seller = e.payload.get("_seller_id")
                if seller is None:
                    # Old release events lack scope metadata. Resolve their entity, otherwise fail closed.
                    for model in MODELS.values():
                        target = s.get(model, e.entity_id)
                        if target and target.tenant_id == p["tenant_id"]:
                            seller = target.seller_id
                            break
                return seller == p["seller_id"]

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
            p = service.sessions.check(db, auth.get("token", ""), clock())
            cursor = int(auth.get("after", 0))
            while True:
                await asyncio.to_thread(service.sessions.check, db, auth["token"], clock())
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
        result = ops.event(service, p, data.event_id, data.event_type, data.call_id, data.payload)
        if saved := result.pop("save_outcome", None):
            ops.auto_disposition(service, *saved)
        return result

    @app.post("/v1/telephony/token")
    async def telephony_token(p=Depends(principal)):
        def context():
            provider = ops.carrier(service, p["tenant_id"])
            conf = service.configs.read(p["tenant_id"])
            with db.transaction(False) as s:
                user = ops.required(service, s, "users", p["user_id"], p)
                sip, credential = audio_leg.identity(
                    user.data, conf, managed=service.managed.enabled(p["tenant_id"])
                )
            return provider, credential, sip

        provider, credential, sip = await asyncio.to_thread(context)
        if not credential and not settings.simulation:
            fail(503, "Scoped rep telephony credential is not configured")
        if not settings.simulation:
            await audio_leg.verify_once(service.ctx, provider, credential, sip)
        return {"token": await provider.token(credential or "simulated"), "simulation": settings.simulation}

    @app.post("/v1/admin/policies/{policy_id}/publish")
    def publish(policy_id: str, data: PolicyUpdate, p=Depends(administrator)):
        body = data.model_dump(exclude_none=True)
        body.pop("expected_version", None)
        # Versions are immutable: only the current policy is edited; publishing makes the next version.
        with db.transaction(False) as s:
            policy = ops.required(service, s, "policy_versions", policy_id, p)
            campaign = ops.required(service, s, "campaigns", policy.data["campaign_id"], p)
            if campaign.data["policy_id"] != policy.id:
                fail(
                    409,
                    "POLICY_VERSION_CONFLICT: a newer calling policy is current; reload it before publishing",
                )
        return ops.publish(service, p, policy_id, body)

    @app.get("/v1/admin/preflight")
    def preflight(deep: bool = False, p=Depends(administrator)):
        return ops.preflight(service, p, deep=deep)

    @app.get("/v1/admin/callback-routing")
    def callback_routing(p=Depends(administrator)):
        return ops.read_callback_routing(service, p)

    @app.put("/v1/admin/callback-routing")
    def write_callback_routing(data: CallbackRoutingEdit, p=Depends(administrator)):
        return ops.edit_callback_routing(service, p, data.model_dump(exclude_none=True))

    @app.post("/v1/callbacks/{task_id}/hangup")
    async def end_callback(task_id: str, p=Depends(principal)):
        result = await asyncio.to_thread(ops.end_inbound, service, p, task_id)
        await ops.dispatch_controls(service, p)
        return result

    @app.get("/v1/definitions")
    def read_definitions(p=Depends(principal)):
        # Schema bounds and enums only: nothing seller-specific, so shared forms read them too.
        return {"bounds": setting_bounds()}

    @app.get("/v1/admin/settings")
    def read_settings(p=Depends(administrator)):
        from app.compliance.operating import OperatingPolicy
        from app.optimization.api import HealthPolicy
        from app.optimization.workflows import DEFAULTS as OPTIMIZATION_DEFAULTS
        from app.platform.config import PROVIDER_DEFAULTS, inference_model
        from app.preparation.templates import PreparationPolicy
        from app.scheduling.models import EstimatePolicy

        config = service.configs.read(p["tenant_id"])
        with db.transaction(False) as s:
            budget = ops.scoped(service, s, "budgets", p)[0]
            # Older stores have daily/monthly limits and no per-list budget; defaults fill the gap.
            budget_values = Budget.model_validate(
                {key: budget.data[key] for key in Budget.model_fields if key in budget.data}
            ).model_dump()
            # Current campaign goal and weekly schedule for the setup checklist.
            optimization = ops.optimization_config(service, s, p)
        return {
            **{k: v for k, v in config.items() if k not in SECRET_FIELDS and not k.startswith("_")},
            "configured": {k: bool(config.get(k)) for k in SECRET_FIELDS},
            "version": config.get("version", 0),
            "budget": budget_values,
            "optimization": optimization,
            "effective": {
                **{
                    key: config.get(key, getattr(PROVIDER_DEFAULTS, key))
                    for key in ("coach_provider", "coach_model")
                },
                "inference_mode": "codex",
                "model": inference_model(config),
                **{
                    role + "_model": inference_model(config, role)
                    for role in ("research_extract", "script_write")
                },
            },
            "defaults": {
                "providers": PROVIDER_DEFAULTS.model_dump(mode="json"),
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
        if values.get("test_session") and not (values.get("test_phone") or ops.test_phone(service, p)):
            fail(409, "Type your phone number in Test phone first")
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
            ops._restore_operator(service, s, p)
            previous_route = service.configs.read(p["tenant_id"]).get("telnyx_connection_id")
            service.configs.write(
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
        if previous_route and values.get("telnyx_connection_id") not in {None, previous_route}:
            ops.forget_workspace_test(service, p["tenant_id"])
        return read_settings(p)

    @app.put("/v1/admin/budget")
    def budget(data: Budget, p=Depends(administrator)):
        with db.transaction() as s:
            obj = ops.scoped(service, s, "budgets", p)[0]
            update(obj, **data.model_dump())
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "budget.updated", obj.id, clock())
            return doc(obj)

    @app.post("/v1/admin/numbers/add")
    async def add_owned_number(data: OwnedNumber, p=Depends(administrator)):
        return await ops.add_owned_number(
            service, p, data.phone, move_from_other_app=data.move_from_other_app
        )

    @app.put("/v1/admin/numbers/spend-cap")
    async def spend_cap(data: SpendCap, p=Depends(administrator)):
        return await ops.save_spend_cap(service, p, data.daily_usd)

    @app.put("/v1/admin/caller-name")
    async def caller_name(data: CallerName, p=Depends(administrator)):
        return await ops.save_caller_name(service, p, data.name)

    @app.get("/v1/admin/numbers/registry-export")
    def registry_export(p=Depends(administrator)):
        return ops.registry_export(service, p)

    @app.post("/v1/admin/leads/{lead_id}/carrier-lookup")
    async def carrier_lookup(lead_id: str, data: CarrierLookup, p=Depends(administrator)):
        return await ops.lookup_lead_carrier(service, p, lead_id)

    @app.get("/v1/admin/numbers/summary")
    def numbers_summary(list_name: str | None = None, p=Depends(administrator)):
        """Whole-inventory counts."""
        from collections import Counter

        from sqlalchemy import func

        from app.optimization.geography import STATES
        from app.platform.db import MODELS

        with db.transaction(False) as s:
            numbers = ops.scoped(service, s, "number_inventory", p)
            lifecycles = Counter(n.data["lifecycle"] for n in numbers)
            ready = sum(bool(ops.number_readiness_in(service, s, p, n)["outbound_ready"]) for n in numbers)
            test_passed = ops.workspace_test_passed(service, s, p)
            binding = MODELS["lead_number_bindings"]
            stable = s.scalar(
                select(func.count())
                .select_from(binding)
                .where(binding.tenant_id == p["tenant_id"], binding.seller_id == p["seller_id"])
            )
            campaign = ops.selected_campaign(service, s, p)
            model = MODELS["leads"]
            lists = sorted(
                s.scalars(
                    select(func.coalesce(model.data["list_name"].as_string(), ""))
                    .where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_campaign_id == campaign.id,
                    )
                    .distinct()
                )
            )
            catalog = campaign.data.get("lists", {})
            lists = sorted(
                name for name in set(lists) | set(catalog) if not catalog.get(name, {}).get("archived")
            )
        return {
            "lists": lists,
            "states": [{"code": code, "name": info["name"]} for code, info in STATES.items()],
            "inventory": {
                "total": len(numbers),
                "ready": ready,
                "pending_setup": sum(lifecycles[k] for k in ("purchased", "identity_configured")),
                "workspace_test_passed": test_passed,
                "limited_use": lifecycles["limited_real_use"],
                "paused_outbound": lifecycles["paused_for_outbound"] + lifecycles["retired_callback_only"],
                "investigating": lifecycles["investigating"],
                "lifecycles": dict(lifecycles),
            },
            "stable_assignments": stable,
        }

    @app.patch("/v1/admin/numbers/{number_id}")
    def edit_number(number_id: str, data: dict, p=Depends(administrator)):
        allowed = {
            "lifecycle",
            "reason",
            "daily_limit",
            "capacity",
            "identity_configured",
            "registration_reference",
            "real_use_review_reference",
            "request_id",
            "expected_version",
            "external_reputation",
        }
        if set(data) - allowed:
            fail(422, "Unrecognized number settings")
        return ops.edit_number(service, p, number_id, data)

    @app.post("/v1/admin/numbers/sync")
    async def sync_numbers(p=Depends(administrator)):
        return await ops.sync_numbers(service, p)

    @app.post("/v1/admin/numbers/{number_id}/configure")
    async def configure_number(number_id: str, data: dict | None = None, p=Depends(administrator)):
        return await ops.configure_owned_number(service, p, number_id, data)

    @app.post("/v1/admin/numbers/search")
    async def search_numbers(data: dict, p=Depends(administrator)):
        from decimal import Decimal, InvalidOperation

        from app.optimization.geography import (
            AREA_STATES,
            CITY_NAMES,
            METROS,
            normalize_state,
            suggested_codes,
        )

        region = str(data.get("region", ""))
        city = " ".join(str(data.get("city") or "").split())[:64] or None
        city = CITY_NAMES.get((city or "").casefold(), city)
        state = normalize_state(data.get("state"))
        if data.get("state") and not state:
            fail(422, "A US state is required")
        if state:

            def areas_for_list():
                with db.transaction(False) as s:
                    campaign = ops.selected_campaign(service, s, p)
                    leads = [
                        r.data
                        for r in ops.scoped(service, s, "leads", p)
                        if r.data["campaign_id"] == campaign.id
                        and (data.get("list_name") is None or data["list_name"] in memberships(r.data))
                    ]
                    return suggested_codes(state, leads)[:3]

            areas = await asyncio.to_thread(areas_for_list)
        elif re.fullmatch(r"[2-9][0-9]{2}", region):
            # A city's numbers may all sit in an overlay, so its area code alone can miss them.
            areas = (
                [region, *(c for c in METROS.get(region, "").split() if c != region)] if city else [region]
            )
        else:
            fail(422, "A US area code is required")

        def place_of(candidate):
            for where in candidate.get("region_information", []):
                if where.get("region_type") == "location":
                    return str(where.get("region_name", "")).title()
            return None

        def in_city(candidate):
            # Telnyx's nearby results ignore the city, so hold them to it here.
            place = place_of(candidate)
            return not city or not place or place.casefold().startswith(city.casefold())

        found, refused = [], 0
        carrier = ops.carrier(service, p["tenant_id"])
        # One at a time: Telnyx rate limits bursts, and one refused area code must not lose the rest.
        for area in areas:
            try:
                candidates = await carrier.search_numbers(area, city=city)
            except httpx.HTTPError:
                refused += 1
                continue
            found.extend((area, candidate) for candidate in candidates[:10] if in_city(candidate))
            if city and len({c.get("phone_number") for _, c in found}) >= 10:
                break
        if refused == len(areas):
            fail(502, "Telnyx did not answer this search. Try again in a moment.")
        found.sort(key=lambda item: str(item[1].get("phone_number", ""))[2:5] != item[0])

        def save_quotes():
            quotes = []
            seen = set()
            with db.transaction() as s:
                for area, candidate in found:
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
                    # A sold-out area code is answered with nearby numbers; keep those in the same state.
                    if (
                        phone in seen
                        or (phone[2:5] != area and AREA_STATES.get(phone[2:5]) != AREA_STATES.get(area, "?"))
                        or (state and AREA_STATES.get(phone[2:5]) != state)
                    ):
                        continue
                    seen.add(phone)
                    value = {
                        "phone": phone,
                        "region": area,
                        "area_code": phone[2:5],
                        "state": AREA_STATES.get(area),
                        "searched_city": city,
                        "city": place_of(candidate),
                        "monthly_cost_usd": float(monthly),
                        "initial_commitment_usd": float(upfront + monthly),
                        "expires_at": (clock() + timedelta(minutes=5)).isoformat(),
                        "currency": "USD",
                    }
                    row = put(
                        s,
                        "command_outbox",
                        p["tenant_id"],
                        p["seller_id"],
                        {"kind": "number_quote", "status": "quoted", **value},
                        now=clock(),
                    )
                    quotes.append({"id": row.id, **value})
            return quotes

        quotes = await asyncio.to_thread(save_quotes)
        return {"numbers": quotes, "simulation": settings.simulation, "searched_area_codes": areas}

    @app.get("/v1/admin/numbers/orders")
    def number_orders(p=Depends(administrator)):
        with db.transaction(False) as s:
            owned = {n.data["phone"]: n.data for n in ops.scoped(service, s, "number_inventory", p)}
            orders = []
            for row in ops.purchase_orders_in(service, s, p):
                number = owned.get(row.data["phone"], {})
                status = number.get("provider_status") if number.get("provider_present") is True else None
                ready = (
                    status == "active"
                    and number.get("configuration_status") == "confirmed"
                    and number.get("identity_configured") is True
                    and number.get("provider_recording_enabled") is False
                )
                orders.append({**doc(row), "number_status": status, "ready": ready})
            return orders

    @app.post("/v1/admin/numbers/purchase")
    async def purchase_number(data: NumberPurchase, p=Depends(administrator)):
        if not data.approve_recurring:
            fail(422, "Approve the displayed number and its recurring rental before ordering")
        return await ops.purchase_number(service, p, str(data.quote_id))

    @app.post("/v1/admin/numbers/orders/{order_id}/reconcile")
    async def reconcile_number_order(order_id: str, p=Depends(administrator)):
        return await ops.reconcile_number_order(service, p, order_id)

    @app.post("/v1/admin/recovery")
    async def recovery(p=Depends(administrator)):
        return await ops.recover(service, p)

    @app.get("/v1/admin/recovery")
    def recovery_items(p=Depends(administrator)):
        return ops.recovery_items(service, p)

    @app.post("/v1/admin/recovery/commands/{identity}")
    async def recovery_action(identity: str, data: RecoveryEdit, p=Depends(administrator)):
        return await ops.recovery_action(service, p, identity, **data.model_dump())

    @app.post("/v1/admin/recovery/agents/{identity}")
    async def recover_agent(identity: str, data: RecoveryEdit, p=Depends(administrator)):
        if data.action != "reconcile":
            fail(422, "Agent recovery requires carrier reconciliation")
        return await ops.reconcile_agent(service, p, identity, data.expected_version, data.provider_id)

    @app.post("/v1/admin/recovery/jobs/{identity}/retry")
    def retry_job(identity: str, data: JobRetry, p=Depends(administrator)):
        return ops.retry_job(service, p, identity, data.expected_attempts)

    @app.post("/v1/admin/jobs/run")
    async def run_jobs(p=Depends(administrator)):
        return await ops.run_jobs(service, p["tenant_id"])

    @app.post("/v1/admin/backup")
    def backup(p=Depends(administrator)):
        return ops.backup(service, p)

    @app.post("/v1/admin/restore")
    def restore(data: dict, p=Depends(administrator)):
        return ops.restore(service, p, data.get("backup_id", ""))

    @app.patch("/v1/leads/{lead_id}/endpoint")
    def correct_endpoint(lead_id: str, data: dict, p=Depends(administrator)):
        from app.compliance.phone import normalize_phone
        from app.optimization.geography import AREA_STATES

        phone = normalize_phone(data.get("phone", ""))
        if not phone.e164 or not data.get("evidence"):
            fail(422, "A valid replacement endpoint and evidence reference are required")
        with db.transaction() as session:
            lead = ops.required(service, session, "leads", lead_id, p)
            endpoint = next(
                (
                    r
                    for r in ops.scoped(service, session, "phone_endpoints", p)
                    if r.data["phone"] == phone.e164
                ),
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
            inferred_state = AREA_STATES.get(phone.e164[2:5]) if phone.e164.startswith("+1") else None
            location = (
                {}
                if lead.data.get("state_source") == "provided"
                else {
                    "state": inferred_state,
                    "state_source": "phone_area_code" if inferred_state else "unknown",
                }
            )
            update(
                lead,
                endpoint_id=endpoint.id,
                phone=phone.e164,
                region=phone.e164[2:5] if phone.e164.startswith("+1") else "general",
                status="eligible",
                next_attempt_at=clock().isoformat(),
                **location,
            )
            lead.external_key = lead.data["campaign_id"] + ":" + endpoint.id
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "lead.endpoint_corrected",
                lead.id,
                clock(),
                {
                    "endpoint_id": endpoint.id,
                    "previous_endpoint_id": old_endpoint,
                    "evidence": str(data["evidence"])[:1000],
                },
            )
            return doc(lead)

    @app.patch("/v1/tasks/{task_id}")
    def task_status(task_id: str, data: TaskEdit, p=Depends(principal)):
        return ops.edit_task(service, p, task_id, data.model_dump(mode="json", exclude_unset=True))

    @app.patch("/v1/leads/{lead_id}/outreach")
    def review_outreach(lead_id: str, data: OutreachEdit, p=Depends(principal)):
        return ops.resolve_goal_review(service, p, lead_id, data.model_dump(mode="json", exclude_unset=True))

    @app.post("/v1/calls/{call_id}/dtmf")
    async def dtmf(call_id: str, data: KeypadRequest, p=Depends(principal)):
        return await ops.send_keypad(service, p, call_id, data.digits, data.idempotency_key)

    @app.get("/v1/calls/{call_id}/dtmf/{request_id}")
    def keypad_status(call_id: str, request_id: str, p=Depends(principal)):
        return ops.keypad_status(service, p, call_id, request_id)

    @app.post("/v1/telephony/agent")
    async def agent_leg(p=Depends(principal)):
        return await audio_leg.create(service.ctx, p, service.managed)

    @app.get("/v1/telephony/agent/{provider_id:path}")
    def agent_status(provider_id: str, p=Depends(principal)):
        with db.transaction(False) as session:
            leg = audio_leg.find(session, p, provider_id=provider_id, rep_id=p["user_id"])
            if not leg:
                fail(404, "Verified agent leg not found")
            return {"state": leg.data["state"], "agent_call_control_id": provider_id}

    @app.post("/v1/admin/control/run")
    async def control_run(p=Depends(administrator)):
        return await ops.dispatch_controls(service, p)

    @app.post("/v1/admin/restore/reconcile")
    async def reconcile_restore(data: dict, p=Depends(administrator)):
        return await ops.reconcile_restore(service, p, data.get("review_reference"), data.get("provider_ids"))

    from app.optimization.api import register_optimization
    from app.optimization.script_lab import register_script_lab

    register_optimization(app, service, principal, administrator)
    register_script_lab(app, service, principal, administrator)
    return app
