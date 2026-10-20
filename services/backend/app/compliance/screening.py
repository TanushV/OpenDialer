"""One leased prepaid lookup per phone; uncertain effects require an explicit new charge choice."""

import asyncio
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, or_, select

from app.compliance.operating import (
    COSTS,
    DncRefreshPolicy,
    ScreeningRequest,
    authority_stamp,
    authorized_screen,
    cached_screen,
    screening_capability,
    screening_expiry,
    screening_key,
    screening_max_age,
    screening_snapshot,
    usable_screen,
)
from app.compliance.phone import normalize_phone
from app.db import MODELS, Job, audit, doc, put, uid, update
from app.jobs.queue import aware, jobdoc
from app.providers.dnc import DNCError, PrepaidDNC
from app.providers.inference import InferenceResult
from app.service import dt, fail

UNFINISHED = {"sending", "uncertain", "review_required", "unsubmitted"}
QUERY_ESTIMATE = COSTS["dnc"]["estimated_per_query_usd"]


class ScreeningRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    accept_possible_duplicate_charge: Literal[True]


def active_admin(service, s, p):
    user = service.required(s, "users", p["user_id"], p)
    return not user.data.get("disabled") and user.data.get("role") in {"owner", "admin"}


def lease_active(service, data):
    until = dt(data.get("lease_until"))
    # Legacy sending rows have no lease; a recent submission still cannot overlap.
    if until is None and data.get("status") == "sending" and data.get("started_at"):
        until = dt(data["started_at"]) + timedelta(seconds=30)
    return bool(until and until > service.clock())


def view(service, row, config, max_age):
    data = row.data
    valid = usable_screen(service, config, data, max_age)
    status = data["status"]
    if status == "completed" and not valid:
        status = "expired"
    if status == "sending" and not lease_active(service, data):
        status = "uncertain"
    return {
        "id": row.id,
        "version": row.version,
        "phone": data["phone"],
        "status": status,
        "listed": valid["listed"] if valid else None,
        "checked_at": data.get("checked_at"),
        "expires_at": data.get("expires_at"),
        "can_retry": status in UNFINISHED | {"expired"} and not lease_active(service, data),
        "possible_charge_usd": data.get("estimate_usd", QUERY_ESTIMATE)
        if data.get("request_key") and status != "unsubmitted"
        else 0,
        "previous_attempts": data.get("previous_attempts", 0),
        "reason": data.get("recovery_reason")
        or (
            "Lookup result is unavailable; a previous charge may remain. Another query requires an explicit choice."
            if status in {"uncertain", "review_required"}
            else "Cached result is no longer current; refresh seller authorization and explicitly check again."
            if status == "expired"
            else ""
        ),
    }


