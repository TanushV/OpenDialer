import asyncio
import base64
import hashlib
import json
import math
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.administration import Dialer
from app.compliance.operating import DNCAuthorization, bind_operating_api
from app.config import Settings
from app.db import Database, Event, audit, doc, put, rows, update
from app.measurement import WorkEdit
from app.providers.telnyx import verify_webhook
from app.rep_workflows import MeetingEdit, OutreachEdit, TaskEdit
from app.security import DUMMY_HASH, PASSWORDS, admin, decode_session, issue_session
from app.service import dt, fail
from app.telephony.callbacks import CallbackRoutingEdit
from app.telephony.reducer import TERMINAL


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoveryEdit(Strict):
    expected_version: int = Field(ge=1)
    action: Literal["reconcile", "end_call", "cancel_unsent"]
    provider_id: str | None = Field(default=None, min_length=1, max_length=2048)


class JobRetry(Strict):
    expected_attempts: int = Field(ge=0)


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


class PracticeRequest(Strict):
    scenario: str = Field(min_length=1, max_length=2000)


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


class Suppress(Strict):
    phone: str
    reason: Literal["opt_out", "wrong_party", "temporary_hold", "invalid_endpoint"] = "opt_out"
    event_id: str | None = None


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
    expected_version: int
    text: str = Field(max_length=20000)


class Disposition(Strict):
    outcome: Literal[
        "right_party",
        "gatekeeper",
        "wrong_party",
        "voicemail",
        "no_answer",
        "busy",
        "screening",
        "ivr",
        "callback_requested",
        "meeting_booked",
        "meeting_held",
        "not_interested",
        "opt_out",
        "invalid_endpoint",
        "temporary_failure",
        "qualified_conversation",
        "sale_won",
    ]
    callback_at: AwareDatetime | None = None
    expected_version: int | None = Field(default=None, ge=1)
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
    approved: bool | None = None
    reason: str = Field(default="", max_length=1000)
    review_reference: str | None = None
    registry_required: Literal[True] | None = None
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
    inference_mode: Literal["api", "litellm", "chatgpt_plan"] | None = None
    inference_base_url: str | None = None
    model: str | None = None
    research_extract_model: str | None = None
    script_write_model: str | None = None
    postcall_extract_model: str | None = None
    scorecard_review_model: str | None = None
    chatgpt_access_token: str | None = None
    groq_api_key: str | None = None
    groq_model: str | None = None
    coach_provider: Literal["openrouter", "static"] | None = None
    openrouter_api_key: str | None = None
    coach_model: str | None = Field(default=None, min_length=3, max_length=200)


SECRET_FIELDS = {
    "dnc_api_key",
    "telnyx_api_key",
    "openai_api_key",
    "groq_api_key",
    "openrouter_api_key",
    "chatgpt_access_token",
}


