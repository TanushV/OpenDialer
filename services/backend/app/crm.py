"""HubSpot sync, the CRM number waterfall, the do-not-call check and call logging, wired into the store (Phase 9B).

The rules live in `leads.hubspot_sync`, `leads.waterfall`, `leads.dnc` and `aftercall.crm_log`; this module reads
HubSpot, Treg and RealValidito and writes leads, suppressions and jobs. Calling never waits on any of it: a failed sync,
check or write retries as a job and, when it cannot settle, reaches Needs attention (section 9B).
"""

import asyncio
import logging
from datetime import timedelta
from weakref import WeakValueDictionary

from app import ops
from app.leads import dnc, hubspot_sync, intake
from app.leads import waterfall as wf
from app.platform.db import audit, doc, emit, put, update
from app.service import fail

log = logging.getLogger(__name__)
LANE = "crm"
STALE = timedelta(hours=24)  # Section 5: a list not synced for a day stops dialing its leads.
SYNC_EVERY = timedelta(minutes=30)  # While calling, the chosen list syncs this often.


def config(service, p):
    return service.configs.read(p["tenant_id"])


def hubspot(service, p):
    from app.providers.hubspot import HubSpot

    key = config(service, p).get("hubspot_service_key")
    if not key:
        fail(409, {"code": "HUBSPOT_NOT_SET", "message": "Add the HubSpot Service Key in Settings first"})
    return HubSpot(key, http=getattr(service, "http", None))


async def reach(call):
    """A HubSpot outage is a named 502 with its fix; calling continues on the last synced leads."""
    from app.providers.hubspot import HubSpotError

    try:
        return await call
    except HubSpotError as exc:
        log.warning("HubSpot request failed: %s", exc)
        fail(
            502,
            {
                "code": "HUBSPOT_UNAVAILABLE",
                "message": f"HubSpot did not answer ({exc}). Calling continues on the leads already synced.",
                "fix": "Try the sync again in a minute; check the Service Key in Settings if it keeps failing",
            },
        )


def assigned_segment(service, p):
    managed = service.managed
    if not managed.pool_checked:
        managed.require_ready(p["tenant_id"])
    return managed.pool.segment_id if managed.pool else None


def require_segment(service, p, list_id):
    if (segment := assigned_segment(service, p)) is not None and segment != list_id:
        fail(403, "This machine can sync only its assigned HubSpot segment.")


async def lists(service, p):
    assigned_segment(service, p)  # Wait for the first exchange before loading this profile's lists.
    available = await reach(hubspot(service, p).lists())
    segment = assigned_segment(service, p)
    return {"lists": [item for item in available if segment is None or item["id"] == segment]}


def set_spend_limit(service, p, list_name, usd):
    """The per-list waterfall limit, set before the first sync; nothing is bought without one (R15)."""
    with service.db.transaction() as s:
        campaign = _campaign(service, s, p)
        lists = campaign.data.get("lists", {})
        state = dict(lists.get(list_name) or {})
        crm = {**(state.get("crm") or {}), "spend_limit_micro": round(usd * 1_000_000)}
        update(campaign, lists={**lists, list_name: {**state, "crm": crm}})
        spent = crm.get("spent_micro", 0)
    return {
        "list_name": list_name,
        "spend_limit_usd": usd,
        "spent_usd": spent / 1_000_000,
        "estimate_usd_per_contact": 0.06,
    }


def _campaign(service, s, p):
    return next(iter(ops.scoped(service, s, "campaigns", p)))


def crm_leads(service, s, p, list_name):
    return {
        r.data["external_id"]: r
        for r in ops.scoped(service, s, "leads", p)
        if r.data.get("source") == "crm" and list_name in (r.data.get("list_names") or [])
    }


def _endpoint(service, s, p, phone, now):
    endpoint = ops._by_key(service, s, p, "phone_endpoints", phone)
    if endpoint:
        return endpoint
    return put(
        s,
        "phone_endpoints",
        p["tenant_id"],
        p["seller_id"],
        {
            "phone": phone,
            "extension": None,
            "format_status": "valid",
            "assignment_evidence": "unknown",
            "ownership_status": "unknown",
            "reachability_status": "unknown",
        },
        key=phone,
        now=now,
    )