def claim(service, p, phone=None, identity=None, retry=None, *, refresh=None):
    config = service.vault.read(p["tenant_id"])
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        if not active_admin(service, s, p):
            fail(403, "An active administrator is required for prepaid screening")
        model = MODELS["registry_snapshots"]
        row = (
            service.required(s, "registry_snapshots", identity, p)
            if identity
            else s.scalar(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == screening_key(service, phone),
                )
            )
        )
        max_age = screening_max_age(service, s, p)
        if retry:
            if row.data.get("kind") != "prepaid":
                fail(404, "Prepaid screening record not found")
            phone = row.data["phone"]
            if row.data.get("retry_idempotency_key") == retry.idempotency_key:
                return None, view(service, row, config, max_age), None
            if row.version != retry.expected_version:
                fail(409, "Screening changed; reload before deciding on another paid query")
        if service.suppressed(s, p, phone):
            if retry:
                fail(403, "INTERNAL_DNC_BLOCKS_LOOKUP")
            return (
                None,
                {"phone": phone, "listed": True, "internal_suppressed": True, "provider": "local_registry"},
                None,
            )
        cached = cached_screen(service, s, p, phone)
        if cached:
            return None, view(service, row, config, max_age) if retry else cached, None
        if row and (
            lease_active(service, row.data)
            or not retry
            and row.data.get("status") in UNFINISHED - {"unsubmitted"}
        ):
            fail(409, "DNC_SCREENING_PENDING_OR_UNCERTAIN_REVIEW_REQUIRED")
        if not config.get("dnc_api_key"):
            fail(409, "DNC_PROVIDER_NOT_CONFIGURED")
        if not authorized_screen(service, config, phone, max_age) or (
            not service.settings.simulation
            and config.get("dnc_seller_authorization", {}).get("seller_id") != p["seller_id"]
        ):
            fail(403, "DNC_SELLER_AUTHORIZATION_REQUIRED")
        key = "dnc:" + uid()
        seller = service.required(s, "sellers", p["seller_id"], p)
        rate = seller.data.get("operating_policy", {}).get("account_rates", {}).get("dnc")
        estimate = float(rate["values"]["estimated_per_query_usd"]) if rate else QUERY_ESTIMATE
        if refresh:
            reserve_refresh_in(service, s, p, refresh, estimate)
        # Retain a legacy/crashed attempt's liability before admitting a new one.
        if row and row.data.get("request_key") and row.data.get("status") in UNFINISHED - {"unsubmitted"}:
            service.usage(
                p,
                "donotcalldnc",
                "dnc_screen",
                InferenceResult({}, {}, None),
                object_id=screening_key(service, phone),
                request_key=row.data["request_key"],
                reserved=row.data.get("estimate_usd", QUERY_ESTIMATE),
                session=s,
            )
        if not service.reserve_budget_in(
            s, p, key, estimate, object_id=screening_key(service, phone), work_kind="dnc_screen"
        ):
            fail(403, "DNC_QUERY_BUDGET_EXHAUSTED")
        values = {
            "kind": "prepaid",
            "estimate_usd": estimate,
            "rate_reference": rate,
            "published": False,
            "phone": phone,
            "status": "sending",
            "request_key": key,
            "started_at": service.clock().isoformat(),
            "lease_owner": uid(),
            "lease_until": (service.clock() + timedelta(seconds=30)).isoformat(),
            "authority_stamp": authority_stamp(config),
            "listed": None,
            "checked_at": None,
            "expires_at": None,
            "recovery_reason": None,
            "previous_attempts": row.data.get("previous_attempts", 0) + 1 if row else 0,
            "retry_idempotency_key": retry.idempotency_key if retry else None,
        }
        if row:
            update(row, **values)
        else:
            row = put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                values,
                key=screening_key(service, phone),
                now=service.clock(),
            )
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "dnc.query_authorized",
            row.id,
            service.clock(),
            {"request_key": key, "explicit_retry": retry is not None},
        )
        for phase in ("intent", "submitted"):
            service.journal_effect_in(s, p, "dnc_screen", key, phase, [row])
        return doc(row), None, config


def finish(service, p, intent, result):
    # Billing is recorded even when current authority/source versions reject publication.
    service.usage(
        p,
        "donotcalldnc",
        "dnc_screen",
        InferenceResult({}, {"credits_remaining": result["credits_remaining"]} if result else {}, None),
        object_id=screening_key(service, intent["phone"]),
        request_key=intent["request_key"],
        reserved=intent.get("estimate_usd", QUERY_ESTIMATE),
    )
    config = service.vault.read(p["tenant_id"])
    rejected = False
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        row = service.required(s, "registry_snapshots", intent["id"], p)
        if (
            row.version != intent["version"]
            or row.data.get("request_key") != intent["request_key"]
            or row.data.get("lease_owner") != intent["lease_owner"]
        ):
            fail(409, "A newer screening attempt owns this result; original charge retained")
        max_age = screening_max_age(service, s, p)
        rejected = not (
            active_admin(service, s, p)
            and lease_active(service, row.data)
            and authority_stamp(config) == intent["authority_stamp"]
            and authorized_screen(service, config, intent["phone"], max_age)
            and not service.suppressed(s, p, intent["phone"])
        )
        if result and not rejected:
            update(
                row,
                **result,
                status="completed",
                authorization_reference=config["dnc_seller_authorization"]["reference"],
                expires_at=screening_expiry(service, config, result, max_age).isoformat(),
                synthetic=service.settings.simulation,
                lease_owner=None,
                lease_until=None,
            )
        else:
            update(
                row,
                status="review_required" if rejected else "uncertain",
                listed=None,
                lease_owner=None,
                lease_until=None,
                recovery_reason="Authority, source or internal opt-out changed; no clearance published. Prior charge may remain."
                if rejected
                else "Lookup result was lost or invalid; a previous charge may remain. Another query requires an explicit choice.",
            )
        saved = view(service, row, config, max_age)
        service.journal_effect_in(s, p, "dnc_screen", intent["request_key"], "receipt", [row], emergency=True)
    if rejected:
        fail(409, "DNC_AUTHORITY_CHANGED_NO_CLEARANCE")
    return saved


