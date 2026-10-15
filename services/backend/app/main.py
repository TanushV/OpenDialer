import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.administration import Dialer
from app.config import Settings
from app.db import Database, Event, audit, doc, get, put, rows, update
from app.providers.telnyx import verify_webhook
from app.security import DUMMY_HASH, PASSWORDS, admin, decode_session, issue_session
from app.service import dt, fail
from app.transcripts import normalize_transcription


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Setup(Strict):
    seller_name: str = Field(min_length=1, max_length=200)
    campaign_name: str = Field(default="My campaign", max_length=200)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=200)
    name: str = Field(default="Local operator", max_length=120)


class Login(Strict):
    email: str
    password: str


class Presence(Strict):
    status: Literal["ready", "paused", "busy"]
    audio_ready: bool = False
    telephony_ready: bool = False
    agent_call_control_id: str | None = None
    pending_optouts: int = Field(default=0, ge=0)


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
        "invalid_endpoint",
        "temporary_failure",
    ]
    callback_at: datetime | None = None
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
    registry_required: bool | None = None
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


SECRET_FIELDS = {"telnyx_api_key", "openai_api_key", "groq_api_key", "chatgpt_access_token"}


def create_app(settings=None, *, clock=None, carrier=None):
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not settings.secret_key:
        raise ValueError("A persistent OPENDIALER_SECRET_KEY is required; use the desktop or CLI bootstrap")
    clock = clock or (lambda: datetime.now(UTC))
    db = Database(settings.database_url)
    service = Dialer(db, settings, clock, carrier)
    stop = asyncio.Event()

    async def worker():
        while not stop.is_set():
            try:
                await service.run_jobs()
            except Exception as exc:
                logging.getLogger(__name__).warning("Worker tick deferred: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                continue

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker()) if settings.worker_enabled else None
        yield
        stop.set()
        if task:
            await task
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
        if settings.mode == "local" and host not in {"127.0.0.1", "localhost", "testserver", "["}:
            return JSONResponse({"detail": "Local authority host rejected"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse({"detail": "Origin rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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
                doc(r)
                for r in service.scoped(s, "coach_events", p)
                if r.data["call_id"] == call_id
                and dt(r.data["expires_at"]) > clock()
                and r.data["state_version"] == call.data["transcript_version"]
            ]
            return {
                **doc(call),
                "transcript": service.transcript_rows(s, p, call_id),
                "coach": coach[-1] if coach else None,
            }

    @app.post("/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, p=Depends(principal)):
        return await service.hangup(p, call_id)

    @app.post("/v1/calls/{call_id}/disposition")
    def disposition(call_id: str, data: Disposition, p=Depends(principal)):
        with db.transaction() as s:
            call = service.required(s, "call_attempts", call_id, p)
            if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                fail(403, "Call belongs to another rep")
            if call.data["state"] not in {"ENDED", "FINALIZED"}:
                fail(409, "End the call before wrap-up")
            update(
                call,
                outcome=data.outcome,
                state="FINALIZED",
                callback_at=data.callback_at.isoformat() if data.callback_at else None,
            )
            lead = service.required(s, "leads", call.data["lead_id"], p)
            phone = lead.data["phone"]
            if data.outcome in {"wrong_party", "invalid_endpoint"}:
                update(lead, status="needs_correction")
                endpoint = service.required(s, "phone_endpoints", lead.data["endpoint_id"], p)
                update(
                    endpoint,
                    **(
                        {"ownership_status": "wrong_party"}
                        if data.outcome == "wrong_party"
                        else {"reachability_status": "invalid_endpoint"}
                    ),
                )
            if data.outcome == "callback_requested":
                update(
                    lead,
                    callback_due_at=data.callback_at.isoformat() if data.callback_at else None,
                    next_attempt_at=data.callback_at.isoformat()
                    if data.callback_at
                    else lead.data["next_attempt_at"],
                )
                put(
                    s,
                    "followup_tasks",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "lead_id": lead.id,
                        "call_id": call_id,
                        "owner_id": p["user_id"],
                        "action": "Confirm callback time" if not data.callback_at else "Call back",
                        "due_at": data.callback_at.isoformat() if data.callback_at else None,
                        "note": data.callback_note,
                        "status": "open",
                    },
                    now=clock(),
                )
            put(
                s,
                "outcomes",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": call_id,
                    "lead_id": lead.id,
                    "outcome": data.outcome,
                    "at": clock().isoformat(),
                    "window_status": "pending",
                },
                now=clock(),
            )
            result = doc(call)
        if data.outcome in {"wrong_party", "invalid_endpoint"}:
            service.suppress(p, phone, data.outcome)
        return result

    @app.post("/v1/suppressions")
    def suppress(data: Suppress, p=Depends(principal)):
        return service.suppress(p, data.phone, data.reason, event_id=data.event_id)

    @app.post("/v1/leads/import")
    def import_leads(data: ImportLeads, p=Depends(administrator)):
        return service.import_leads(p, data.model_dump())

    @app.get("/v1/leads/{lead_id}/packet")
    def packet(lead_id: str, p=Depends(principal)):
        return service.packet(p, lead_id)

    @app.post("/v1/research/batches")
    def research(data: ResearchBatch, p=Depends(administrator)):
        return service.queue_research(p, data.lead_ids)

    @app.post("/v1/calls/{call_id}/transcript")
    async def transcript(call_id: str, data: Transcript, p=Depends(principal)):
        # Synthetic/manual corrections only. Live machine ingestion is signature-verified via webhooks.
        if not settings.simulation and p["role"] not in {"owner", "admin"}:
            fail(403, "Transcript correction requires administrator review")
        return await service.transcript(p, call_id, data.model_dump())

    @app.post("/v1/calls/{call_id}/coach")
    async def coach(call_id: str, p=Depends(principal)):
        return await service.coach_turn(p, call_id, urgent=True)

    @app.get("/v1/calls/{call_id}/analysis")
    def analysis(call_id: str, p=Depends(principal)):
        return service.analysis(p, call_id)

    @app.patch("/v1/calls/{call_id}/notes")
    def edit_notes(call_id: str, data: NoteEdit, p=Depends(principal)):
        return service.edit_notes(p, call_id, data.expected_version, data.text)

    @app.post("/v1/scorecards/{scorecard_id}/review")
    def review_score(scorecard_id: str, data: dict, p=Depends(principal)):
        if not data.get("reason"):
            fail(422, "Review reason required")
        with db.transaction() as s:
            card = service.required(s, "scorecards", scorecard_id, p)
            segments = {r["segment_id"] for r in service.transcript_rows(s, p, card.data["call_id"])}
            if not set(data.get("evidence", [])).issubset(segments):
                fail(422, "Review evidence does not exist in this call")
            obj = put(
                s,
                "reviews",
                p["tenant_id"],
                p["seller_id"],
                {
                    "scorecard_id": scorecard_id,
                    "reviewer_id": p["user_id"],
                    "reason": str(data["reason"])[:1000],
                    "dimension": data.get("dimension"),
                    "status": data.get("status", "disputed"),
                    "evidence": data.get("evidence", []),
                    "at": clock().isoformat(),
                },
                now=clock(),
            )
            return doc(obj)

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
            result = [
                {
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "tenant_id": e.tenant_id,
                    "type": e.type,
                    "entity_id": e.entity_id,
                    "entity_version": e.entity_version,
                    "occurred_at": e.occurred_at.isoformat(),
                    "payload": e.payload,
                }
                for e in found
            ]
            return {"events": result, "cursor": found[-1].sequence if found else after}

    @app.websocket("/v1/live")
    async def live(ws: WebSocket):
        if ws.headers.get("origin") and ws.headers["origin"] not in settings.allowed_origins:
            await ws.close(code=4403)
            return
        await ws.accept()
        try:
            auth = await asyncio.wait_for(ws.receive_json(), 10)
            p = decode_session(db, settings, auth.get("token", ""), clock())
            cursor = int(auth.get("after", 0))
            while True:
                decode_session(db, settings, auth["token"], clock())
                batch = events(cursor, p)
                if batch["events"]:
                    await ws.send_json(batch)
                    cursor = batch["cursor"]
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, HTTPException, TimeoutError, ValueError):
            try:
                await ws.close(code=4401)
            except RuntimeError:
                pass

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
            credential = user.data.get("telephony_credential_id") or service.vault.read(p["tenant_id"]).get(
                "telephony_credential_id"
            )
        if not credential and not settings.simulation:
            fail(503, "Scoped rep telephony credential is not configured")
        return {"token": await provider.token(credential or "simulated"), "simulation": settings.simulation}

    @app.post("/v1/admin/policies/{policy_id}/publish")
    def publish(policy_id: str, data: PolicyUpdate, p=Depends(administrator)):
        return service.publish(p, policy_id, data.model_dump(exclude_none=True))

    @app.get("/v1/admin/preflight")
    def preflight(p=Depends(administrator)):
        return service.preflight(p)

    @app.get("/v1/admin/settings")
    def read_settings(p=Depends(administrator)):
        config = service.vault.read(p["tenant_id"])
        return {
            **{k: v for k, v in config.items() if k not in SECRET_FIELDS},
            "configured": {k: bool(config.get(k)) for k in SECRET_FIELDS},
        }

    @app.put("/v1/admin/settings")
    def write_settings(data: ProviderSettings, p=Depends(administrator)):
        values = data.model_dump(exclude_none=True)
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
        return service.proposal(p, data["campaign_id"], data.get("daily_capacity_per_number", 40))

    @app.post("/v1/admin/numbers/sync")
    async def sync_numbers(p=Depends(administrator)):
        inventory = await service.carrier(p["tenant_id"]).list_numbers()
        added = 0
        for n in inventory:
            try:
                number = service.add_number(p, {"phone": n["phone_number"], "region": n["phone_number"][2:5]})
                with db.transaction() as s:
                    update(service.required(s, "number_inventory", number["id"], p), provider_id=n["id"])
                added += 1
            except HTTPException as e:
                if e.status_code != 409:
                    raise
        return {"added": added, "provider_count": len(inventory)}

    @app.post("/v1/admin/numbers/purchase")
    async def purchase_numbers(data: dict, p=Depends(administrator)):
        numbers, budget = data.get("phone_numbers", []), data.get("approved_budget_usd", 0)
        if not numbers or budget < len(numbers):
            fail(403, "Number purchase requires an adequate operator-approved budget")
        with db.transaction() as s:
            configured = service.scoped(s, "budgets", p)[0].data.get("number_purchase_limit_usd", 0)
            if budget > configured:
                fail(403, "Number purchase exceeds configured procurement budget")
            key = "purchase:" + str(data.get("idempotency_key", ""))
            prior = next((c for c in service.scoped(s, "command_outbox", p) if c.external_key == key), None)
            if prior:
                return doc(prior)
            intent = put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {"kind": "purchase", "status": "sending", "numbers": numbers, "approved_budget_usd": budget},
                key=key,
                now=clock(),
            )
        try:
            receipt = await service.carrier(p["tenant_id"]).purchase(numbers, intent.id)
        except Exception:
            with db.transaction() as s:
                update(service.required(s, "command_outbox", intent.id, p), status="uncertain")
            fail(503, "Purchase outcome uncertain; reconcile the existing order before retrying")
        with db.transaction() as s:
            update(service.required(s, "command_outbox", intent.id, p), status="completed", receipt=receipt)
        for n in numbers:
            service.add_number(p, {"phone": n, "lifecycle": "purchased"})
        return {"order_id": receipt.get("id"), "purchased": True}

    @app.post("/v1/admin/recovery")
    async def recovery(p=Depends(administrator)):
        return await service.recover(p)

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
                    update(previous, active=False)
            update(model, **candidate, active=True)
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "model.activated", model.id, clock())
            return doc(model)

    @app.post("/v1/calls/{call_id}/voicemail")
    async def voicemail(call_id: str, data: Voicemail, p=Depends(principal)):
        from app.compliance.policy import authorize

        with db.transaction() as s:
            call = service.required(s, "call_attempts", call_id, p)
            if call.data["state"] != "VOICEMAIL_READY":
                fail(409, "MAILBOX_NOT_READY")
            lead = service.required(s, "leads", call.data["lead_id"], p)
            number = service.required(s, "number_inventory", call.data["number_id"], p)
            campaign = service.required(s, "campaigns", lead.data["campaign_id"], p)
            policy = service.required(s, "policy_versions", campaign.data["policy_id"], p).data
            decision = authorize(service.context(s, p, lead, number, permission="prerecorded", active=True))
            if (
                not decision.allowed
                or not policy.get("voicemail_enabled")
                or not policy.get("voicemail_optout_supported")
                or data.asset_id not in policy.get("approved_assets", [])
            ):
                fail(403, "PRERECORDED_PERMISSION_OR_ASSET_NOT_APPROVED")
            if data.detach and (
                not policy.get("detached_topology_verified")
                or not policy.get("late_human_recovery_destination")
            ):
                fail(403, "DETACHED_TOPOLOGY_AND_HUMAN_RECOVERY_NOT_VERIFIED")
            update(
                call,
                state="VM_PENDING",
                detach_requested=data.detach,
                voicemail_asset=data.asset_id,
                voicemail_deadline=(clock() + timedelta(seconds=120)).isoformat(),
            )
            provider_id = call.data["provider_id"]
            result = doc(call)
        await service.carrier(p["tenant_id"]).playback(
            provider_id,
            {
                "media_name": data.asset_id,
                "target_legs": "self",
                "command_id": call_id + ":voicemail:" + data.asset_id,
            },
        )
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
            payload = event["payload"]
        except (ValueError, KeyError):
            fail(422, "Invalid provider event")
        with db.transaction(False) as s:
            calls = rows(s, "call_attempts", owner.tenant_id)
            call = next(
                (c for c in calls if c.data.get("provider_id") == payload.get("call_control_id")), None
            )
            if not call and payload.get("client_state"):
                try:
                    context = json.loads(base64.b64decode(payload["client_state"]))
                    call = get(s, "call_attempts", context.get("call_id"), owner.tenant_id)
                except (ValueError, TypeError):
                    pass
            if not call:
                return {"received": True, "unmatched": True}
            p = {
                "tenant_id": call.tenant_id,
                "seller_id": call.seller_id,
                "user_id": call.data["rep_id"],
                "role": "rep",
            }
        event_type = event["event_type"]
        if "machine" in event_type and payload.get("result"):
            event_type += ":" + payload["result"]
        result = service.event(p, event["id"], event_type, call.id, payload)
        if event["event_type"] == "call.transcription" and not result["duplicate"]:
            for segment in normalize_transcription(payload, call.id, event["id"]):
                segment.pop("content_hash", None)
                segment.pop("call_id", None)
                await service.transcript(p, call.id, segment)
        if event["event_type"] == "call.hangup":
            service.jobs.enqueue(
                "postcall",
                {"call_id": call.id, "seller_id": p["seller_id"], "user_id": p["user_id"]},
                "postcall-webhook:" + event["id"],
                tenant_id=p["tenant_id"],
            )
        return result

    return app
