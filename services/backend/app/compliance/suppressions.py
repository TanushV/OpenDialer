"""Exact opt-out receipts, bounded effects and independent denial recovery."""

import hashlib
import hmac
import json
import logging

from sqlalchemy import delete, select

from app.compliance.phone import normalize_phone
from app.db import MODELS, ActiveClaim, doc, emit, get, put, uid, update
from app.service import dt, fail


def identity(entry):
    return "suppression:" + hashlib.sha256(entry["event_id"].encode()).hexdigest()


def fingerprint(entry):
    return hashlib.sha256(json.dumps([entry["phone"], entry["reason"]]).encode()).hexdigest()


def journal_entries(service):
    entries = service.vault.read_journal("suppression")
    seen = {}
    for entry in entries:
        try:
            valid = (
                all(
                    isinstance(entry.get(k), str) and entry[k]
                    for k in ("event_id", "tenant_id", "seller_id", "phone", "at")
                )
                and normalize_phone(entry["phone"]).e164 == entry["phone"]
                and entry["reason"] in {"opt_out", "wrong_party", "temporary_hold", "invalid_endpoint"}
                and dt(entry["at"]).tzinfo is not None
            )
            key = (entry["tenant_id"], entry["seller_id"], identity(entry))
            if not valid or key in seen and seen[key] != fingerprint(entry):
                raise ValueError("Conflicting denial")
            seen[key] = fingerprint(entry)
        except (ValueError, KeyError, TypeError, AttributeError):
            fail(409, "Suppression journal cannot be verified; recover its protected copy before resuming")
    return entries


def apply_in(service, s, entry):
    p = {k: entry[k] for k in ("tenant_id", "seller_id")}
    p.update(user_id=entry.get("user_id", "journal-recovery"), role="admin")
    key = hmac.new(service.db.identity_key, entry["phone"].encode(), hashlib.sha256).hexdigest()
    receipt = service._by_key(s, p, "audit_events", identity(entry))
    if receipt and receipt.data.get("detail", {}).get("request_hash") != fingerprint(entry):
        fail(409, "Opt-out request identity already describes another endpoint or reason")
    row = service._by_key(s, p, "suppressions", key)
    if not row:
        row = put(
            s,
            "suppressions",
            p["tenant_id"],
            p["seller_id"],
            {
                "phone": entry["phone"],
                "reason": entry["reason"],
                "active": True,
                "permanent": entry["reason"] != "temporary_hold",
                "effective_at": entry["at"],
                "origin_event_id": entry["event_id"],
            },
            key=entry["phone"],
            object_id=receipt.data["target_id"] if receipt else None,
            now=dt(entry["at"]),
        )
    elif not row.data.get("active", True) or not row.data.get("permanent"):
        permanent = row.data.get("permanent") or entry["reason"] != "temporary_hold"
        reason = row.data["reason"] if row.data.get("permanent") else entry["reason"]
        update(row, active=True, reason=reason, permanent=permanent)
    endpoint = service._by_key(s, p, "phone_endpoints", key)
    if endpoint:
        from app.compliance.consent import revoke_effects

        revoke_effects(service, s, p, endpoint.id, ["live", "transcription", "recording", "prerecorded"])
        reservations = MODELS["reservations"]
        for reservation in s.scalars(
            select(reservations).where(
                reservations.tenant_id == p["tenant_id"],
                reservations.seller_id == p["seller_id"],
                reservations.op_endpoint_id == endpoint.id,
                reservations.op_status == "reserved",
            )
        ):
            update(reservation, status="cancelled")
            s.execute(
                delete(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.seller_id == p["seller_id"],
                    ActiveClaim.reservation_id == reservation.id,
                    ActiveClaim.call_id.is_(None),
                )
            )
    if not receipt:
        put(
            s,
            "audit_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "actor_id": p["user_id"],
                "action": "endpoint.suppressed",
                "target_id": row.id,
                "detail": {"reason": entry["reason"], "request_hash": fingerprint(entry)},
            },
            key=identity(entry),
            now=dt(entry["at"]),
        )
        emit(s, p["tenant_id"], "suppression.created", row, service.clock())
    return doc(row)


def replay_in(service, s, *, p=None, entries=None):
    entries = journal_entries(service) if entries is None else entries
    for entry in entries:
        if p and (entry["tenant_id"], entry["seller_id"]) != (p["tenant_id"], p["seller_id"]):
            continue
        seller = get(s, "sellers", entry["seller_id"], entry["tenant_id"])
        if seller:
            service.db.authorize_stream(s, entry)
            apply_in(service, s, entry)


def replay(service):
    entries = journal_entries(service)
    if entries:
        with service.db.transaction() as s:
            replay_in(service, s, entries=entries)


def persist_fault(service, p, entry):
    try:
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            seller = service.required(s, "sellers", p["seller_id"], p)
            update(
                seller,
                journal_recovery_required=True,
                journal_expected={
                    **seller.data.get("journal_expected", {}),
                    identity(entry): fingerprint(entry),
                },
            )
    except Exception as exc:
        logging.getLogger(__name__).error("Opt-out recovery hold could not persist: %s", type(exc).__name__)


def record(service, p, phone, reason, *, event_id=None, session=None):
    normalized = normalize_phone(phone)
    if not normalized.e164 or reason not in {"opt_out", "wrong_party", "temporary_hold", "invalid_endpoint"}:
        fail(422, "Valid phone endpoint and suppression reason required")
    entry = {
        **{k: p[k] for k in ("tenant_id", "seller_id", "user_id")},
        "event_id": event_id or uid(),
        "phone": normalized.e164,
        "reason": reason,
        "at": service.clock().isoformat(),
    }
    started = False
    failed = False

    def mark_failed():
        nonlocal failed
        failed = True
        service.__dict__.setdefault("_journal_faults", set()).add((p["tenant_id"], p["seller_id"]))

    try:
        with service.db.transaction(session=session) as s:
            service.db.authorize_stream(s, p)
            seller = service.required(s, "sellers", p["seller_id"], p)
            expected = seller.data.get("journal_expected", {})
            if identity(entry) in expected and expected[identity(entry)] != fingerprint(entry):
                fail(409, "Recover the original opt-out request; its endpoint or reason cannot change")
            prior = service._by_key(s, p, "audit_events", identity(entry))
            if prior and prior.data.get("detail", {}).get("request_hash") != fingerprint(entry):
                fail(409, "Opt-out request identity already describes another endpoint or reason")
            if seller.data.get("journal_recovery_required") or (
                p["tenant_id"],
                p["seller_id"],
            ) in service.__dict__.get("_journal_faults", set()):
                journal_entries(service)  # Never extend known partial/corrupt storage during repair.
            started = True
            s.info.setdefault("before_rollback", []).append(mark_failed)
            s.info.setdefault("after_transaction", []).append(
                lambda: persist_fault(service, p, entry) if failed else None
            )
            if not prior:
                service.vault.append_journal("suppression", entry)
            return apply_in(service, s, entry)
    except Exception:
        if not started:
            raise
        mark_failed()
        fail(503, "Opt-out receipt is incomplete; outbound held until journal recovery is reviewed")
