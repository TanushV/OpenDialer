"""Shared caller IDs owned by the relay and mirrored for the local dialer."""

import logging
from typing import Literal

from app import ops
from app.platform.db import SCOPE_KEY, doc, emit, put, rows, update
from pydantic import BaseModel, Field

LOG = logging.getLogger(__name__)


class PoolNumber(BaseModel):
    phone: str = Field(pattern=r"^\+1[0-9]{10}$")
    daily_limit: int = Field(ge=1, le=90)
    used: int = Field(ge=0)
    outbound_enabled: bool


class PoolSnapshot(BaseModel):
    id: str
    segment_id: str = Field(pattern=r"^[0-9]{1,40}$")
    shared_connection_id: str | None = None
    numbers: tuple[PoolNumber, ...]

    def number(self, phone):
        return next((number for number in self.numbers if number.phone == phone), None)


class PoolAdmission(BaseModel):
    phone: str | None = Field(default=None, pattern=r"^\+1[0-9]{10}$")
    reason: Literal["unavailable", "busy", "capacity", "expired"] | None = None
    submitted: bool = False


def mirror_inventory(service, scope, pool):
    """Only local routing references are added here. Carrier number configuration belongs to the pool operator."""
    with service.db.transaction() as s:
        seller = next(iter(rows(s, "sellers", scope)), None)
        if seller is None:
            return
        p = {SCOPE_KEY: scope, "seller_id": seller.id}
        service.db.authorize_stream(s, p)
        for entry in pool.numbers:
            number = ops._by_key(service, s, p, "number_inventory", entry.phone)
            if number is not None:
                if doc(number).get("shared_pool") != pool.id:
                    update(number, shared_pool=pool.id)
                else:
                    continue
            else:
                number = put(
                    s,
                    "number_inventory",
                    scope,
                    seller.id,
                    {
                        "phone": entry.phone,
                        "region": entry.phone[2:5],
                        "shared_pool": pool.id,
                        "lifecycle": "limited_real_use",
                        "daily_limit": entry.daily_limit,
                        "capacity": 1,
                        "callback_retained": True,
                    },
                    key=entry.phone,
                    now=service.clock(),
                )
            emit(s, scope, "number.updated", number, service.clock())


async def request(managed, method, params):
    try:
        if managed.client is None:
            return PoolAdmission(reason="unavailable")
        response = await managed.client.rpc(method, {**params, **lease_params(managed)}).execute()
        return PoolAdmission.model_validate(response.data)
    except Exception as error:
        LOG.warning("Shared number pool %s failed: %s", method, type(error).__name__)
        return PoolAdmission(reason="unavailable")


async def cancel(managed, ref, *, unsent=False):
    if managed.client is None:
        return
    try:
        await managed.client.rpc(
            "relay_pool_cancel", {"request_ref": ref, "was_unsent": unsent, **lease_params(managed)}
        ).execute()
    except Exception as error:
        LOG.warning("Shared number hold could not be released: %s", type(error).__name__)


def lease_params(managed):
    if managed.pool and managed.pool.shared_connection_id:
        return {"store": managed.config["store_id"], "installation": managed.installation}
    return {}