def reserve_refresh_in(service, s, p, refresh, estimate):
    """Debit the run/day allowance atomically with the existing per-query intent."""
    job = s.scalar(select(Job).where(Job.id == refresh["job_id"]).with_for_update())
    seller = service.required(s, "sellers", p["seller_id"], p)
    policy = DncRefreshPolicy(**seller.data.get("operating_policy", {}).get("dnc_refresh", {}))
    if (
        not job
        or job.tenant_id != p["tenant_id"]
        or job.payload.get("seller_id") != p["seller_id"]
        or job.status != "running"
        or job.lease_owner != refresh["worker_id"]
        or aware(job.lease_expires_at) <= service.clock()
        or job.payload["policy_version"] != seller.data.get("operating_policy_version", 1)
        or not policy.enabled
    ):
        fail(409, "DNC_REFRESH_CHANGED_OR_DISABLED")
    amount = int((Decimal(str(estimate)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    day = service.clock().date().isoformat()
    spending = seller.data.get("dnc_refresh_spending", {})
    used = spending.get("micro_usd", 0) if spending.get("day") == day else 0
    run_used = job.payload.get("reserved_micro_usd", 0)
    if used + amount > int(policy.max_day_usd * 1_000_000) or run_used + amount > int(
        policy.max_run_usd * 1_000_000
    ):
        fail(403, "DNC_REFRESH_BUDGET_EXHAUSTED")
    job.payload = {**job.payload, "reserved_micro_usd": run_used + amount}
    update(seller, dnc_refresh_spending={"day": day, "micro_usd": used + amount})


def schedule_refresh(service, p):
    def due(s):
        """This window's refresh identity, or None when disabled or not authorized."""
        if not active_admin(service, s, p):
            return None
        seller = service.required(s, "sellers", p["seller_id"], p)
        policy = DncRefreshPolicy(**seller.data.get("operating_policy", {}).get("dnc_refresh", {}))
        if not policy.enabled:
            return None
        window = int(service.clock().timestamp()) // (policy.interval_hours * 3600)
        version = seller.data.get("operating_policy_version", 1)
        return version, f"dnc-refresh:{p['seller_id']}:{version}:{window}"

    # Read first: a disabled policy or an already scheduled window needs no writer.
    with service.db.transaction(False) as s:
        identity = due(s)
        if identity is None:
            return None
        scheduled = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == identity[1]))
        if scheduled is not None:
            return jobdoc(scheduled)
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        identity = due(s)
        if identity is None:
            return None
        version, key = identity
        horizon = service.clock() + timedelta(hours=service.optimization_config(s, p)["horizon_hours"])
        return service.jobs.enqueue_in(
            s,
            "research",
            {
                "kind": "dnc_refresh",
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
                "policy_version": version,
                "cursor": "",
                "phase": "due",
                "horizon": horizon.isoformat(),
                "desired_generation": 1,
                "inspected": 0,
                "submitted": 0,
                "reserved_micro_usd": 0,
            },
            key,
            tenant_id=p["tenant_id"],
            priority=25,
        )


def refresh_page(service, p, job_id, worker_id):
    with service.db.transaction(False) as s:
        job = s.get(Job, job_id)
        seller = service.required(s, "sellers", p["seller_id"], p)
        policy = DncRefreshPolicy(**seller.data.get("operating_policy", {}).get("dnc_refresh", {}))
        if (
            not active_admin(service, s, p)
            or not policy.enabled
            or not job
            or job.status != "running"
            or job.lease_owner != worker_id
            or aware(job.lease_expires_at) <= service.clock()
            or job.payload["policy_version"] != seller.data.get("operating_policy_version", 1)
        ):
            return None
        endpoint, lead = MODELS["phone_endpoints"], MODELS["leads"]
        query = select(endpoint).where(
            endpoint.tenant_id == p["tenant_id"],
            endpoint.seller_id == p["seller_id"],
            endpoint.id > job.payload["cursor"],
        )
        if job.payload["phase"] == "due":
            query = query.where(
                exists(
                    select(lead.id).where(
                        lead.tenant_id == p["tenant_id"],
                        lead.seller_id == p["seller_id"],
                        lead.op_endpoint_id == endpoint.id,
                        or_(lead.op_due_at.is_(None), lead.op_due_at <= dt(job.payload["horizon"])),
                    )
                )
            )
        rows = list(s.scalars(query.order_by(endpoint.id).limit(policy.batch_size)))
        return {"rows": [doc(r) for r in rows], "payload": dict(job.payload), "limit": policy.batch_size}


def refresh_progress(
    service,
    job_id,
    worker_id,
    *,
    cursor=None,
    inspected=0,
    submitted=0,
    finish=False,
    more=False,
    reason=None,
):
    with service.db.transaction() as s:
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if (
            not job
            or job.status != "running"
            or job.lease_owner != worker_id
            or aware(job.lease_expires_at) <= service.clock()
        ):
            return False
        values = {
            **job.payload,
            "inspected": job.payload.get("inspected", 0) + inspected,
            "submitted": job.payload.get("submitted", 0) + submitted,
        }
        if cursor is not None:
            values["cursor"] = cursor
        if finish:
            if not more and values["phase"] == "due" and not reason:
                values.update(phase="all", cursor="")
                more = True
            values["complete"] = not more
            values["reason"] = reason
            if more:
                values["desired_generation"] = values["claimed_generation"] + 1
        job.payload = values
        return True


async def refresh_screening(service, p, job, worker_id):
    page = await asyncio.to_thread(refresh_page, service, p, job["id"], worker_id)
    if page is None:
        await asyncio.to_thread(
            refresh_progress, service, job["id"], worker_id, finish=True, reason="DNC_REFRESH_CHANGED"
        )
        return
    refresh = {"job_id": job["id"], "worker_id": worker_id}

    async def one(endpoint):
        async with service.screening_capacity:

            def prepare():
                with service.db.transaction(False) as s:
                    decision = screening_capability(
                        screening_snapshot(service, s, p, endpoint["phone"]), endpoint["phone"]
                    )
                    if decision["decision"] in {"clear", "listed", "blocked"}:
                        return None
                return claim(service, p, endpoint["phone"], refresh=refresh)

            try:
                result = await asyncio.to_thread(prepare)
                if result is None or result[0] is None:
                    return 0, None
                intent, _, config = result
                try:
                    await execute(service, p, intent, config)
                except HTTPException:
                    pass  # Original intent/uncertain liability is the recovery owner.
                return 1, None
            except HTTPException as exc:
                return 0, str(exc.detail) if "BUDGET_EXHAUSTED" in str(
                    exc.detail
                ) or "REFRESH_CHANGED" in str(exc.detail) else None

    for offset in range(0, len(page["rows"]), 2):
        pair = page["rows"][offset : offset + 2]
        result = await asyncio.gather(*(one(row) for row in pair))
        reason = next((value[1] for value in result if value[1]), None)
        if not await asyncio.to_thread(
            refresh_progress,
            service,
            job["id"],
            worker_id,
            cursor=pair[-1]["id"],
            inspected=len(pair),
            submitted=sum(value[0] for value in result),
        ):
            return
        if reason:
            await asyncio.to_thread(
                refresh_progress, service, job["id"], worker_id, finish=True, reason=reason
            )
            return
    await asyncio.to_thread(
        refresh_progress, service, job["id"], worker_id, finish=True, more=len(page["rows"]) == page["limit"]
    )


async def execute(service, p, intent, config):
    result = None
    try:
        async with asyncio.timeout(10):
            value = await PrepaidDNC(
                config["dnc_api_key"], http=getattr(service, "background_http", None), clock=service.clock
            ).check(intent["phone"])
        at = dt(value["checked_at"])
        if (
            value.get("phone") != intent["phone"]
            or type(value.get("listed")) is not bool
            or value.get("provider") != "donotcalldnc"
            or type(value.get("credits_remaining")) is not int
            or value["credits_remaining"] < 0
            or not at
            or not -60 <= (service.clock() - at).total_seconds() <= 300
        ):
            raise DNCError("Invalid original lookup receipt")
        result = {
            key: value[key] for key in ("phone", "listed", "checked_at", "provider", "credits_remaining")
        }
        result["source_authorization_required"] = True
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(finish, service, p, intent, None))
        raise
    except Exception:
        # A transport/adapter failure is not proof that the provider did not charge.
        # Store only the generic recovery state, never the raw response/exception.
        result = None
    saved = await asyncio.to_thread(finish, service, p, intent, result)
    if result is None:
        fail(503, "DNC_SCREENING_UNAVAILABLE_NO_CLEARANCE")
    return saved