def _lead_fields(row):
    fields = intake.read(row, intake.guess_map(list(row)))
    return {
        "row": fields["row"],
        "profile": fields["profile"],
        "signals": fields["signals"],
        "number_source": fields["number_source"] or "crm",
        "live_status": fields["live_status"],
        "name": str(row.get("name") or "Unknown")[:200],
        "company": str(row.get("company") or "")[:200],
        "domain": str(row.get("domain") or "")[:250].lower(),
        "crm_phone": row.get("phone"),
        "crm_row": row,
    }


def _previous_crm_row(lead):
    row = lead.get("crm_row") or {}
    # Older syncs could clear the source row while leaving the last checked CRM number in use.
    if not hubspot_sync.dialable_phone(row.get("phone")) and (
        previous := hubspot_sync.dialable_phone(lead.get("crm_phone"))
    ):
        return {**row, "phone": previous}
    return row


def _set_phone(service, s, p, lead, phone, now):
    """Point a lead at a number; with none, it waits as Needs a number and is not dialable."""
    from app.optimization.geography import AREA_STATES

    changes = {}
    if not lead or lead.get("phone") != phone:
        changes["dnc"] = {"status": "not_checked", "reason": "not yet run"}
    if not phone:
        if not lead or lead.get("status") in (None, "eligible", "needs_number"):
            changes["status"] = "needs_number"
        return {**changes, "phone": "", "endpoint_id": None, "number_status": "needs_number"}
    endpoint = _endpoint(service, s, p, phone, now)
    changes.update(phone=phone, endpoint_id=endpoint.id, region=phone[2:5])
    if not lead or not lead.get("state") or lead.get("state_source") == "phone_area_code":
        changes["state"] = AREA_STATES.get(phone[2:5])
        changes["state_source"] = "phone_area_code" if changes["state"] else "unknown"
    if (
        not lead
        or lead.get("status") in (None, "needs_number")
        or (lead.get("status") == "needs_correction" and lead.get("phone") != phone)
    ):
        changes["status"] = "eligible"
    return changes


