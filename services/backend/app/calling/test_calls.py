"""Claim approved test phones using the calling engine's own safety checks."""

from sqlalchemy import select

from app import ops
from app.calling.blockers import test_line_refusal
from app.calling.decide import Situation, require
from app.calling.records import presence_of
from app.calling.reservations import hold
from app.compliance.policy import Decision
from app.platform.db import ActiveClaim, doc, emit, scoped_where, workspace
from app.platform.errors import fail


def authorized(service, s, p, contact, number, scope):
    """Recheck before dispatch so a stale test never becomes an ordinary dial."""
    phone = contact.data["phone"]
    if scope.get("test_run_id"):
        run = ops.test_list_run(service, s, p)
        selected = run and run["id"] == scope["test_run_id"] and phone in run["remaining"].values()
    else:
        selected = ops.in_test_session(service, p) and phone == ops.test_phone(service, p)
    if blocked := test_line_refusal(phone, test_session=True, test_lines={phone} if selected else ()):
        return Decision(False, [blocked[0].code])
    if not phone.startswith("+1"):
        return Decision(False, ["ROUTE_NOT_ALLOWED"])
    if not _rep_ready(service, s, p):
        return Decision(False, ["REP_NOT_READY"])
    if ops.test_phone_issue(service, s, p, phone):
        return Decision(False, ["INTERNAL_SUPPRESSION"])
    if not ops.number_authorized(service, p, number):
        return Decision(False, ["NUMBER_NOT_AUTHORIZED"])
    if not ops.number_readiness_in(service, s, p, number)["outbound_ready"]:
        return Decision(False, ["NUMBER_UNHEALTHY"])
    return Decision(True, [])


def _phones(service, s, p, run_id=None):
    run = ops.test_list_run(service, s, p)
    if run_id and run and run["id"] == run_id:
        return list(run["remaining"].values())
    if not run_id and not run and ops.in_test_session(service, p):
        return [ops.test_phone(service, p)]
    return []


def _ready_number(service, s, p):
    numbers = ops.scoped(service, s, "number_inventory", p)
    if service.managed.enabled(workspace(p)) or service.managed.pool is not None:
        assigned = service.managed.assigned(workspace(p), [doc(n) for n in numbers])
        ids = {n["id"] for n in assigned}
        numbers = [n for n in numbers if n.id in ids]
    return next(
        (
            n
            for n in numbers
            if ops.number_authorized(service, p, n)
            and ops.number_readiness_in(service, s, p, n)["outbound_ready"]
        ),
        None,
    )


def _rep_ready(service, s, p):
    user = ops.required(service, s, "users", p["user_id"], p)
    return presence_of(user.data).status == "ready"


def readiness(service, p):
    with service.db.transaction(False) as s:
        run = ops.test_list_run(service, s, p)
        if not run and not ops.in_test_session(service, p):
            return None
        scope = {**p, "test_only": True, "test_run_id": run["id"] if run else None}
        phones = _phones(service, s, p, scope["test_run_id"])
        blockers = ops.dial_blockers_in(service, s, scope, rep=False)
        available = [phone for phone in phones if not ops.test_phone_issue(service, s, p, phone)]
        reasons = ["INTERNAL_SUPPRESSION"] if phones and not available else []
        if phones and not blockers and _ready_number(service, s, p) is None:
            reasons = ["NUMBER_UNHEALTHY"]
            available = []
        detail = ops.no_lead_detail(service, s, scope, reasons) if reasons else {}
        return {
            "blockers": [b.detail() for b in blockers],
            "callable_now": 0 if blockers else len(available),
            "inspected": len(phones),
            "why_none": detail.get("message") or ("Test list complete." if not phones else None),
            "fix": detail.get("fix"),
            "next_action": "Start a new test list in Settings." if not phones else None,
            "checked_at": service.clock().isoformat(),
        }


def reserve(service, p, run_id):
    scope = {"test_only": True, **({"test_run_id": run_id} if run_id else {})}
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        now = service.clock()
        phones = _phones(service, s, p, run_id)
        if not phones:
            fail(409, "The test session ended. Press Test call in the Dialer to start another.")
        require(Situation(blockers=tuple(ops.dial_blockers_in(service, s, {**p, **scope}, rep=True))))
        if not _rep_ready(service, s, p):
            fail(409, "The Dialer is paused. Press Test call or Dial now to place a test call.")
        ops._expire_claims(service, s)
        claims = list(s.scalars(select(ActiveClaim).where(*scoped_where(ActiveClaim, p))))
        own = next((claim for claim in claims if claim.rep_id == p["user_id"]), None)
        if own:
            require(Situation(claim="live_call" if own.call_id else "reservation"))
        number = _ready_number(service, s, p)
        claimed = {claim.endpoint_id for claim in claims}
        reasons = []
        for phone in phones:
            if ops.test_phone_issue(service, s, p, phone):
                reasons.append("INTERNAL_SUPPRESSION")
                continue
            target = ops.save_test_contact(service, s, p, phone)
            if target.data["endpoint_id"] not in claimed:
                break
            reasons.append("ENDPOINT_CLAIMED")
        else:
            require(Situation(queue="none"), ops.no_lead_detail(service, s, {**p, **scope}, reasons))
        decision = authorized(service, s, p, target, number, scope)
        if not decision.allowed:
            require(Situation(queue="none"), ops.no_lead_detail(service, s, {**p, **scope}, decision.reasons))
        reservation = hold(s, p, target, number, {"test_contact": True, "dial_scope": scope}, now=now)
        shared = service.managed.pool is not None
        if not shared:
            emit(s, workspace(p), "reservation.created", reservation, now)
        return {
            "reservation_id": reservation.id,
            "expires_at": reservation.data["expires_at"],
            "lead": ops.test_contact_view(target),
            "number": doc(number),
            **({"preferred_is_bound": False} if shared else {}),
        }
