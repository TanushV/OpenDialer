"""Saved test phones and a run bound to its approved destinations."""

from sqlalchemy import select

from app import ops
from app.calling.blockers import refuse, test_line_refusal
from app.compliance.phone import normalize_phone
from app.platform.db import MODELS, doc, emit, get, put, uid, update, workspace
from app.platform.errors import fail

NAME = "Test calls"
MAX_CONTACTS = 50


def _legacy_contacts(s, p):
    model = MODELS["leads"]
    return list(
        s.scalars(
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.data["test_list_contact"].as_boolean().is_(True),
                model.data["archived"].as_boolean().is_not(True),
            )
            .order_by(model.created_at, model.id)
        )
    )


def save_test_contact(service, s, p, phone, name=None):
    target = ops._by_key(service, s, p, "test_contacts", phone)
    if target:
        if name and target.data["name"] != name:
            update(target, name=name)
        return target
    endpoint = ops._by_key(service, s, p, "phone_endpoints", phone)
    if endpoint is None:
        endpoint = put(
            s,
            "phone_endpoints",
            workspace(p),
            p["seller_id"],
            {"phone": phone, "format_status": "valid"},
            key=phone,
            now=service.clock(),
        )
    return put(
        s,
        "test_contacts",
        workspace(p),
        p["seller_id"],
        {"phone": phone, "name": name or "Your test phone", "endpoint_id": endpoint.id},
        key=phone,
        now=service.clock(),
    )


def recipient_kind(record):
    return "test_contacts" if record.get("test_contact") else "leads"


def call_recipient(service, s, p, record, *, lock=False):
    return ops.required(service, s, recipient_kind(record), record["lead_id"], p, lock=lock)


def test_contact_view(target):
    return {**doc(target), "test_contact": True, "status": "eligible", "attempt_count": 0}


def _saved_contacts(service, s, p, user):
    ids = user.data.get("test_contact_ids")
    if ids is None:
        # Saved lists from older stores used private lead copies. Preserve their approved numbers.
        targets = [
            save_test_contact(service, s, p, row.data["phone"], row.data["name"])
            for row in _legacy_contacts(s, p)
        ]
        update(user, test_contact_ids=list(dict.fromkeys(row.id for row in targets)))
        return list({row.id: row for row in targets}.values())
    return [ops.required(service, s, "test_contacts", identity, p) for identity in ids]


def test_list_run(service, s, p):
    user = get(s, "users", p.get("user_id"), p["tenant_id"])
    return user.data.get("test_run") if user and user.seller_id == p["seller_id"] else None


def permits(run, p, lead):
    return bool(
        run
        and run["id"] == p.get("test_run_id")
        and lead.data.get("test_list_contact")
        and not lead.data.get("archived")
        and run["remaining"].get(lead.id) == lead.data["phone"]
    )


def view(service, p):
    with service.db.transaction() as s:
        user = ops.required(service, s, "users", p["user_id"], p, lock=True)
        data = _saved_contacts(service, s, p, user)
        return {
            "list_name": NAME,
            "contacts": sorted(
                ({"id": row.id, "name": row.data["name"], "phone": row.data["phone"]} for row in data),
                key=lambda row: row["phone"],
            ),
        }


def replace(service, p, rows):
    normalized = {}
    for index, row in enumerate(rows, 2):
        phone = normalize_phone(str(row.get("phone") or ""))
        if not phone.e164 or not phone.e164.startswith("+1") or phone.extension:
            fail(422, f"Row {index}: enter a valid +1 test phone with no extension.")
        if phone.e164 in normalized:
            fail(422, f"Row {index}: each test phone must appear once.")
        normalized[phone.e164] = str(row.get("name") or "").strip()[:200] or f"Test phone {index - 1}"
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        user = ops.required(service, s, "users", p["user_id"], p, lock=True)
        if user.data.get("test_run"):
            fail(409, "End the current test session in the Dialer before replacing its list.")
        update(
            user,
            test_contact_ids=[
                save_test_contact(service, s, p, phone, name).id for phone, name in normalized.items()
            ],
        )
    return view(service, p)


def start(service, p):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        user = ops.required(service, s, "users", p["user_id"], p, lock=True)
        if user.data.get("test_run") or ops.in_test_session(service, p):
            fail(409, "End the current test session in the Dialer before starting another.")
        if user.data.get("presence", {}).get("status") == "ready":
            fail(409, "Pause calling in the Dialer before starting a test list.")
        if set(ops.quiescence_in(service, s, p)["reasons"]) - {"ACTIVE_LEG"}:
            fail(409, "Finish the current call or recovery in the Dialer before starting a test list.")
        selected = _saved_contacts(service, s, p, user)
        if not selected:
            fail(409, "Import a test list in Settings first.")
        run = {
            "id": uid(),
            "remaining": {row.id: row.data["phone"] for row in selected},
        }
        update(user, test_run=run, presence={**user.data.get("presence", {}), "status": "paused"})
        emit(s, p["tenant_id"], "user.updated", user, service.clock())
        return {"id": run["id"], "phones": sorted(run["remaining"].values())}


def close(service, s, p, user):
    update(user, test_run=None, presence={**user.data.get("presence", {}), "status": "paused"})
    emit(s, p["tenant_id"], "user.updated", user, service.clock())


def complete_call(service, s, p, call):
    scope = call.data.get("dial_scope", {})
    if not scope.get("test_run_id"):
        return
    user = ops.required(service, s, "users", p["user_id"], p, lock=True)
    run = user.data.get("test_run")
    if run and run["id"] == scope["test_run_id"]:
        phone = call_recipient(service, s, p, call.data).data["phone"]
        remaining = {k: v for k, v in run["remaining"].items() if v != phone}
        update(user, test_run={**run, "remaining": remaining})


def scope_for_queue(service, p, campaign_id, run_id):
    """Resolve an explicit run. Stale requests never enter the prospect queue."""
    if not run_id:
        with service.db.transaction(False) as s:
            if test_list_run(service, s, p):
                refuse(test_line_refusal(None, test_session=True, test_lines=()))
        return None
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        user = ops.required(service, s, "users", p["user_id"], p, lock=True)
        run = user.data.get("test_run")
        if (
            not run
            or run["id"] != run_id
            or (campaign_id and run.get("campaign_id", campaign_id) != campaign_id)
        ):
            fail(409, "Open the Dialer and start the selected test list again.")
        if run["remaining"]:
            return run
        close(service, s, p, user)
    fail(
        409,
        {"code": "TEST_LIST_COMPLETE", "message": "Test list complete. Start another test from Settings."},
    )