def apply_sync(service, p, list_id, list_name, members, contacts):
    """Write one sync's plan: new and changed leads, removals, opt-outs and the last-contacted guard."""
    from app.preparation.workflows import company_key

    now = service.clock()
    with service.db.transaction() as s:
        require_segment(service, p, list_id)
        campaign = _campaign(service, s, p)
        existing = crm_leads(service, s, p, list_name)
        previous = {cid: _previous_crm_row(lead.data) for cid, lead in existing.items()}
        last_called = {
            cid: service_time(lead.data.get("last_called_at"))
            for cid, lead in existing.items()
            if lead.data.get("last_called_at")
        }
        planned = hubspot_sync.plan(
            contacts,
            members,
            previous,
            now=now,
            last_called=last_called,
        )
        lists = campaign.data.get("lists", {})
        crm = {
            "list_id": list_id,
            "synced_at": now.isoformat(),
            **(lists.get(list_name, {}).get("crm") or {}),
        }
        crm.update(list_id=list_id, synced_at=now.isoformat())
        update(campaign, lists={**lists, list_name: {**lists.get(list_name, {}), "crm": crm}})
        checks = []
        for cid, row in planned["add"]:
            phone = hubspot_sync.dialable_phone(row.get("phone"))
            data = {
                **_lead_fields(row),
                "company_key": company_key(row),
                "campaign_id": campaign.id,
                "timezone": None,
                "timezone_confidence": "unknown",
                "list_name": list_name,
                "list_names": [list_name],
                "segment": "general",
                "originally_eligible": True,
                "coverage_due_at": (now + timedelta(days=1)).isoformat(),
                "next_attempt_at": planned["recent"].get(cid, now.isoformat()),
                "attempt_count": 0,
                "requested_contact": False,
                "imported_at": now.isoformat(),
                "source": "crm",
                "external_id": cid,
                "crm_list_id": list_id,
                "crm_number_revision": 1,
                "number_status": "checking",
                "crm_synced_at": now.isoformat(),
            }
            data.update(_set_phone(service, s, p, None, phone, now))
            if data["number_status"] == "needs_number":
                data["number_status"] = "checking"  # The waterfall looks for one next.
            lead = put(
                s, "leads", p["tenant_id"], p["seller_id"], data, key=f"crm:{campaign.id}:{cid}", now=now
            )
            checks.append(lead)
        for cid, row in planned["update"]:
            lead = existing[cid]
            changes = _lead_fields(row)
            phone = hubspot_sync.dialable_phone(row.get("phone"))
            if phone != hubspot_sync.dialable_phone(previous[cid].get("phone")):
                changes.update(
                    _set_phone(service, s, p, lead.data, phone, now),
                    number_status="checking",
                    number_note=None,
                    crm_number_revision=lead.version + 1,
                )
                checks.append(lead)  # A changed CRM number is checked again (R9B.14).
            update(lead, **changes)
        for lead in existing.values():
            update(lead, crm_synced_at=now.isoformat())
        for cid, until in planned["recent"].items():
            lead = existing.get(cid)
            if lead and until > (lead.data.get("next_attempt_at") or ""):
                update(lead, next_attempt_at=until, recently_contacted_until=until)
        for cid in planned["drop"]:
            lead = existing[cid]
            update(
                lead,
                list_names=[n for n in lead.data.get("list_names", []) if n != list_name],
                crm_removed=True,
            )
            if not lead.data.get("list_names"):
                update(
                    lead,
                    status="removed"
                    if lead.data["status"] in {"eligible", "needs_number"}
                    else lead.data["status"],
                )
        for entry in planned["opt_outs"]:
            phone = hubspot_sync.dialable_phone(entry["phone"])
            if phone and not ops.suppressed(service, s, p, phone):
                ops.suppress(
                    service,
                    p,
                    phone,
                    "opt_out",
                    event_id=f"hubspot-opt-out:{entry['contact_id']}:{phone}",
                    session=s,
                )
        for lead in checks:
            revision = lead.data["crm_number_revision"]
            service.jobs.enqueue_in(
                s,
                LANE,
                {"kind": "waterfall", "lead_id": lead.id, "crm_number_revision": revision, **_who(p)},
                f"waterfall:{lead.id}:sync:{revision}",
                tenant_id=p["tenant_id"],
            )
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "hubspot.synced",
            campaign.id,
            now,
            {k: len(v) if isinstance(v, list | dict) else v for k, v in planned.items()},
        )
        emit(s, p["tenant_id"], "campaign.updated", campaign, now)
    return {
        "added": len(planned["add"]),
        "updated": len(planned["update"]),
        "removed": len(planned["drop"]),
        "opted_out": len(planned["opt_outs"]),
        "recently_contacted": len(planned["recent"]),
    }


def service_time(value):
    from datetime import datetime

    return datetime.fromisoformat(value) if value else None


def _who(p):
    return {k: p.get(k) for k in ("seller_id", "user_id")}


def schedule_syncs(service, p, *, starting=False):
    """Sync every CRM list at Start calling, and every 30 minutes while the rep is calling (R9C.6)."""
    if not config(service, p).get("hubspot_service_key"):
        return 0
    now = service.clock()
    with service.db.transaction() as s:
        user = ops.required(service, s, "users", p["user_id"], p)
        if not starting and (user.data.get("presence") or {}).get("status") != "ready":
            return 0
        queued = 0
        for name, _, synced, _ in lists_due(service, s, p, now):
            if starting or synced is None or now - synced >= SYNC_EVERY:
                stamp = now.replace(second=0, microsecond=0).isoformat()
                service.jobs.enqueue_in(
                    s,
                    LANE,
                    {"kind": "hubspot_sync", "list_name": name, **_who(p)},
                    f"hubspot-sync:{name}:{stamp}",
                    tenant_id=p["tenant_id"],
                    priority=5,
                )
                queued += 1
    return queued


async def sync(service, p, list_id, list_name):
    """Read the list's members and their contacts, then write the plan. Calling never waits on it."""
    require_segment(service, p, list_id)
    hs = hubspot(service, p)
    members = await reach(hs.members(list_id))
    contacts = await reach(hs.contacts(members, hubspot_sync.PROPERTIES)) if members else []
    return await asyncio.to_thread(apply_sync, service, p, list_id, list_name, members, contacts)


