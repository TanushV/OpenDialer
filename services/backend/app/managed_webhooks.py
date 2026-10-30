"""Outgoing control delivery. Auth refresh belongs to the supported Supabase SDK."""

import asyncio
import re
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.db import MODELS
from app.secret_files import read_private, write_private
from app.service import dt, fail
from app.telephony.reducer import TERMINAL


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


class ManagedConnection:
    def __init__(self, service):
        self.service = service
        self.path = service.settings.data_dir / "managed-connection.json"
        self.storage = SessionStorage(service.settings.data_dir / "managed-session.json")
        self.config = read_private(self.path)
        self.installation = str(uuid4())
        self.task = None
        self.client = None
        self.last_probe = 0.0
        self.caught_up = False
        self.state = "disconnected"
        self.wake = asyncio.Event()
        self.joined = asyncio.Event()
        self.probe = ""
        self.numbers = set()
        self.edit_lock = asyncio.Lock()
        self.speech_failed = False
        self.configuring = False

    def status(self):
        return {
            "configured": bool(self.config),
            "label": self.config.get("label", ""),
            "connected": bool(
                not self.speech_failed
                and self.caught_up
                and self.last_probe
                and monotonic() - self.last_probe < 35
            ),
            "state": self.state,
            "url": self.config.get("url", ""),
            "numbers": len(self.numbers),
            "speech_failed": self.speech_failed,
        }

    def enabled(self, tenant_id):
        return self.service.settings.mode == "local" and self.config.get("tenant_id") == tenant_id

    def require_number(self, tenant_id, phone):
        if self.enabled(tenant_id):
            self.require_ready(tenant_id)
            if phone not in self.numbers:
                fail(409, "This number is not assigned to this workspace")

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
        if self.configuring:
            fail(409, "Calling setup is being changed; new calls are paused")
        if self.enabled(tenant_id) and not self.status()["connected"]:
            fail(409, "Call events reconnecting; new calls paused. Hangup remains available.")

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
        self.last_probe = 0
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
        if self.service.settings.mode != "local":
            fail(409, "Managed delivery is for local workspaces")
        await self.close()
        values = profile.model_dump(mode="json", exclude={"password", "telnyx_api_key", "openrouter_api_key"})
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
        configured.update(
            {k: getattr(profile, k) for k in ("telnyx_api_key", "openrouter_api_key") if getattr(profile, k)}
        )
        configured["webhook_url"] = profile.url + "/functions/v1/telnyx-webhook"
        self.service.configs.write(tenant_id, configured, advance_version=True)
        self.speech_failed = False
        await self.start()
        return self.status()

    async def start(self):
        if self.config and self.service.settings.mode == "local" and not self.task:
            self.task = asyncio.create_task(self.run(), name="opendialer:managed-events")

    def broadcast(self, message):
        payload = message.get("payload", {})
        if self.probe and payload.get("probe") == self.probe:
            self.last_probe = monotonic()
            self.state = "connected"
        if payload.get("pending"):
            self.wake.set()

    def subscription(self, state, error):
        if str(state) in {"SUBSCRIBED", "RealtimeSubscribeStates.SUBSCRIBED"}:
            self.joined.set()
        else:
            self.joined.clear()
            self.last_probe = 0
            self.state = "reconnecting"
        self.wake.set()

    async def exchange(self, acknowledgements=(), probe=""):
        return (
            await self.client.rpc(
                "relay_exchange",
                {
                    "store": self.config["store_id"],
                    "installation": self.installation,
                    "acknowledgements": list(acknowledgements),
                    "probe": probe,
                },
            ).execute()
        ).data

    async def run(self):
        while True:
            self.state = "reconnecting"
            self.caught_up = False
            try:
                if self.client is None:
                    self.client = await self.make_client(self.config)
                if not await self.client.auth.get_session():
                    self.state = "sign_in_required"
                    return
                first = await self.exchange()
                self.joined.clear()
                channel = self.client.channel(first["topic"], {"config": {"private": True}})
                await channel.on_broadcast("wake", self.broadcast).subscribe(self.subscription)
                await asyncio.wait_for(self.joined.wait(), 15)
                acknowledgements = []
                while self.joined.is_set():
                    self.wake.clear()
                    self.probe = str(uuid4())
                    batch = await self.exchange(acknowledgements, self.probe)
                    if batch["topic"] != first["topic"]:
                        raise RuntimeError("Subscription changed")
                    acknowledgements = await deliver(self.service, self.config["tenant_id"], batch["events"])
                    self.numbers = set(batch.get("numbers", []))
                    if not acknowledgements and not batch.get("gap_since"):
                        for _ in range(20):
                            if await asyncio.to_thread(self.local_caught_up):
                                break
                            await asyncio.sleep(0.1)
                    self.caught_up = (
                        not acknowledgements
                        and not batch.get("gap_since")
                        and await asyncio.to_thread(self.local_caught_up)
                    )
                    if batch.get("gap_since"):
                        self.state = "delivery_gap"
                    if acknowledgements:
                        continue
                    try:
                        await asyncio.wait_for(self.wake.wait(), 20)
                    except TimeoutError:
                        pass
                raise RuntimeError("Subscription disconnected")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_probe = 0
                self.state = "reconnecting"
                if self.client:
                    await self.client.remove_all_channels()
                await asyncio.sleep(5)


def require_idle(service, p):
    with service.db.transaction(False) as session:
        calls = MODELS["call_attempts"]
        if session.scalar(
            select(calls.id)
            .where(calls.tenant_id == p["tenant_id"], calls.op_status.not_in(TERMINAL))
            .limit(1)
        ):
            fail(409, "Finish or reconcile the current call before changing setup")
        tasks = MODELS["followup_tasks"]
        if session.scalar(
            select(tasks.id)
            .where(
                tasks.tenant_id == p["tenant_id"],
                tasks.data["inbound_state"]
                .as_string()
                .in_(["answering", "ringing", "connected", "notifying", "ending"]),
            )
            .limit(1)
        ):
            fail(409, "Finish the incoming call before changing setup")
        commands = MODELS["command_outbox"]
        if session.scalar(
            select(commands.id)
            .where(
                commands.tenant_id == p["tenant_id"],
                commands.op_status.in_(["pending", "sending", "reconciling", "review_required"]),
            )
            .limit(1)
        ):
            fail(409, "Resolve pending calling operations before changing setup")


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
