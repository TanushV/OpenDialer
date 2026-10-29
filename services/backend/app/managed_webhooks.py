"""Outgoing control delivery. Auth refresh belongs to the supported Supabase SDK."""

import asyncio
import json
import os
import re
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.service import dt, fail


class ConnectionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    label: str = Field(min_length=1, max_length=100)
    url: str
    publishable_key: str = Field(min_length=10, max_length=512)
    workspace_id: UUID
    email: str = Field(min_length=3, max_length=254)
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


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix("." + str(uuid4()) + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(value, stream)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionStorage:
    """SDK storage interface; secrets never enter normal settings or API responses."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

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
        self.config = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.installation = str(uuid4())
        self.task = None
        self.client = None
        self.last_probe = 0.0
        self.caught_up = False
        self.state = "disconnected"
        self.wake = asyncio.Event()
        self.joined = asyncio.Event()
        self.probe = ""

    def status(self):
        return {
            "configured": bool(self.config),
            "label": self.config.get("label", ""),
            "connected": bool(self.caught_up and self.last_probe and monotonic() - self.last_probe < 35),
            "state": self.state,
            "url": self.config.get("url", ""),
        }

    def require_ready(self, tenant_id):
        if self.config.get("tenant_id") == tenant_id and not self.status()["connected"]:
            fail(409, "Call events reconnecting; new calls paused. Hangup remains available.")

    async def make_client(self, config, storage=None):
        from supabase import AsyncClientOptions, acreate_client

        return await acreate_client(
            config["url"],
            config["publishable_key"],
            AsyncClientOptions(storage=storage or self.storage, postgrest_client_timeout=10),
        )

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
        values = profile.model_dump(mode="json", exclude={"password"})
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
                .select("id")
                .eq("id", str(profile.workspace_id))
                .execute()
            )
            if not row.data:
                fail(403, "Workspace disabled or not provisioned")
        except Exception:
            await candidate.auth.close()
            await candidate.postgrest.aclose()
            staged.path.unlink(missing_ok=True)
            await self.start()
            fail(400, "Connection failed; check the profile and network")
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
                    self.caught_up = not acknowledgements
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
