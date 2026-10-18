"""Bounded private-help receipts. Raw questions/answers never enter this store."""

import hashlib
from datetime import timedelta

from app.db import emit, put, update
from app.service import dt, fail


def request_key(call_id, identity):
    return f"coach:{call_id}:{identity}"


def _load(service, s, p, call_id):
    call = service.required(s, "call_attempts", call_id, p)
    s.refresh(call, with_for_update=True)
    user = service.required(s, "users", p["user_id"], p)
    if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin", "rep"}:
        fail(403, "Current workspace access is required")
    if user.data["role"] == "rep" and call.data["rep_id"] != user.id:
        fail(404, "Object not found")
    state = service._by_key(s, p, "conversation_states", call_id)
    if not state:
        state = put(
            s,
            "conversation_states",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call_id, "version": call.data.get("transcript_version", 0), "privacy_contract": 1},
            key=call_id,
            now=service.clock(),
        )
    entries = [
        dict(r) for r in state.data.get("manual_help_requests", []) if dt(r["expires_at"]) > service.clock()
    ]
    return call, state, entries


def _new(service, p, entries, identity, status, digest=None):
    if len(entries) >= 32:
        fail(429, "Too many private help requests in five minutes; wait before asking again")
    item = {
        "id": identity,
        "user_id": p["user_id"],
        "status": status,
        "digest": digest,
        "received_at": service.clock().isoformat(),
        "expires_at": (service.clock() + timedelta(seconds=300)).isoformat(),
    }
    entries.append(item)
    return item


def begin(service, p, call_id, identity, question):
    digest = hashlib.sha256((question or "").encode()).hexdigest()
    with service.db.transaction() as s:
        call, state, entries = _load(service, s, p, call_id)
        if not service._help_allowed(s, p, call):
            return {"action": "NO_ACTION", "status": "unavailable"}
        prior = next((r for r in state.data.get("manual_help_requests", []) if r["id"] == identity), None)
        if prior:
            if dt(prior["expires_at"]) <= service.clock():
                return {"action": "NO_ACTION", "status": "expired"}
            if prior["user_id"] != p["user_id"] or prior.get("digest") not in {None, digest}:
                fail(409, "Private request identity already describes different work")
            content = service.raw.get("help:" + p["tenant_id"] + ":" + prior.get("event_id", ""))
            if content and prior["status"] == "complete":
                return content
            return {"action": "NO_ACTION", "status": prior["status"]}
        if service._request_hold(s, p, request_key(call_id, identity)):
            return {"action": "NO_ACTION", "status": "expired"}
        _new(service, p, entries, identity, "pending", digest)
        update(state, manual_help_requests=entries, manual_help_current=identity, coach_request_key=None)
        service.coach.cancel(call_id)
    return None


def finish(service, p, call_id, identity, result):
    with service.db.transaction() as s:
        call = service.required(s, "call_attempts", call_id, p)
        s.refresh(call, with_for_update=True)
        state = service._by_key(s, p, "conversation_states", call_id)
        entries = [dict(r) for r in state.data.get("manual_help_requests", [])] if state else []
        item = next((r for r in entries if r["id"] == identity), None)
        if not item or dt(item["expires_at"]) <= service.clock():
            return {"action": "NO_ACTION", "status": "expired"}
        if item["status"] != "pending":
            return {"action": "NO_ACTION", "status": item["status"]}
        if state.data.get("manual_help_current") != identity:
            result = {"action": "NO_ACTION", "status": "superseded"}
        item.update(status="complete" if result.get("id") else result.get("status", "no_action"))
        if result.get("id"):
            item["event_id"] = result["id"]
        update(state, manual_help_requests=entries)
        return result


def cancel(service, p, call_id, identity):
    with service.db.transaction() as s:
        call, state, entries = _load(service, s, p, call_id)
        item = next((r for r in entries if r["id"] == identity), None)
        if item is None:
            item = _new(service, p, entries, identity, "cancelled")
        item["status"] = "cancelled"
        if item.get("event_id"):
            event = service.required(s, "coach_events", item["event_id"], p)
            update(event, expires_at=service.clock().isoformat(), cancelled=True)
            service.raw.discard("help:" + p["tenant_id"] + ":" + event.id)
        update(state, manual_help_requests=entries)
        if state.data.get("manual_help_current") == identity:
            update(state, coach_request_key=None, manual_help_current=None)
            service.coach.cancel(call_id)
        emit(s, p["tenant_id"], "coach.cancelled", call, service.clock())
    return {"action": "NO_ACTION", "status": "cancelled", "request_id": identity}


def current(service, state, request):
    now = service.clock()
    identity = request.get("manual_request_id")
    pending = next(
        (
            r
            for r in (state.data.get("manual_help_requests", []) if state else [])
            if r["id"] == state.data.get("manual_help_current")
            and r["status"] == "pending"
            and dt(r["expires_at"]) > now
        ),
        None,
    )
    return bool(pending and pending["id"] == identity) if identity else pending is None