def create_app(settings=None, *, clock=None, carrier=None):
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not settings.secret_key:
        raise ValueError("A persistent OPENDIALER_SECRET_KEY is required; use the desktop or CLI bootstrap")
    clock = clock or (lambda: datetime.now(UTC))
    db = Database(settings.database_url, encryption_key=settings.secret_key)
    service = Dialer(db, settings, clock, carrier)
    service.privacy_cutover()

    @asynccontextmanager
    async def lifespan(app):
        service.coach_http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
        )
        from app.runtime import AuthorityRuntime

        runtime = AuthorityRuntime(service, queues=settings.worker_queues)
        if settings.worker_enabled:
            await runtime.start()
        try:
            yield
        finally:
            await runtime.close()
            service.raw.clear()
            await service.coach_http.aclose()
            db.engine.dispose()

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
            and host not in {"127.0.0.1", "localhost", "testserver", "["}
        ):
            return JSONResponse({"detail": "Local authority host rejected"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse({"detail": "Origin rejected"}, status_code=403)
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
            verified = PASSWORDS.verify(data.password, user.data["password_hash"] if user else DUMMY_HASH)
            if not verified or not user or user.data.get("disabled"):
                login_failures[key] = [*failures, time.monotonic()]
                fail(401, "Email or password is invalid")
            login_failures.pop(key, None)
            return issue_session(s, settings, user, clock())

    @app.post("/v1/auth/logout")
    def logout(p=Depends(principal)):
        with db.transaction() as s:
            db.authorize_stream(s, p)
            service.record_work_in(s, p, False)
            update(service.required(s, "sessions", p["session_id"], p), revoked=True)
        return {"logged_out": True}

    @app.get("/v1/workspace")
    def workspace(p=Depends(principal)):
        return service.snapshot(p)

    @app.get("/v1/metrics")
    def metrics(p=Depends(principal)):
        return service.metrics(p)

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
        with db.transaction(False) as s:
            call = service.required(s, "call_attempts", call_id, p)
            if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                fail(404, "Object not found")
            coach = [
                service.coach_content(p, doc(r))
                for r in service.scoped(s, "coach_events", p)
                if r.data["call_id"] == call_id
                and (
                    r.id == call.data.get("coach_preferences", {}).get("pinned_event_id")
                    and dt(r.data.get("raw_expires_at", r.data["expires_at"])) > clock()
                    or dt(r.data["expires_at"]) > clock()
                    and r.data["state_version"] == call.data["transcript_version"]
                )
            ]
            coach = [item for item in coach if item]
            pinned_id = call.data.get("coach_preferences", {}).get("pinned_event_id")
            pinned = next((item for item in coach if item["id"] == pinned_id), None)
            if pinned:
                coach = [{**pinned, "pinned": True, "expires_at": pinned["raw_expires_at"]}]
            segments = service.transcript_rows(s, p, call_id)
            state = next(
                (r.data for r in service.scoped(s, "conversation_states", p) if r.external_key == call_id),
                None,
            )
            return {
                **{k: v for k, v in doc(call).items() if k != "recording_url"},
                "transcript": segments,
                "raw_evidence_status": service.raw_status(call, segments),
                "coach": {
                    **coach[-1],
                    "remaining_ms": max(
                        0, int((dt(coach[-1]["expires_at"]) - clock()).total_seconds() * 1000)
                    ),
                }
                if coach and call.data["state"] not in TERMINAL | {"ENDING"}
                else None,
                "conversation_state": state,
            }

    @app.post("/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, p=Depends(principal)):
        return await service.hangup(p, call_id)

    @app.post("/v1/calls/{call_id}/disposition")
    def disposition(call_id: str, data: Disposition, p=Depends(principal)):
        return service.save_disposition(p, call_id, data.model_dump(mode="json"))

    @app.post("/v1/leads/{lead_id}/meeting")
    def report_meeting(lead_id: str, data: MeetingEdit, p=Depends(principal)):
        return service.report_meeting(p, lead_id, data.model_dump(exclude_unset=True))

    @app.post("/v1/suppressions")
    def suppress(data: Suppress, p=Depends(principal)):
        return service.suppress(p, data.phone, data.reason, event_id=data.event_id)

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
                visible = [
                    r
                    for r in service.scoped(s, "coach_events", p)
                    if r.data["call_id"] == call_id
                    and dt(r.data["expires_at"]) > clock()
                    and service.coach_content(p, doc(r))
                ]
                preferences["pinned_event_id"] = visible[-1].id if data.pinned and visible else None
            update(call, coach_preferences=preferences)
            return preferences

    @app.post("/v1/calls/{call_id}/coach")
    async def coach(call_id: str, data: CoachQuestion | None = None, p=Depends(principal)):
        return await service.coach_turn(p, call_id, urgent=True, question=data.question if data else None)

    @app.post("/v1/coaching/practice")
    async def practice(data: PracticeRequest, p=Depends(principal)):
        return await service.practice(p, data.scenario)

    @app.get("/v1/calls/{call_id}/analysis")
    def analysis(call_id: str, p=Depends(principal)):
        return service.analysis(p, call_id)

    @app.patch("/v1/calls/{call_id}/notes")
    def edit_notes(call_id: str, data: NoteEdit, p=Depends(principal)):
        return service.edit_notes(p, call_id, data.expected_version, data.text)

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
        provider = service.carrier(p["tenant_id"])
        with db.transaction(False) as s:
            user = service.required(s, "users", p["user_id"], p)
            credential = user.data.get("telephony_credential_id") or (
                service.vault.read(p["tenant_id"]).get("telephony_credential_id")
                if p["role"] in {"owner", "admin"}
                else None
            )
        if not credential and not settings.simulation:
            fail(503, "Scoped rep telephony credential is not configured")
        if not settings.simulation:
            await provider.verify_incoming_only_credential(credential)
        return {"token": await provider.token(credential or "simulated"), "simulation": settings.simulation}

    @app.post("/v1/admin/policies/{policy_id}/publish")
    def publish(policy_id: str, data: PolicyUpdate, p=Depends(administrator)):
        return service.publish(p, policy_id, data.model_dump(exclude_none=True))

    @app.get("/v1/admin/preflight")
    def preflight(p=Depends(administrator)):
        return service.preflight(p)

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

    @app.get("/v1/admin/settings")
    def read_settings(p=Depends(administrator)):
        config = service.vault.read(p["tenant_id"])
        own_profile = service.vault.read("plan-" + p["user_id"])
        config["chatgpt_access_token"] = own_profile.get("access_token", "")
        return {
            **{k: v for k, v in config.items() if k not in SECRET_FIELDS},
            "configured": {k: bool(config.get(k)) for k in SECRET_FIELDS},
        }

    @app.put("/v1/admin/settings")
    def write_settings(data: ProviderSettings, p=Depends(administrator)):
        values = data.model_dump(exclude_none=True)
        if values.get("dnc_seller_authorization"):
            values["dnc_seller_authorization"]["seller_id"] = p["seller_id"]
        from urllib.parse import urlsplit

        for key in ("webhook_url", "inference_base_url"):
            if values.get(key):
                parsed = urlsplit(values[key])
                if (
                    parsed.username
                    or parsed.password
                    or parsed.scheme not in {"https", "http"}
                    or not parsed.hostname
                ):
                    fail(422, "Endpoint requires a valid HTTP(S) URL without embedded credentials")
                if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    fail(422, "Remote provider endpoints require HTTPS")
        if "chatgpt_access_token" in values:
            service.vault.write("plan-" + p["user_id"], {"access_token": values.pop("chatgpt_access_token")})
        service.vault.write(p["tenant_id"], values)
        with db.transaction() as s:
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
    async def configure_number(number_id: str, p=Depends(administrator)):
        return await service.configure_owned_number(p, number_id)

    @app.post("/v1/admin/numbers/search")
    async def search_numbers(data: dict, p=Depends(administrator)):
        from decimal import Decimal, InvalidOperation

        region = str(data.get("region", ""))
        if not re.fullmatch(r"[2-9][0-9]{2}", region):
            fail(422, "A US area code is required")
        found = await service.carrier(p["tenant_id"]).search_numbers(region)
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
            if (
                not data.get("approved")
                or not data.get("review_reference")
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
        from app.ml.training import promotion_allowed

        with db.transaction() as s:
            model = service.required(s, "model_versions", model_id, p)
            candidate = {**model.data, "manual_approved": data.get("approved", False)}
            if not promotion_allowed(candidate):
                fail(409, "Model has not passed calibration, online trial, coverage and policy gates")
            for previous in service.scoped(s, "model_versions", p):
                if previous.data.get("active"):
                    update(previous, active=False, trial_active=False)
            update(model, **candidate, active=True, trial_active=False, ever_activated=True)
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "model.activated", model.id, clock())
            return doc(model)

    @app.post("/v1/calls/{call_id}/voicemail")
    async def voicemail(call_id: str, data: Voicemail, p=Depends(principal)):
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
        await service.dispatch_controls(p)
        return result

    @app.post("/webhooks/telnyx")
    async def webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("telnyx-signature-ed25519", "")
        timestamp = request.headers.get("telnyx-timestamp", "")
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
        if not owner:
            fail(401, "Webhook signature invalid or stale")
        try:
            event = json.loads(body)["data"]
        except (ValueError, KeyError, TypeError):
            fail(422, "Invalid provider event")
        return await service.accept_webhook(owner.tenant_id, event, hashlib.sha256(body).hexdigest())

    @app.post("/v1/admin/registry/import")
    def registry_import(data: dict, p=Depends(administrator)):
        from app.compliance.registry import RegistryBitmap

        npas = data.get("npas", [])
        if (
            data.get("kind") not in {"national", "state"}
            or not data.get("subscription_reference")
            or not npas
            or any(not isinstance(n, str) or len(n) != 3 or not n.isdigit() for n in npas)
        ):
            fail(422, "Seller-authorized subscription, dataset type and area-code coverage required")
        try:
            refreshed = dt(data["refreshed_at"])
            if not refreshed.tzinfo or refreshed > clock():
                fail(422, "Registry timestamp must be a known past UTC instant")
            bitmap = RegistryBitmap.build(data.get("numbers", []))
            if any(n[2:5] not in npas for n in data.get("numbers", [])):
                fail(422, "Registry includes an unauthorized area code")
            if data.get("checksum") and data["checksum"] != bitmap.checksum:
                fail(422, "Registry checksum mismatch")
        except (ValueError, KeyError, TypeError) as exc:
            fail(422, f"Invalid registry snapshot: {type(exc).__name__}")
        with db.transaction() as session:
            for previous in service.scoped(session, "registry_snapshots", p):
                if previous.data["kind"] == data["kind"] and previous.data.get(
                    "jurisdiction", ""
                ) == data.get("jurisdiction", ""):
                    update(previous, published=False)
            subscription = put(
                session,
                "registry_subscriptions",
                p["tenant_id"],
                p["seller_id"],
                {"reference": data["subscription_reference"], "npas": npas, "kind": data["kind"]},
                now=clock(),
            )
            snapshot = put(
                session,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": data["kind"],
                    "jurisdiction": data.get("jurisdiction", ""),
                    "npas": npas,
                    "subscription_id": subscription.id,
                    "refreshed_at": refreshed.isoformat(),
                    "published": True,
                    "checksum": bitmap.checksum,
                    "count": len(set(data.get("numbers", []))),
                    "bitmap": base64.b64encode(bitmap.dump()).decode(),
                    "synthetic": settings.simulation,
                },
                now=clock(),
            )
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "registry.published",
                snapshot.id,
                clock(),
                {"checksum": bitmap.checksum, "count": snapshot.data["count"]},
            )
            return {
                "id": snapshot.id,
                "published": True,
                "checksum": bitmap.checksum,
                "count": snapshot.data["count"],
            }

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
    def publish_playbook(data: dict, p=Depends(administrator)):
        from app.research.validation import BANNED

        with db.transaction() as session:
            seller = service.required(session, "sellers", p["seller_id"], p)
            if (
                not data.get("reason")
                or not data.get("opener")
                or not data.get("screening_intro")
                or seller.data["name"].casefold() not in data["screening_intro"].casefold()
                or BANNED.search(str(data))
            ):
                fail(422, "Reviewed truthful identity and publication reason required")
            cards = data.get("cards", [])
            if any(not c.get("id") or not c.get("text") or len(c["text"].split()) > 25 for c in cards):
                fail(422, "Cards require stable IDs and at most 25 guidance words")
            previous = service.approved_playbook(session, p)
            obj = put(
                session,
                "playbooks",
                p["tenant_id"],
                p["seller_id"],
                {
                    "name": str(data.get("name", "Business introduction"))[:200],
                    "opener": data["opener"][:600],
                    "screening_intro": data["screening_intro"][:400],
                    "cards": cards,
                    "claims": data.get("claims", []),
                    "approved": bool(data.get("approved")),
                    "reason": data["reason"],
                    "previous_id": previous.id if previous else None,
                },
                now=clock(),
            )
            obj.version = previous.version + 1 if previous else 1
            audit(
                session, p["tenant_id"], p["seller_id"], p["user_id"], "playbook.published", obj.id, clock()
            )
            return doc(obj)

    @app.patch("/v1/tasks/{task_id}")
    def task_status(task_id: str, data: TaskEdit, p=Depends(principal)):
        return service.edit_task(p, task_id, data.model_dump(exclude_unset=True))

    @app.patch("/v1/leads/{lead_id}/outreach")
    def review_outreach(lead_id: str, data: OutreachEdit, p=Depends(principal)):
        return service.resolve_goal_review(p, lead_id, data.model_dump(exclude_unset=True))

    @app.post("/v1/calls/{call_id}/dtmf")
    async def dtmf(call_id: str, data: dict, p=Depends(principal)):
        import re

        digits = data.get("digits", "")
        if not isinstance(digits, str) or not re.fullmatch(r"[0-9*#]{1,30}", digits):
            fail(422, "Keypad digits only")
        with db.transaction(False) as session:
            call = service.required(session, "call_attempts", call_id, p)
            if call.data["state"] in {"ENDED", "FINALIZED"}:
                fail(409, "Call is no longer active")
            if call.data["rep_id"] != p["user_id"] and p["role"] == "rep":
                fail(403, "Call belongs to another rep")
            provider_id = call.data["provider_id"]
        await service.carrier(p["tenant_id"]).action(
            provider_id, "send_dtmf", {"digits": digits, "command_id": str(__import__("uuid").uuid4())}
        )
        return {"sent": True}

    @app.post("/v1/admin/seller/approve")
    def approve_seller(data: dict, p=Depends(administrator)):
        if not data.get("identity_reference"):
            fail(422, "Verified business identity evidence is required")
        with db.transaction() as session:
            seller = service.required(session, "sellers", p["seller_id"], p)
            update(seller, approved=bool(data.get("approved")), identity_reference=data["identity_reference"])
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
    def consent(data: dict, p=Depends(administrator)):
        if not data.get("evidence_reference") or not data.get("permissions"):
            fail(422, "Permission scope and evidence required")
        with db.transaction() as session:
            endpoint = service.required(session, "phone_endpoints", data.get("endpoint_id"), p)
            return doc(
                put(
                    session,
                    "consent_evidence",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "endpoint_id": endpoint.id,
                        "permissions": data["permissions"],
                        "revoked": bool(data.get("revoked")),
                        "evidence_reference": data["evidence_reference"],
                        "acquired_at": data.get("acquired_at", clock().isoformat()),
                    },
                    now=clock(),
                )
            )

    @app.post("/v1/telephony/agent")
    async def agent_leg(p=Depends(principal)):
        return await service.create_agent(p)

    @app.get("/v1/telephony/agent/{provider_id:path}")
    def agent_status(provider_id: str, p=Depends(principal)):
        with db.transaction(False) as session:
            leg = next(
                (
                    r
                    for r in service.scoped(session, "call_legs", p)
                    if r.data.get("kind") == "agent"
                    and r.data.get("rep_id") == p["user_id"]
                    and r.data.get("provider_id") == provider_id
                ),
                None,
            )
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

    @app.post("/v1/admin/billing/reconcile")
    def reconcile_billing(data: dict, p=Depends(administrator)):
        if (
            not data.get("source_reference")
            or not isinstance(data.get("rows", []), list)
            or len(data.get("rows", [])) > 10000
        ):
            fail(422, "Bounded provider record batch and original source reference required")
        matched = duplicates = 0
        duration = 0
        with db.transaction() as session:
            for record in data.get("rows", []):
                if not isinstance(record, dict):
                    fail(422, "Billing rows must be objects")
                try:
                    cost = float(record.get("cost_usd", -1))
                    seconds = float(record.get("duration_seconds", -1))
                except (TypeError, ValueError):
                    fail(422, "Numeric billed amount and duration required")
                if (
                    not record.get("provider_record_id")
                    or isinstance(record.get("cost_usd"), bool)
                    or isinstance(record.get("duration_seconds"), bool)
                    or not math.isfinite(cost)
                    or cost < 0
                    or not math.isfinite(seconds)
                    or seconds < 0
                    or not seconds.is_integer()
                ):
                    fail(422, "Provider ID, nonnegative billed amount and duration required")
                callback = bool(record.get("callback_id"))
                if callback and record.get("call_id"):
                    fail(422, "Choose one call or callback billing identity")
                call = service.required(
                    session,
                    "followup_tasks" if callback else "call_attempts",
                    record.get("callback_id") if callback else record.get("call_id"),
                    p,
                )
                if callback and not call.data.get("inbound"):
                    fail(422, "Callback billing requires an inbound call")
                key = "cdr:" + record["provider_record_id"]
                entries = service.scoped(session, "usage_ledger", p)
                existing = next(
                    (
                        r
                        for r in entries
                        if r.external_key == key
                        or r.data.get("provider_record_id") == record["provider_record_id"]
                    ),
                    None,
                )
                if existing:
                    if callback and (
                        existing.data.get("object_id") != call.id
                        or existing.data.get("cost_usd") != float(record["cost_usd"])
                    ):
                        fail(409, "Billing receipt attribution conflicts")
                    duplicates += 1
                    continue
                values = {
                    "callback_id" if callback else "call_id": call.id,
                    "object_id": call.id,
                    "provider": "telnyx",
                    "provider_record_id": record["provider_record_id"],
                    "leg_id": record.get("leg_id"),
                    "kind": record.get("kind", "voice"),
                    "duration_seconds": int(record["duration_seconds"]),
                    "cost_usd": float(record["cost_usd"]),
                    "source_reference": data["source_reference"],
                    "billing_verified": True,
                    "status": "settled",
                    "reserved_usd": 0,
                    "at": clock().isoformat(),
                }
                hold = next(
                    (
                        r
                        for r in entries
                        if record.get("request_key") and r.data.get("request_key") == record["request_key"]
                    ),
                    None,
                )
                if callback and record.get("request_key"):
                    if (
                        not hold
                        or hold.data.get("object_id") != call.id
                        or hold.data.get("work_kind") != record.get("kind")
                    ):
                        fail(409, "Callback billing does not match the reserved action")
                    update(hold, **values)
                else:
                    put(session, "usage_ledger", p["tenant_id"], p["seller_id"], values, key=key, now=clock())
                for reserved in service.scoped(session, "usage_ledger", p):
                    if (
                        reserved.data.get("call_id") == call.id
                        and reserved.data.get("kind") == "call_authorization"
                    ):
                        update(
                            reserved,
                            reserved_usd=0,
                            reconciled_to_provider_records=True,
                            billing_verified=True,
                        )
                matched += 1
                duration += int(record["duration_seconds"])
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "billing.reconciled",
                p["tenant_id"],
                clock(),
                {"matched": matched, "duplicates": duplicates},
            )
        return {
            "matched": matched,
            "duplicates": duplicates,
            "duration_seconds": duration,
            "evidence": "operator-imported provider records",
            "invoice_verified": False,
        }

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
                return {"active": "pooled-beta-v1"}
            model = service.required(session, "model_versions", model_id, p)
            if (
                not model.data.get("ever_activated")
                or model.data.get("goal") != service.optimization_config(session, p)["conversion_goal"]
            ):
                fail(409, "Rollback requires a previously activated model for the current goal")
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
    def publish_snippet(data: dict, p=Depends(administrator)):
        with db.transaction() as session:
            call = service.required(session, "call_attempts", data.get("call_id"), p)
            segments = {seg["segment_id"] for seg in service.transcript_rows(session, p, call.id)}
            if (
                not data.get("evidence")
                or not set(data["evidence"]).issubset(segments)
                or not data.get("review_reference")
            ):
                fail(422, "Reviewed transcript evidence required before publishing a snippet")
            return doc(
                put(
                    session,
                    "snippets",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "call_id": call.id,
                        "evidence": data["evidence"],
                        "title": str(data.get("title", "Reviewed example"))[:200],
                        "review_reference": data["review_reference"],
                        "approved": True,
                    },
                    now=clock(),
                )
            )

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

    @app.post("/v1/inference/chatgpt/start")
    def chatgpt_start(request: Request, p=Depends(principal)):
        from app.providers.oauth import PlanOAuth

        if settings.mode != "local":
            fail(
                409,
                "Complete the official OAuth flow on your local desktop, then import your protected registration into your own server user profile",
            )
        profile_key = "plan-" + p["user_id"]
        profile = service.vault.read(profile_key)
        host_id = service.host_id()
        pending = PlanOAuth(host_id, clock).begin(
            str(request.base_url).rstrip("/") + "/auth/callback", profile
        )
        service.vault.write(profile_key, {"pending": pending})
        return {"authorization_url": pending["authorization_url"], "expires_in": 600}

    @app.get("/auth/callback")
    async def chatgpt_callback(
        state: str, code: str | None = None, client_id: str | None = None, error: str | None = None
    ):
        from fastapi.responses import HTMLResponse

        from app.providers.oauth import OAuthError, PlanOAuth

        if settings.mode != "local":
            fail(403, "Loopback authorization is local only")
        with db.transaction(False) as session:
            users = rows(session, "users")
        matched = next(
            (u for u in users if service.vault.read("plan-" + u.id).get("pending", {}).get("state") == state),
            None,
        )
        if not matched:
            fail(400, "OAuth state does not match a pending local authorization")
        key = "plan-" + matched.id
        pending = service.vault.read(key)["pending"]
        service.vault.write(key, {"pending": {**pending, "used": True}})
        try:
            result = await PlanOAuth(service.host_id(), clock).finish(
                pending, state=state, code=code, client_id=client_id, error=error
            )
        except (OAuthError, Exception) as exc:
            fail(400, "Authorization was not completed: " + type(exc).__name__)
        service.vault.write(key, {**result, "pending": None})
        service.vault.write(matched.tenant_id, {"inference_mode": "chatgpt_plan"})
        return HTMLResponse(
            "<!doctype html><title>OpenDialer authorization</title><h1>ChatGPT is connected to your OpenDialer user.</h1><p>You may return to the desktop application.</p>"
        )

    @app.post("/v1/inference/chatgpt/import")
    async def import_chatgpt_profile(data: dict, p=Depends(principal)):
        import httpx

        from app.providers.oauth import PlanOAuth

        if (
            data.get("client_id") == "dynamic_agent_client"
            or not data.get("access_token")
            or not data.get("id_token")
        ):
            fail(422, "A complete protected official registration is required")
        async with httpx.AsyncClient() as http:
            identity = await PlanOAuth(service.host_id(), clock).identity(
                data["id_token"], data["client_id"], http
            )
        if identity["sub"] != data.get("subject") or "chatgpt.tokens.use.direct" not in data.get(
            "scopes", []
        ):
            fail(403, "Identity or plan permission mismatch")
        service.vault.write("plan-" + p["user_id"], {**data, "ext_agent_host_id": service.host_id()})
        return {"imported": True, "client_id": data["client_id"], "personal_user_id": p["user_id"]}

    @app.get("/v1/inference/chatgpt/models")
    async def chatgpt_models(p=Depends(principal)):
        import httpx

        profile = service.vault.read("plan-" + p["user_id"])
        if not profile.get("access_token"):
            fail(409, "Sign in with your own eligible ChatGPT account first")
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": "Bearer " + profile["access_token"]},
            )
            response.raise_for_status()
            return {
                "models": [
                    {"slug": m["slug"], "display_name": m["display_name"]}
                    for m in response.json().get("models", [])
                    if m.get("visibility") == "list"
                ]
            }

    @app.post("/v1/admin/restore/reconcile")
    async def reconcile_restore(data: dict, p=Depends(administrator)):
        return await service.reconcile_restore(p, data.get("review_reference"), data.get("provider_ids"))

    @app.post("/v1/admin/models/{model_id}/trial")
    def save_trial(model_id: str, data: dict, p=Depends(administrator)):
        if (
            not data.get("report_reference")
            or not isinstance(data.get("policy_violations"), int)
            or "coverage_delta" not in data
        ):
            fail(
                422,
                "A reviewed controlled-trial report, coverage delta and policy violation count are required",
            )
        with db.transaction() as session:
            model = service.required(session, "model_versions", model_id, p)
            if model.data.get("source_dataset_id"):
                report_id = data.get("cohort_report_id")
                if not report_id:
                    fail(409, "A saved controlled cohort report is required for an operational candidate")
                report = service.required(session, "evaluation_reports", report_id, p)
                if (
                    report.data.get("kind") != "conversion_trial"
                    or report.data.get("simulation")
                    or not report.data.get("sufficient_evidence")
                    or report.data.get("goal") != model.data.get("goal")
                    or report.data.get("trial_model_id") != model.id
                ):
                    fail(409, "The saved report lacks sufficient independent live conversion evidence")
                data = {
                    **data,
                    "coverage_delta": report.data.get("coverage_delta", -1),
                    "policy_violations": report.data.get("policy_violations", 1),
                }
            update(
                model,
                online_trial_passed=bool(data.get("online_trial_passed")),
                report_reference=data["report_reference"],
                coverage_delta=float(data["coverage_delta"]),
                policy_violations=data["policy_violations"],
                trial_reviewed_by=p["user_id"],
                cohort_report_id=data.get("cohort_report_id"),
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
    from app.preparation.batches import register_batches

    register_batches(app, service, administrator)
    register_optimization(app, service, principal, administrator)
    return app
