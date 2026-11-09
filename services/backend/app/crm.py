"""HubSpot sync, the CRM number waterfall, the do-not-call check and call logging, wired into the store (Phase 9B).

The rules live in `leads.hubspot_sync`, `leads.waterfall`, `leads.dnc` and `aftercall.crm_log`; this module reads
HubSpot, Treg and RealValidito and writes leads, suppressions and jobs. Calling never waits on any of it: a failed sync,
check or write retries as a job and, when it cannot settle, reaches Needs attention (section 9B).
"""

import asyncio
import logging
from datetime import timedelta

from app.compliance.phone import normalize_phone
from app.leads import dnc, hubspot_sync, intake
from app.leads import waterfall as wf
from app.platform.db import audit, doc, emit, put, update
from app.service import fail

log = logging.getLogger(__name__)
LANE = "crm"
STALE = timedelta(hours=24)  # Section 5: a list not synced for a day stops dialing its leads.
SYNC_EVERY = timedelta(minutes=30)  # Section 9C: while calling, every seat syncs this often.


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


async def lists(service, p):
    return {"lists": await reach(hubspot(service, p).lists())}


async def owners(service, p):
    return {
        "owners": await reach(hubspot(service, p).owners()),
        "calls_for": config(service, p).get("hubspot_owner_id"),
    }


def set_calls_for(service, p, owner_id):
    """Chosen once when the seat is set up; changed only by setting the seat up again (D25)."""
    current = config(service, p).get("hubspot_owner_id")
    if current and current != owner_id:
        fail(
            409, {"code": "CALLS_FOR_SET", "message": "This computer already calls for another HubSpot user"}
        )
    service.configs.write(p["tenant_id"], {"hubspot_owner_id": str(owner_id)})
    return {"calls_for": str(owner_id)}


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
    return next(iter(service.scoped(s, "campaigns", p)))


def crm_leads(service, s, p, list_name):
    return {
        r.data["external_id"]: r
        for r in service.scoped(s, "leads", p)
        if r.data.get("source") == "crm" and list_name in (r.data.get("list_names") or [])
    }


def _endpoint(service, s, p, phone, now):
    endpoint = service._by_key(s, p, "phone_endpoints", phone)
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
        "crm_row": row,
    }


def _set_phone(service, s, p, lead, phone, now):
    """Point a lead at a number; with none, it waits as Needs a number and is not dialable."""
    from app.optimization.geography import AREA_STATES

    if not phone:
        return {"phone": "", "endpoint_id": None, "status": "needs_number", "number_status": "needs_number"}
    endpoint = _endpoint(service, s, p, phone, now)
    changes = {"phone": phone, "endpoint_id": endpoint.id, "region": phone[2:5]}
    if not lead or not lead.get("state"):
        changes["state"] = AREA_STATES.get(phone[2:5])
        changes["state_source"] = "phone_area_code" if changes["state"] else "unknown"
    if not lead or lead.get("status") in (None, "needs_number"):
        changes["status"] = "eligible"
    return changes


def apply_sync(service, p, list_id, list_name, members, contacts):
    """Write one sync's plan: new and changed leads, removals, opt-outs and the last-contacted guard."""
    from app.preparation.workflows import company_key

    now = service.clock()
    conf = config(service, p)
    with service.db.transaction() as s:
        campaign = _campaign(service, s, p)
        existing = crm_leads(service, s, p, list_name)
        last_called = {
            cid: service_time(lead.data.get("last_called_at"))
            for cid, lead in existing.items()
            if lead.data.get("last_called_at")
        }
        planned = hubspot_sync.plan(
            contacts,
            members,
            {cid: lead.data.get("crm_row") for cid, lead in existing.items()},
            owner_id=conf.get("hubspot_owner_id"),
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
            phone = normalize_phone(str(row.get("phone") or "")).e164
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
                "number_status": "checking",
                "dnc": {"status": "not_checked", "reason": "not yet run"},
            }
            data.update(_set_phone(service, s, p, None, phone, now))
            if data["number_status"] == "needs_number":
                data["number_status"] = "checking"  # The waterfall looks for one next.
            lead = put(
                s, "leads", p["tenant_id"], p["seller_id"], data, key=f"crm:{campaign.id}:{cid}", now=now
            )
            checks.append(lead.id)
        for cid, row in planned["update"]:
            lead = existing[cid]
            changes = _lead_fields(row)
            phone = normalize_phone(str(row.get("phone") or "")).e164
            if phone and phone != lead.data.get("crm_phone"):
                changes.update(_set_phone(service, s, p, lead.data, phone, now), number_status="checking")
                checks.append(lead.id)  # A changed CRM number is checked again (R9B.14).
            update(lead, **changes)
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
            phone = normalize_phone(str(entry["phone"] or "")).e164
            if phone and not service.suppressed(s, p, phone):
                service.suppress(
                    p, phone, "opt_out", event_id=f"hubspot-opt-out:{entry['contact_id']}", session=s
                )
        for lead_id in checks:
            service.jobs.enqueue_in(
                s,
                LANE,
                {"kind": "waterfall", "lead_id": lead_id, **_who(p)},
                f"waterfall:{lead_id}:{now.isoformat()}",
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
        "other_owner": planned["other_owner"],
    }