def lists_due(service, s, p, now):
    """CRM lists whose last sync is older than the calling rule allows: (name, synced_at, stale)."""
    campaign = _campaign(service, s, p)
    out = []
    for name, state in (campaign.data.get("lists") or {}).items():
        crm = (state or {}).get("crm")
        if not crm or state.get("archived"):
            continue
        if (pool := service.managed.pool) is not None and pool.segment_id != crm["list_id"]:
            continue
        synced = service_time(crm.get("synced_at"))
        out.append((name, crm["list_id"], synced, synced is None or now - synced >= STALE))
    return out


# --- Jobs -------------------------------------------------------------------------------------------------------------


async def run_job(service, p, payload):
    kind = payload["kind"]
    if kind == "waterfall":
        lead_id = payload["lead_id"]
        locks = service.__dict__.setdefault("_waterfall_locks", WeakValueDictionary())
        async with locks.setdefault(lead_id, asyncio.Lock()):
            return await waterfall_job(service, p, lead_id, revision=payload.get("crm_number_revision"))
    if kind == "dnc":
        return await dnc_job(service, p)
    if kind == "crm_log":
        return await log_job(service, p, payload["call_id"])
    if kind == "hubspot_sync":
        with service.db.transaction(False) as s:
            crm = ((_campaign(service, s, p).data.get("lists") or {}).get(payload["list_name"]) or {}).get(
                "crm"
            ) or {}
        return (
            await sync(service, p, crm["list_id"], payload["list_name"])
            if crm.get("list_id")
            else {"skipped": True}
        )
    if kind == "crm_contact":
        return await contact_job(service, p, payload["lead_id"])
    if kind == "crm_summary":
        return await summary_job(service, p, payload["call_id"])
    raise ValueError(f"No crm handler for {kind!r}")


def _contact(lead):
    row = lead.get("crm_row") or {}
    return {
        "name": lead.get("name"),
        "company": lead.get("company"),
        "domain": lead.get("domain"),
        "linkedin_url": row.get("linkedin url"),
        "email": row.get("email"),
        "existing_phone": hubspot_sync.dialable_phone(row.get("phone")),
    }


def _list_room(service, s, p, lead):
    """Micro-USD this list may still spend on the waterfall; None when no limit is set (nothing is bought)."""
    campaign = _campaign(service, s, p)
    crm = (campaign.data.get("lists", {}).get(lead.data["list_name"]) or {}).get("crm") or {}
    limit = crm.get("spend_limit_micro")
    return None if limit is None else max(0, limit - crm.get("spent_micro", 0))


async def waterfall_job(service, p, lead_id, *, revision=None):
    """Run the agreed waterfall on one CRM lead; then its do-not-call check. A CSV lead is never checked."""
    from app.providers.treg import Treg, TregError

    with service.db.transaction(False) as s:
        lead = ops.required(service, s, "leads", lead_id, p)
        if lead.data.get("source") != "crm":
            return {"skipped": "csv"}
        current_revision = lead.data.get("crm_number_revision")
        if revision is not None and revision != current_revision:
            return {"skipped": "superseded"}
        room = _list_room(service, s, p, lead)
        ledger = dict(lead.data.get("waterfall") or {})
        contact = _contact(lead.data)
    existing_phone = contact["existing_phone"]
    if existing_phone in ledger.get("wrong_numbers", []):
        existing_phone = None
    token = config(service, p).get("treg_token")
    if not token or room is None:
        # No token or no spend limit: a CRM number dials as Unconfirmed; with none, the lead waits (section 9A).
        await asyncio.to_thread(
            _store_number,
            service,
            p,
            lead_id,
            {"number_status": "unconfirmed" if existing_phone else "needs_number"},
            None,
            revision=current_revision,
        )
        return await dnc_job(service, p)
    saves = []

    def save():
        saves.append(dict(ledger))
        _store_ledger(service, p, lead_id, ledger)

    try:
        engine = wf.Waterfall(
            ledger,
            Treg(token, http=getattr(service, "http", None)),
            now=service.clock(),
            room_micro=room,
            save=save,
        )
        result = await engine.run(contact)
    except wf.SpendLimit:
        await asyncio.to_thread(
            _store_number,
            service,
            p,
            lead_id,
            {"number_status": "checking", "number_note": "The list's spend limit is reached"},
            None,
            revision=current_revision,
        )
        return {"status": "spend_limit"}
    except (wf.Held, TregError) as exc:
        log.warning("Waterfall held for a lead: %s", type(exc).__name__)
        await asyncio.to_thread(
            _store_number,
            service,
            p,
            lead_id,
            {
                "number_status": "unconfirmed" if existing_phone else "needs_number",
                "number_note": str(exc)[:200],
            },
            None,
            revision=current_revision,
        )
        return {"status": "held"}
    status = {"provider_error": "needs_number"}.get(result["status"], result["status"])
    changes = {"number_status": status, "number_source": result["finder"] or lead.data.get("number_source")}
    await asyncio.to_thread(_store_number, service, p, lead_id, changes, result, revision=current_revision)
    await dnc_job(service, p)
    return {"status": status}


