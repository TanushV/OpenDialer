"""Outgoing control delivery. Auth refresh belongs to the supported Supabase SDK."""

import asyncio
import logging
import re
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.calling.blockers import CATALOG, blocked, refuse
from app.calling.relay import after_exchange, after_failure
from app.calling.state import TERMINAL
from app.platform.db import MODELS
from app.platform.secret_files import read_private, write_private
from app.service import dt, fail

FILE_SECRETS = (
    "telnyx_api_key",
    "openrouter_api_key",
    "hubspot_service_key",
    "treg_token",
    "realvalidito_api_key",
    "realvalidito_api_secret",
)


class ConnectionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    label: str = Field(min_length=1, max_length=100)
    url: str
    publishable_key: str = Field(min_length=10, max_length=512)
    workspace_id: UUID
    email: str = Field(min_length=3, max_length=254)
    telnyx_api_key: str = Field(default="", max_length=512, repr=False)
    openrouter_api_key: str = Field(default="", max_length=512, repr=False)
    password: str = Field(min_length=12, max_length=512, repr=False)
    # Keys a setup file may carry, so a new computer needs no retyping.
    hubspot_service_key: str = Field(default="", max_length=512, repr=False)
    treg_token: str = Field(default="", max_length=512, repr=False)
    realvalidito_api_key: str = Field(default="", max_length=200, repr=False)
    realvalidito_api_secret: str = Field(default="", max_length=200, repr=False)

    @field_validator("url")
    @classmethod
    def project_url(cls, value):
        if not re.fullmatch(r"https://[a-z]{20}\.supabase\.co", value):
            raise ValueError("Use the Supabase project HTTPS URL")
        return value

    @field_validator("publishable_key")
    @classmethod
    def public_key(cls, value):
        if not value.startswith("sb_publishable_"):
            raise ValueError("Use a publishable key, never an admin key")
        return value


