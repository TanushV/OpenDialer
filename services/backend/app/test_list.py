"""A saved, local list of test contacts and a run bound to its remaining contacts."""

from sqlalchemy import select

from app import ops
from app.calling.blockers import refuse, test_line_refusal
from app.compliance.phone import normalize_phone
from app.optimization.geography import normalize_state
from app.platform.db import MODELS, emit, get, uid, update
from app.platform.errors import fail

NAME = "Test calls"
MAX_CONTACTS = 50


def contacts(s, p, campaign_id):
    model = MODELS["leads"]
    return list(
        s.scalars(
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_campaign_id == campaign_id,
                model.data["test_list_contact"].as_boolean().is_(True),
                model.data["archived"].as_boolean().is_not(True),
            )
            .order_by(model.created_at, model.id)
        )
    )


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
    with service.db.transaction(False) as s:
        data = contacts(s, p, ops.selected_campaign(service, s, p).id)
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
        if row.get("state") and not normalize_state(row["state"]):
            fail(422, f"Row {index}: enter a valid state.")
        normalized[phone.e164] = {
            **row,
            "phone": phone.e164,
            "name": row.get("name") or f"Test phone {index - 1}",
        }
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        if test_list_run(service, s, p):
            fail(409, "End the current test session in the Dialer before replacing its list.")
        campaign = ops.selected_campaign(service, s, p)
        existing = campaign.data.get("lists", {}).get(NAME)
        if existing is not None and not existing.get("test_list"):
            fail(409, f'Rename the existing "{NAME}" outreach list in Leads before saving a test list.')
        for lead in contacts(s, p, campaign.id):
            if lead.data["phone"] not in normalized:
                update(lead, archived=True)
                emit(s, p["tenant_id"], "lead.updated", lead, service.clock())
        ops.import_leads(
            service,
            p,
            {
                "campaign_id": campaign.id,
                "list_name": NAME,
                "rows": list(normalized.values()),
                "test_list": True,
            },
            session=s,
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
        campaign = ops.selected_campaign(service, s, p)
        selected = contacts(s, p, campaign.id)
        if not selected:
            fail(409, "Import a test list in Settings first.")
        run = {
            "id": uid(),
            "campaign_id": campaign.id,
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
    if run and run["id"] == scope["test_run_id"] and call.data["lead_id"] in run["remaining"]:
        remaining = {k: v for k, v in run["remaining"].items() if k != call.data["lead_id"]}
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
        if not run or run["id"] != run_id or run["campaign_id"] != campaign_id:
            fail(409, "Open the Dialer and start the selected test list again.")
        if run["remaining"]:
            return run
        close(service, s, p, user)
    fail(
        409,
        {"code": "TEST_LIST_COMPLETE", "message": "Test list complete. Start another test from Settings."},
    )