def _store_ledger(service, p, lead_id, ledger):
    with service.db.transaction() as s:
        lead = ops.required(service, s, "leads", lead_id, p)
        update(lead, waterfall=ledger)


def _store_number(service, p, lead_id, changes, result, *, revision):
    now = service.clock()
    with service.db.transaction() as s:
        lead = ops.required(service, s, "leads", lead_id, p)
        if result:
            campaign = _campaign(service, s, p)
            lists = campaign.data.get("lists", {})
            crm = dict((lists.get(lead.data["list_name"]) or {}).get("crm") or {})
            crm["spent_micro"] = crm.get("spent_micro", 0) + max(
                0, result["spent_micro"] - lead.data.get("waterfall_spent", 0)
            )
            update(
                campaign,
                lists={**lists, lead.data["list_name"]: {**lists[lead.data["list_name"]], "crm": crm}},
            )
            update(lead, waterfall_spent=result["spent_micro"])
        if lead.data.get("crm_number_revision") != revision:
            return  # A sync changed the stored number while the check was in flight.
        stored = (lead.data.get("crm_row") or {}).get("phone")
        if result:
            if result.get("phone") and result["phone"] != lead.data.get("phone"):
                changes.update(_set_phone(service, s, p, lead.data, result["phone"], now))
            changes["waterfall_result"] = {k: result[k] for k in ("status", "phone", "finder", "spent_micro")}
        update(lead, **changes)
        status = changes.get("number_status")
        props = {"phone_status": status} if status in {"confirmed", "unconfirmed", "conflict"} else {}
        if result and result.get("phone") and not stored:
            props["phone"] = result[
                "phone"
            ]  # Written only into an empty field; a CRM number is never overwritten.
        if props and lead.data.get("external_id"):
            write_contact(service, s, p, lead, props, now)
        emit(s, p["tenant_id"], "lead.updated", lead, now)


async def dnc_job(service, p):
    """Check every CRM lead that is due, in batches of 1,000; a failure leaves them "DNC not checked"."""
    from app.providers.realvalidito import DNCUnavailable, RealValidito

    now = service.clock()
    with service.db.transaction(False) as s:
        due = [doc(r) for r in ops.scoped(service, s, "leads", p) if dnc.due(r.data, now)]
    if not due:
        return {"checked": 0}
    conf = config(service, p)
    try:
        if not (conf.get("realvalidito_api_key") and conf.get("realvalidito_api_secret")):
            raise DNCUnavailable("No RealValidito key", "DNC_NOT_SET")
        answers = await RealValidito(
            conf["realvalidito_api_key"], conf["realvalidito_api_secret"], http=getattr(service, "http", None)
        ).validate([lead["phone"] for lead in due])
    except DNCUnavailable as exc:
        await asyncio.to_thread(
            _store_dnc,
            service,
            p,
            [(lead["id"], lead["phone"], dnc.failed(lead, exc.reason, now), None) for lead in due],
        )
        return {"checked": 0, "reason": exc.reason}
    results = [(lead["id"], lead["phone"], *dnc.outcome(lead, answers[lead["phone"]], now)) for lead in due]
    await asyncio.to_thread(_store_dnc, service, p, results)
    return {"checked": len(results)}