class SessionStorage:
    """SDK storage interface; secrets never enter normal settings or API responses."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return read_private(self.path)

    async def get_item(self, key):
        return self.read().get(key)

    async def set_item(self, key, value):
        write_private(self.path, {**self.read(), key: value})

    async def remove_item(self, key):
        values = self.read()
        values.pop(key, None)
        write_private(self.path, values)


async def deliver(service, tenant_id, events):
    """A crash before remote ack causes safe local deduplication on the next fetch."""
    acknowledgements = []
    for row in events:
        await service.accept_webhook(
            tenant_id, row["event"], row["hash"], verified=True, received_at=dt(row["received_at"])
        )
        acknowledgements.append({"account_id": row["account_id"], "event_id": row["event_id"]})
    return acknowledgements


# The relay lease lasts 45 seconds; an exchange at least every 20 keeps it, and a 35-second-old one is still fresh.
POLL_SECONDS = 20
EXCHANGE_FRESH_SECONDS = 35
UNJOINED_POLL_SECONDS = 5  # Without Realtime wake-ups the exchange polls this often.
RECONNECT_SECONDS = 5  # A failed exchange retries after this long.


class ManagedConnection:
    def __init__(self, service):
        self.service = service
        self.path = service.settings.data_dir / "managed-connection.json"
        self.storage = SessionStorage(service.settings.data_dir / "managed-session.json")
        self.config = read_private(self.path)
        self.installation = installation_id(service.settings.data_dir)
        self.task = None
        self.client = None
        # When an HTTP exchange with the relay last succeeded. This, not Realtime, gates new dials (R4.7).
        self.last_exchange = 0.0
        self.caught_up = False
        self.state = "disconnected"
        self.wake = asyncio.Event()
        self.joined = asyncio.Event()
        self.numbers = set()
        self.edit_lock = asyncio.Lock()
        self.speech_failed = False
        self.configuring = False

    def status(self):
        connected = bool(
            self.caught_up
            and self.last_exchange
            and monotonic() - self.last_exchange < EXCHANGE_FRESH_SECONDS
        )
        return {
            "configured": bool(self.config),
            "label": self.config.get("label", ""),
            # Speech is a warning only: a missing transcript never stops calling.
            "connected": connected,
            "state": self.state,
            "reason": None if connected else self.refusal(),
            "url": self.config.get("url", ""),
            "numbers": len(self.numbers),
            "speech_failed": self.speech_failed,
            "speech_warning": (
                "Speech was missing on a recent call. Calling continues; that call is marked as missing speech."
                if self.speech_failed
                else None
            ),
        }

    def refusal_code(self):
        """Why new calls are paused, as a blocker code (see `calling/blockers.py`)."""
        if self.configuring:
            return "CONNECTION_SETUP_CHANGING"
        if self.state == "sign_in_required":
            return "CONNECTION_SIGN_IN_REQUIRED"
        if self.state == "in_use_elsewhere":
            return "CONNECTION_IN_USE_ELSEWHERE"
        if self.state == "delivery_gap" or (self.last_exchange and not self.caught_up):
            return "CONNECTION_DELIVERY_GAP"
        return "CONNECTION_RECONNECTING"

    def refusal(self):
        """The real reason new calls are paused, in the rep's words."""
        return CATALOG[self.refusal_code()][0]

    def connection_code(self, tenant_id):
        """None while call events flow for this workspace, else the blocker code."""
        if self.configuring:
            return "CONNECTION_SETUP_CHANGING"
        if not self.enabled(tenant_id) or self.status()["connected"]:
            return None
        return self.refusal_code()

    def enabled(self, tenant_id):
        return self.config.get("tenant_id") == tenant_id

    def assigned(self, tenant_id, numbers):
        """Numbers on this workspace's Telnyx app, or already seen by its relay."""
        connection = self.service.configs.read(tenant_id).get("telnyx_connection_id")
        return [
            n
            for n in numbers
            if n.get("phone") in self.numbers or (connection and n.get("connection_id") == connection)
        ]

    def require_number(self, tenant_id, number):
        if self.enabled(tenant_id):
            self.require_ready(tenant_id)
            if not self.assigned(tenant_id, [number]):
                fail(409, "This number is not assigned to this workspace")

    async def settle_gap(self, batch, acknowledgements):
        """Clear the relay's delivery gap once nothing here is waiting on the missed events (see `after_exchange`).

        The relay marks a gap when events sat unacknowledged for a day, usually because the app was closed. Before
        November 9 only a Reconnect button cleared it, and that button was hidden while connected.
        """
        if not batch.get("gap_since") or acknowledgements:
            return False
        caught = await asyncio.to_thread(self.local_caught_up)
        idle = not await asyncio.to_thread(idle_reason, self.service, self.config["tenant_id"])
        nxt = after_exchange(gap=True, acknowledged=False, local_caught_up=caught, idle=idle)
        if nxt.clear_gap:
            await self.client.rpc("relay_clear_gap", {}).execute()
            logging.getLogger(__name__).info("Relay delivery gap cleared: nothing was pending locally")
        return nxt.clear_gap

    def local_caught_up(self):
        with self.service.db.transaction(False) as session:
            inbox = MODELS["webhook_inbox"]
            return not session.scalar(
                select(inbox.id)
                .where(
                    inbox.tenant_id == self.config["tenant_id"],
                    inbox.op_status.in_(["received", "processing", "retry"]),
                )
                .limit(1)
            )

    def require_ready(self, tenant_id):
        code = self.connection_code(tenant_id)
        if code:
            refuse([blocked(code)])

    async def make_client(self, config, storage=None):
        from supabase import AsyncClientOptions, acreate_client

        client = await acreate_client(
            config["url"],
            config["publishable_key"],
            AsyncClientOptions(storage=storage or self.storage, postgrest_client_timeout=10),
        )
        # Restoring storage alone sets HTTP auth, but does not initialize refresh or Realtime.
        await client.auth.initialize_from_storage()
        session = await client.auth.get_session()
        if session:
            await client.realtime.set_auth(session.access_token)
        return client

    async def close(self):
        self.last_exchange = 0
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client:
            await self.client.remove_all_channels()
            await self.client.realtime.close()
            await self.client.auth.close()
            await self.client.postgrest.aclose()
            self.client = None

    async def connect(self, tenant_id, profile):
        await self.close()
        values = profile.model_dump(mode="json", exclude={"password", *FILE_SECRETS})
        staged = SessionStorage(self.storage.path.with_suffix("." + str(uuid4()) + ".staged"))
        candidate = await self.make_client(values, staged)
        try:
            result = await candidate.auth.sign_in_with_password(
                {"email": profile.email, "password": profile.password}
            )
            if not result.user or str(result.user.id) != str(profile.workspace_id):
                fail(403, "Profile does not match its workspace")
            row = (
                await candidate.table("relay_workspaces")
                .select("id,settings")
                .eq("id", str(profile.workspace_id))
                .execute()
            )
            if not row.data:
                fail(403, "Workspace disabled or not provisioned")
            await asyncio.to_thread(require_idle, self.service, {"tenant_id": tenant_id})
        except Exception:
            await candidate.auth.close()
            await candidate.postgrest.aclose()
            staged.path.unlink(missing_ok=True)
            await self.start()
            fail(400, "Connection failed; check the profile and network")
        settings = row.data[0].get("settings", {})
        same = self.config.get("workspace_id") == values["workspace_id"]
        self.config = {
            **values,
            "tenant_id": tenant_id,
            "store_id": self.config["store_id"] if same else str(uuid4()),
        }
        staged.path.replace(self.storage.path)
        staged.path = self.storage.path
        self.storage = staged
        write_private(self.path, self.config)
        self.client = candidate
        configured = {
            k: v
            for k, v in settings.items()
            if k in {"telnyx_connection_id", "telnyx_public_key", "telephony_credential_id", "sip_username"}
        }
        configured.update({k: getattr(profile, k) for k in FILE_SECRETS if getattr(profile, k)})
        configured["webhook_url"] = profile.url + "/functions/v1/telnyx-webhook"
        previous = self.service.configs.read(tenant_id).get("telnyx_connection_id")
        self.service.configs.write(tenant_id, configured, advance_version=True)
        if previous and configured.get("telnyx_connection_id") not in {None, previous}:
            self.service.forget_workspace_test(tenant_id)
        self.speech_failed = False
        await self.start()
        return self.status()

    async def start(self):
        if self.config and not self.task:
            self.task = asyncio.create_task(self.run(), name="opendialer:managed-events")

    def broadcast(self, message):
        """Realtime only wakes the exchange loop sooner; it never decides whether calling may proceed."""
        if message.get("payload", {}).get("pending"):
            self.wake.set()

    def subscription(self, state, error):
        if str(state) in {"SUBSCRIBED", "RealtimeSubscribeStates.SUBSCRIBED"}:
            self.joined.set()
        else:
            self.joined.clear()
        self.wake.set()

    async def exchange(self, acknowledgements=()):
        return (
            await self.client.rpc(
                "relay_exchange",
                {
                    "store": self.config["store_id"],
                    "installation": self.installation,
                    "acknowledgements": list(acknowledgements),
                    "probe": "",
                },
            ).execute()
        ).data

    async def listen(self, topic):
        """Subscribe for wake-ups. A failure here leaves the HTTP exchange polling on its own."""
        try:
            await self.client.remove_all_channels()
            self.joined.clear()
            channel = self.client.channel(topic, {"config": {"private": True}})
            await channel.on_broadcast("wake", self.broadcast).subscribe(self.subscription)
            await asyncio.wait_for(self.joined.wait(), 15)
        except Exception as exc:
            logging.getLogger(__name__).warning("Realtime wake-ups unavailable: %s", type(exc).__name__)

    async def run(self):
        while True:
            self.caught_up = False
            try:
                if self.client is None:
                    self.client = await self.make_client(self.config)
                if not await self.client.auth.get_session():
                    self.state = "sign_in_required"
                    return
                first = await self.exchange()
                await self.listen(first["topic"])
                acknowledgements = []
                while True:
                    self.wake.clear()
                    batch = await self.exchange(acknowledgements)
                    if batch["topic"] != first["topic"]:
                        raise RuntimeError("Subscription changed")
                    self.last_exchange = monotonic()
                    acknowledgements = await deliver(self.service, self.config["tenant_id"], batch["events"])
                    self.numbers = set(batch.get("numbers", []))
                    gap = bool(batch.get("gap_since"))
                    if not acknowledgements and not gap:
                        for _ in range(
                            20
                        ):  # Applied events finish their local jobs within about two seconds.
                            if await asyncio.to_thread(self.local_caught_up):
                                break
                            await asyncio.sleep(0.1)
                    caught = await asyncio.to_thread(self.local_caught_up)
                    idle = gap and not await asyncio.to_thread(
                        idle_reason, self.service, self.config["tenant_id"]
                    )
                    nxt = after_exchange(
                        gap=gap, acknowledged=bool(acknowledgements), local_caught_up=caught, idle=idle
                    )
                    self.caught_up, self.state = nxt.caught_up, nxt.state
                    if nxt.clear_gap:
                        await self.client.rpc("relay_clear_gap", {}).execute()
                        logging.getLogger(__name__).info(
                            "Relay delivery gap cleared: nothing was pending locally"
                        )
                    if nxt.again:
                        continue
                    if not self.joined.is_set():
                        await self.listen(first["topic"])
                    try:
                        # Realtime wakes this early; without it, polling keeps the exchange fresh.
                        await asyncio.wait_for(
                            self.wake.wait(), POLL_SECONDS if self.joined.is_set() else UNJOINED_POLL_SECONDS
                        )
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_exchange = 0
                self.state = after_failure(exc)
                logging.getLogger(__name__).warning("Relay exchange failed: %s", self.state)
                if self.client:
                    await self.client.remove_all_channels()
                await asyncio.sleep(RECONNECT_SECONDS)