def service_time(value):
    from datetime import datetime

    return datetime.fromisoformat(value) if value else None


def _who(p):
    return {k: p.get(k) for k in ("seller_id", "user_id")}


async def sync(service, p, list_id, list_name):
    """Read the list's members and their contacts, then write the plan. Calling never waits on it."""
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
        synced = service_time(crm.get("synced_at"))
        out.append((name, crm["list_id"], synced, synced is None or now - synced >= STALE))
    return out


# --- Jobs -------------------------------------------------------------------------------------------------------------


async def run_job(service, p, payload):
    kind = payload["kind"]
    if kind == "waterfall":
        return await waterfall_job(service, p, payload["lead_id"])
    if kind == "dnc":
        return await dnc_job(service, p)
    if kind == "crm_log":
        return await log_job(service, p, payload["call_id"])
    if kind == "hubspot_sync":
        return await sync(service, p, payload["list_id"], payload["list_name"])
    if kind == "crm_contact":
        return await contact_job(service, p, payload["lead_id"])
    raise ValueError(f"No crm handler for {kind!r}")


def _contact(lead):
    row = lead.get("crm_row") or {}
    return {
        "name": lead.get("name"),
        "company": lead.get("company"),
        "domain": lead.get("domain"),
        "linkedin_url": row.get("linkedin url"),
        "email": row.get("email"),
        "existing_phone": row.get("phone"),
    }


def _list_room(service, s, p, lead):
    """Micro-USD this list may still spend on the waterfall; None when no limit is set (nothing is bought)."""
    campaign = _campaign(service, s, p)
    crm = (campaign.data.get("lists", {}).get(lead.data["list_name"]) or {}).get("crm") or {}
    limit = crm.get("spend_limit_micro")
    return None if limit is None else max(0, limit - crm.get("spent_micro", 0))


async def waterfall_job(service, p, lead_id):
    """Run the agreed waterfall on one CRM lead; then its do-not-call check. A CSV lead is never checked."""
    from app.providers.treg import Treg, TregError

    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", lead_id, p)
        if lead.data.get("source") != "crm":
            return {"skipped": "csv"}
        room = _list_room(service, s, p, lead)
        ledger = dict(lead.data.get("waterfall") or {})
    token = config(service, p).get("treg_token")
    if not token or room is None:
        # No token or no spend limit: a CRM number dials as Unconfirmed; with none, the lead waits (section 9A).
        has_number = bool(_contact(lead.data)["existing_phone"])
        await asyncio.to_thread(
            _store_number,
            service,
            p,
            lead_id,
            {"number_status": "unconfirmed" if has_number else "needs_number"},
            None,
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
        result = await engine.run(_contact(lead.data))
    except wf.SpendLimit:
        await asyncio.to_thread(
            _store_number,
            service,
            p,
            lead_id,
            {"number_status": "checking", "number_note": "The list's spend limit is reached"},
            None,
        )
        return {"status": "spend_limit"}
    except (wf.Held, TregError) as exc:
        log.warning("Waterfall held for a lead: %s", type(exc).__name__)
        has_number = bool(_contact(lead.data)["existing_phone"])
        await asyncio.to_thread(
            _store_number,
            service,
            p,
            lead_id,
            {"number_status": "unconfirmed" if has_number else "needs_number", "number_note": str(exc)[:200]},
            None,
        )
        return {"status": "held"}
    status = {"provider_error": "needs_number"}.get(result["status"], result["status"])
    changes = {"number_status": status, "number_source": result["finder"] or lead.data.get("number_source")}
    await asyncio.to_thread(_store_number, service, p, lead_id, changes, result)
    await dnc_job(service, p)
    return {"status": status}


def _store_ledger(service, p, lead_id, ledger):
    with service.db.transaction() as s:
        lead = service.required(s, "leads", lead_id, p)
        update(lead, waterfall=ledger)


def _store_number(service, p, lead_id, changes, result):
    now = service.clock()
    with service.db.transaction() as s:
        lead = service.required(s, "leads", lead_id, p)
        if result and result.get("phone") and result["phone"] != lead.data.get("phone"):
            changes.update(_set_phone(service, s, p, lead.data, result["phone"], now))
            changes["number_status"] = {"provider_error": "needs_number"}.get(
                result["status"], result["status"]
            )
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
            changes["waterfall_spent"] = result["spent_micro"]
            changes["waterfall_result"] = {k: result[k] for k in ("status", "phone", "finder", "spent_micro")}
        changes["crm_phone"] = (lead.data.get("crm_row") or {}).get("phone")
        update(lead, **changes)
        status = changes.get("number_status")
        props = {"phone_status": status} if status in {"confirmed", "unconfirmed", "conflict"} else {}
        if result and result.get("phone") and not changes["crm_phone"]:
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
        due = [doc(r) for r in service.scoped(s, "leads", p) if dnc.due(r.data, now)]
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
            _store_dnc, service, p, [(lead["id"], dnc.failed(lead, exc.reason, now), None) for lead in due]
        )
        return {"checked": 0, "reason": exc.reason}
    results = [(lead["id"], *dnc.outcome(lead, answers[lead["phone"]], now)) for lead in due]
    await asyncio.to_thread(_store_dnc, service, p, results)
    return {"checked": len(results)}