def _store_dnc(service, p, results):
    now = service.clock()
    with service.db.transaction() as s:
        for lead_id, phone, changes, suppression in results:
            lead = ops.required(service, s, "leads", lead_id, p)
            if lead.data["phone"] != phone:
                continue  # A reply for an older number cannot clear or suppress the replacement.
            update(lead, **changes)
            if suppression and not ops.suppressed(service, s, p, suppression["phone"]):
                ops.suppress(
                    service,
                    p,
                    suppression["phone"],
                    "opt_out",
                    event_id=f"dnc:{lead.id}:{suppression['phone']}",
                    session=s,
                )
                update(lead, dnc={**changes["dnc"], "suppressed": dnc.describe(suppression["lists"])})
                write_contact(
                    service,
                    s,
                    p,
                    lead,
                    {
                        "opendialer_opt_out": "true",
                        "opendialer_opt_out_reason": dnc.describe(suppression["lists"])[:250],
                    },
                    now,
                )
            emit(s, p["tenant_id"], "lead.updated", lead, now)


async def log_job(service, p, call_id):
    """Write one finished call to HubSpot; the job's retries and Needs attention cover failures (R9B.9)."""
    from app.aftercall import crm_log

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        lead = ops.required(service, s, "leads", call.data["lead_id"], p)
        data, lead_data = dict(call.data), dict(lead.data)
        endpoint_id = data.get("endpoint_id")
        endpoint = ops.scoped_get(service, s, p, "phone_endpoints", endpoint_id) if endpoint_id else None
        dialed_phone = endpoint.data["phone"] if endpoint else lead_data.get("phone") or ""
    # R11.7: a test call to a HubSpot contact is the owner's own test contact (a test session dials only the
    # rep's phone), so it is written; that is how L15 to L18 run on the owner's line.
    if lead_data.get("source") != "crm" or not lead_data.get("external_id"):
        return {"skipped": True}
    hs = hubspot(service, p)
    dispositions = await hs.dispositions()
    outcome = (data.get("disposition") or {}).get("outcome") or data.get("outcome") or "no_answer"
    mapped, missing = crm_log.map_outcomes([outcome], dispositions)
    if missing:
        raise RuntimeError(f"No HubSpot outcome for {outcome}; map it in Settings")
    record = crm_log.CallRecord(
        call_id=call_id,
        contact_id=lead_data["external_id"],
        outcome=outcome,
        started_at=data.get("answered_at") or data.get("created_at") or service.clock().isoformat(),
        duration_ms=int(1000 * float(data.get("talk_seconds") or 0)),
        from_number=data.get("from_number") or "",
        to_number=dialed_phone,
        note=((data.get("disposition") or {}).get("note") or "")[:2000],
    )
    uncertain = bool(data.get("crm_log_attempted"))
    await asyncio.to_thread(_mark_attempt, service, p, call_id)
    hubspot_id = await crm_log.log_call(hs, record, mapped, uncertain=uncertain)
    await asyncio.to_thread(_mark_logged, service, p, call_id, hubspot_id)
    with service.db.transaction(False) as s:
        summary = (ops.call_summary(service, s, p, call_id) or {}).get("summary")
    if summary:
        await crm_log.add_summary(hs, hubspot_id, summary)
    if outcome in {"wrong_party", "invalid_endpoint"} and dialed_phone == lead_data.get("phone"):
        await hs.update_contact(lead_data["external_id"], {"phone_status": "wrong_number"})
    if outcome == "opt_out":
        await hs.update_contact(
            lead_data["external_id"], {"opendialer_opt_out": "true", "opendialer_opt_out_reason": "opt_out"}
        )
    return {"hubspot_call_id": hubspot_id}


async def summary_job(service, p, call_id):
    """R10.2: a summary that finished after the call was logged is added to the HubSpot call record."""
    from app.aftercall import crm_log

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        hubspot_id = call.data.get("crm_logged")
        summary = (ops.call_summary(service, s, p, call_id) or {}).get("summary")
    if not hubspot_id or not summary:
        return {"skipped": True}
    await crm_log.add_summary(hubspot(service, p), hubspot_id, summary)
    return {"hubspot_call_id": hubspot_id}