def installation_id(data_dir):
    """This data folder's relay identity. It survives restarts, so a restarted backend keeps its relay lease."""
    path = data_dir / "relay-installation"
    try:
        value = path.read_text().strip()
        if len(value) == 36:
            return value
    except OSError:
        pass
    value = str(uuid4())
    try:
        path.write_text(value)
    except OSError as exc:
        logging.getLogger(__name__).warning("Relay identity not saved: %s", type(exc).__name__)
    return value


def idle_reason(service, tenant_id):
    """Why setup or a gap reset must wait (a call, an inbound call or a calling operation still open), else None."""
    with service.db.transaction(False) as session:
        calls = MODELS["call_attempts"]
        if session.scalar(
            select(calls.id).where(calls.tenant_id == tenant_id, calls.op_status.not_in(TERMINAL)).limit(1)
        ):
            return "Finish or reconcile the current call before changing setup"
        tasks = MODELS["followup_tasks"]
        if session.scalar(
            select(tasks.id)
            .where(
                tasks.tenant_id == tenant_id,
                tasks.data["inbound_state"]
                .as_string()
                .in_(["answering", "ringing", "connected", "notifying", "ending"]),
            )
            .limit(1)
        ):
            return "Finish the incoming call before changing setup"
        commands = MODELS["command_outbox"]
        if session.scalar(
            select(commands.id)
            .where(
                commands.tenant_id == tenant_id,
                commands.op_status.in_(["pending", "sending", "reconciling", "review_required"]),
            )
            .limit(1)
        ):
            return "Resolve pending calling operations before changing setup"
    return None


def require_idle(service, p):
    reason = idle_reason(service, p["tenant_id"])
    if reason:
        fail(409, reason)


def bind_api(app, service, principal, administrator):
    @app.get("/v1/managed-connection")
    def status(p=Depends(principal)):
        return (
            service.managed.status()
            if service.managed.enabled(p["tenant_id"])
            else {"configured": False, "connected": False, "state": "direct"}
        )

    @app.post("/v1/admin/managed-connection")
    async def connect(profile: ConnectionProfile, p=Depends(administrator)):
        async with service.managed.edit_lock:
            service.managed.configuring = True
            try:
                await asyncio.to_thread(require_idle, service, p)
                return await service.managed.connect(p["tenant_id"], profile)
            finally:
                service.managed.configuring = False

    @app.post("/v1/admin/managed-connection/retry")
    async def retry(p=Depends(administrator)):
        await asyncio.to_thread(require_idle, service, p)
        if service.managed.client:
            await service.managed.client.rpc("relay_clear_gap", {}).execute()
        service.managed.speech_failed = False
        await service.managed.close()
        await service.managed.start()
        return service.managed.status()