def bind_screening_api(app, service, administrator):
    @app.get("/v1/admin/dnc/screens")
    def screens(p=Depends(administrator)):
        config = service.vault.read(p["tenant_id"])
        with service.db.transaction(False) as s:
            model = MODELS["registry_snapshots"]
            base = select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.ctx_key == "prepaid",
            )
            pending = list(
                s.scalars(
                    base.where(model.ctx_status.in_(UNFINISHED))
                    .order_by(model.created_at, model.id)
                    .limit(100)
                )
            )
            recent = list(
                s.scalars(
                    base.where(model.ctx_status == "completed")
                    .order_by(model.created_at.desc(), model.id)
                    .limit(20)
                )
            )
            max_age = screening_max_age(service, s, p)
            refresh_job = s.scalar(
                select(Job)
                .where(Job.tenant_id == p["tenant_id"], Job.key.like(f"dnc-refresh:{p['seller_id']}:%"))
                .order_by(Job.available_at.desc(), Job.id.desc())
                .limit(1)
            )
            return {
                "screens": [view(service, row, config, max_age) for row in pending + recent],
                "limited": len(pending) == 100,
                "refresh": {
                    "status": refresh_job.status,
                    **{
                        key: refresh_job.payload.get(key)
                        for key in (
                            "phase",
                            "inspected",
                            "submitted",
                            "reserved_micro_usd",
                            "complete",
                            "reason",
                        )
                    },
                }
                if refresh_job
                else None,
            }

    @app.post("/v1/admin/dnc/screen")
    async def screen(data: ScreeningRequest, p=Depends(administrator)):
        phones = list(dict.fromkeys(normalize_phone(value).e164 for value in data.phones))
        if any(not phone or not phone.startswith("+1") for phone in phones):
            fail(422, "Valid US phone numbers are required")
        results = []
        for phone in phones:
            intent, cached, config = await asyncio.to_thread(claim, service, p, phone)
            if cached is not None:
                results.append({**cached, "cached": True})
            else:
                results.append({**await execute(service, p, intent, config), "cached": False})
        return {"results": results, "authorization_required": True}

    @app.post("/v1/admin/dnc/screens/{identity}/retry")
    async def retry(identity: str, data: ScreeningRetry, p=Depends(administrator)):
        intent, cached, config = await asyncio.to_thread(claim, service, p, identity=identity, retry=data)
        return cached if cached is not None else await execute(service, p, intent, config)