def enqueue_summary(service, s, p, call):
    if call.data.get("crm_logged"):
        service.jobs.enqueue_in(
            s,
            LANE,
            {"kind": "crm_summary", "call_id": call.id, **_who(p)},
            f"crm-summary:{call.id}:{call.data.get('transcript_version', 0)}",
            tenant_id=p["tenant_id"],
            max_attempts=6,
        )


def _mark_attempt(service, p, call_id):
    with service.db.transaction() as s:
        update(ops.required(service, s, "call_attempts", call_id, p), crm_log_attempted=True)


def _mark_logged(service, p, call_id, hubspot_id):
    with service.db.transaction() as s:
        update(ops.required(service, s, "call_attempts", call_id, p), crm_logged=hubspot_id)


def write_contact(service, s, p, lead, properties, now):
    """Queue a contact write-back (phone_status, an empty phone, an opt-out). The values wait on the lead and the job
    carries only its id; it retries and never blocks dialing."""
    pending = {**(lead.data.get("crm_pending") or {}), **properties}
    update(lead, crm_pending=pending)
    service.jobs.enqueue_in(
        s,
        LANE,
        {"kind": "crm_contact", "lead_id": lead.id, **_who(p)},
        f"crm-contact:{lead.id}:{now.isoformat()}",
        tenant_id=p["tenant_id"],
        max_attempts=6,
    )


async def contact_job(service, p, lead_id):
    with service.db.transaction(False) as s:
        lead = ops.required(service, s, "leads", lead_id, p).data
        pending, contact_id = dict(lead.get("crm_pending") or {}), lead.get("external_id")
    if not pending or not contact_id:
        return {"written": []}
    hs = hubspot(service, p)
    properties = dict(pending)
    if "phone" in properties:
        contacts = await hs.contacts([contact_id], hubspot_sync.PHONE_PROPERTIES)
        saved = (contacts[0].get("properties") or {}) if contacts else {}
        if not contacts or any(
            str(saved.get(field) or "").strip() for field in hubspot_sync.PHONE_PROPERTIES
        ):
            properties.pop("phone")
    if properties:
        await hs.update_contact(contact_id, properties)
    with service.db.transaction() as s:
        row = ops.required(service, s, "leads", lead_id, p)
        left = {k: v for k, v in (row.data.get("crm_pending") or {}).items() if pending.get(k) != v}
        update(row, crm_pending=left)
    return {"written": sorted(properties)}


def on_outcome(service, s, p, call, lead, outcome):
    """After a saved outcome on a CRM lead: log it to HubSpot, note our own call for the guard, and reopen a wrong number."""
    if lead.data.get("source") != "crm":
        return
    now = service.clock()
    update(lead, last_called_at=now.isoformat())
    enqueue_log(service, s, p, call)
    if (
        outcome == "wrong_party"
        and (call.data.get("endpoint_id") or lead.data["endpoint_id"]) == lead.data["endpoint_id"]
    ):
        ledger = dict(lead.data.get("waterfall") or {})
        wf.mark_wrong(ledger, lead.data["phone"])
        update(lead, waterfall=ledger, number_status="checking")
        service.jobs.enqueue_in(
            s,
            LANE,
            {"kind": "waterfall", "lead_id": lead.id, **_who(p)},
            f"waterfall:{lead.id}:{now.isoformat()}",
            tenant_id=p["tenant_id"],
        )


def enqueue_log(service, s, p, call):
    """Queue the HubSpot record for a saved outcome; never for a CSV lead. A test call to a HubSpot contact is
    written: it can only be the owner's own test contact (R11.7)."""
    lead = ops.required(service, s, "leads", call.data["lead_id"], p)
    if lead.data.get("source") != "crm" or not config(service, p).get("hubspot_service_key"):
        return
    service.jobs.enqueue_in(
        s,
        LANE,
        {"kind": "crm_log", "call_id": call.id, **_who(p)},
        f"crm-log:{call.id}",
        tenant_id=p["tenant_id"],
        max_attempts=6,
    )