def _store_dnc(service, p, results):
    now = service.clock()
    with service.db.transaction() as s:
        for lead_id, changes, suppression in results:
            lead = service.required(s, "leads", lead_id, p)
            update(lead, **changes)
            if suppression and not service.suppressed(s, p, suppression["phone"]):
                service.suppress(
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
        call = service.required(s, "call_attempts", call_id, p)
        lead = service.required(s, "leads", call.data["lead_id"], p)
        data, lead_data = dict(call.data), dict(lead.data)
    if lead_data.get("source") != "crm" or not lead_data.get("external_id") or data.get("is_test"):
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
        to_number=lead_data.get("phone") or "",
        note=((data.get("disposition") or {}).get("note") or "")[:2000],
        owner_id=config(service, p).get("hubspot_owner_id"),
    )
    uncertain = bool(data.get("crm_log_attempted"))
    await asyncio.to_thread(_mark_attempt, service, p, call_id)
    hubspot_id = await crm_log.log_call(hs, record, mapped, uncertain=uncertain)
    await asyncio.to_thread(_mark_logged, service, p, call_id, hubspot_id)
    if outcome in {"wrong_party", "invalid_endpoint"}:
        await hs.update_contact(lead_data["external_id"], {"phone_status": "wrong_number"})
    if outcome == "opt_out":
        await hs.update_contact(
            lead_data["external_id"], {"opendialer_opt_out": "true", "opendialer_opt_out_reason": "opt_out"}
        )
    return {"hubspot_call_id": hubspot_id}


def _mark_attempt(service, p, call_id):
    with service.db.transaction() as s:
        update(service.required(s, "call_attempts", call_id, p), crm_log_attempted=True)


def _mark_logged(service, p, call_id, hubspot_id):
    with service.db.transaction() as s:
        update(service.required(s, "call_attempts", call_id, p), crm_logged=hubspot_id)


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
        lead = service.required(s, "leads", lead_id, p).data
        pending, contact_id = dict(lead.get("crm_pending") or {}), lead.get("external_id")
    if not pending or not contact_id:
        return {"written": []}
    await hubspot(service, p).update_contact(contact_id, pending)
    with service.db.transaction() as s:
        row = service.required(s, "leads", lead_id, p)
        left = {k: v for k, v in (row.data.get("crm_pending") or {}).items() if pending.get(k) != v}
        update(row, crm_pending=left)
    return {"written": sorted(pending)}


def on_outcome(service, s, p, call, lead, outcome):
    """After a saved outcome on a CRM lead: log it to HubSpot, note our own call for the guard, and reopen a wrong number."""
    if lead.data.get("source") != "crm":
        return
    now = service.clock()
    update(lead, last_called_at=now.isoformat())
    enqueue_log(service, s, p, call)
    if outcome == "wrong_party":
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
    """Queue the HubSpot record for a saved outcome; never for a CSV lead or a test call."""
    lead = service.required(s, "leads", call.data["lead_id"], p)
    if (
        lead.data.get("source") != "crm"
        or call.data.get("is_test")
        or not config(service, p).get("hubspot_service_key")
    ):
        return
    service.jobs.enqueue_in(
        s,
        LANE,
        {"kind": "crm_log", "call_id": call.id, **_who(p)},
        f"crm-log:{call.id}",
        tenant_id=p["tenant_id"],
        max_attempts=6,
    )
