"""W6: number inventory stays scoped, carrier rejections are definite, and preflight is honest."""

import httpx
import pytest
from app.db import Job, put, update
from sqlalchemy import event
from test_context_reads import count_decrypted
from test_number_reconciliation import configure_fixture
from test_remediation_privacy import principal


def foreign_inventory(service, count, phone=None):
    """Another tenant's numbers, as a shared server holds them."""
    with service.db.transaction() as s:
        for i in range(count):
            number = phone if phone and i == 0 else f"+1628555{i:04d}"
            put(
                s,
                "number_inventory",
                "foreign-tenant",
                "foreign-seller",
                {"phone": number, "lifecycle": "healthy", "daily_limit": 5, "capacity": 1},
                key=number,
            )


def test_inventory_sync_detects_foreign_ownership_without_decrypting_foreign_rows(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)
    foreign_inventory(service, 80, phone="+16285550000")
    with service.db.transaction(False) as s:
        phone = service.required(s, "number_inventory", identifier, p).data["phone"]

    async def inventory():
        return [
            {"id": "owned", "phone_number": phone, "connection_id": "old", "status": "active"},
            {"id": "elsewhere", "phone_number": "+16285550000", "status": "active"},
        ]

    monkeypatch.setattr(carrier, "list_numbers", inventory)
    decrypted = count_decrypted(app, monkeypatch)
    r = workspace["client"].post("/v1/admin/numbers/sync", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["assigned_elsewhere"] == 1 and r.json()["added"] == 0
    assert len(decrypted) < 40, f"Sync decrypted {len(decrypted)} records with 80 foreign numbers"
    with service.db.transaction(False) as s:
        foreign = [n for n in service.scoped(s, "number_inventory", p) if n.data["phone"] == "+16285550000"]
        assert not foreign, "A number owned by another tenant is never imported here"


def test_adding_a_number_owned_elsewhere_is_rejected_by_lookup_key(workspace, app, monkeypatch):
    service = app.state.service
    foreign_inventory(service, 80, phone="+16285550000")
    decrypted = count_decrypted(app, monkeypatch)
    c, h = workspace["client"], workspace["headers"]
    assert c.post("/v1/admin/numbers", headers=h, json={"phone": "+16285550000"}).status_code == 409
    assert len(decrypted) < 40, f"Adding one number decrypted {len(decrypted)} records"
    assert c.post("/v1/admin/numbers", headers=h, json={"phone": "+16285559999"}).status_code == 200


@pytest.mark.parametrize(
    "status,expected", [("observed", "ceiling"), ("unknown", "ceiling"), ("investigating", "current")]
)
def test_workload_recommendation_uses_reviewed_ceiling_and_health_not_sample_counts(
    workspace, app, monkeypatch, status, expected
):
    service, p = app.state.service, principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    number = c.get("/v1/workspace", headers=h).json()["numbers"][0]
    original = service.inventory_evidence

    def evidence(s, principal, fast=False):
        views = original(s, principal, fast=fast)
        for view in views:
            if view["id"] == number["id"]:
                view["health"] = {**view["health"], "status": status, "delivery_samples": 12}
        return views

    monkeypatch.setattr(service, "inventory_evidence", evidence)
    with service.db.transaction() as s:
        update(service.required(s, "number_inventory", number["id"], p), daily_limit=5)
    view = c.get(f"/v1/admin/numbers/{number['id']}/review", headers=h)
    assert view.status_code == 200, view.text
    workload = view.json()["workload"]
    ceiling = workload["maximum_reviewed_limit"]
    assert ceiling > 12, "The fixture's staffed ceiling must exceed the sample count to distinguish them"
    assert workload["recommended_limit"] == (ceiling if expected == "ceiling" else min(5, ceiling))


def rejected(status, title="Invalid connection"):
    request = httpx.Request("PATCH", "https://api.telnyx.com/v2/phone_numbers/owned")
    response = httpx.Response(status, json={"errors": [{"code": "10015", "title": title}]}, request=request)
    return httpx.HTTPStatusError("carrier rejected", request=request, response=response)


def test_definitive_carrier_rejection_is_terminal_with_the_provider_error(
    workspace, app, carrier, monkeypatch, clock
):
    service, p, identifier = configure_fixture(workspace, app)
    sent = []

    async def configure(number, connection):
        sent.append(number)
        raise rejected(422)

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    path = f"/v1/admin/numbers/{identifier}/configure"
    c, h = workspace["client"], workspace["headers"]
    r = c.post(path, headers=h)
    assert r.status_code == 422, r.text
    assert "Invalid connection" in r.json()["detail"]
    with service.db.transaction(False) as s:
        intent = next(
            x for x in service.scoped(s, "command_outbox", p) if x.data.get("kind") == "number_configure"
        )
        assert intent.data["status"] == "failed" and "Invalid connection" in intent.data["provider_error"]
        number = service.required(s, "number_inventory", identifier, p).data
        assert number["configuration_status"] == "failed" and not number["identity_configured"]

    async def accepted(number, connection):
        sent.append(number)
        return {"id": number, "connection_id": connection}

    monkeypatch.setattr(carrier, "configure_number", accepted, raising=False)
    # A rejected request was not applied: a corrected attempt sends again instead of reconciling.
    retry = c.post(path, headers=h)
    assert retry.status_code == 200, retry.text
    assert sent == ["owned", "owned"]


@pytest.mark.parametrize("status", [408, 429, 500])
def test_ambiguous_carrier_failures_remain_uncertain(workspace, app, carrier, monkeypatch, status):
    service, p, identifier = configure_fixture(workspace, app)

    async def configure(number, connection):
        raise rejected(status, "Try again")

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    r = workspace["client"].post(f"/v1/admin/numbers/{identifier}/configure", headers=workspace["headers"])
    assert r.status_code == 503, r.text


def test_preflight_without_campaign_or_budget_reports_blockers(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for row in [*service.scoped(s, "budgets", p), *service.scoped(s, "campaigns", p)]:
            s.delete(row)
    r = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    assert {"NO_CAMPAIGN", "BUDGET_NOT_CONFIGURED"} <= set(r.json()["blockers"])
    assert r.json()["ready_to_call"] is False


def test_preflight_requires_a_number_with_current_callback_proof_not_a_lifecycle_label(workspace, app):
    service, p = app.state.service, principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    assert "NO_HEALTHY_NUMBERS" not in c.get("/v1/admin/preflight", headers=h).json()["blockers"]
    with service.db.transaction() as s:
        for number in service.scoped(s, "number_inventory", p):
            # Still labelled healthy, but its callback test no longer holds for the route.
            update(number, lifecycle="healthy", callback_tested=False, callback_proof=None)
    assert "NO_HEALTHY_NUMBERS" in c.get("/v1/admin/preflight", headers=h).json()["blockers"]


def test_routine_preflight_uses_quick_check_and_deep_runs_the_full_integrity_check(workspace, app):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(app.state.db.engine, "before_cursor_execute", record)
    try:
        c, h = workspace["client"], workspace["headers"]
        routine = c.get("/v1/admin/preflight", headers=h)
        quick = list(statements)
        statements.clear()
        deep = c.get("/v1/admin/preflight?deep=true", headers=h)
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", record)
    assert routine.status_code == deep.status_code == 200
    assert routine.json()["integrity_ok"] and routine.json()["integrity_check"] == "quick"
    assert deep.json()["integrity_ok"] and deep.json()["integrity_check"] == "full"
    if app.state.db.sqlite:  # PostgreSQL has no page-level integrity pragma; the flags still apply.
        assert any("quick_check" in v for v in quick) and not any("integrity_check" in v for v in quick)
        assert any("integrity_check" in v for v in statements)


def test_preparation_capability_is_derived_from_configured_inference(workspace, app, monkeypatch):
    service = app.state.service
    c, h = workspace["client"], workspace["headers"]

    def preparation():
        items = c.get("/v1/admin/preflight", headers=h).json()["capabilities"]
        return next(item for item in items if item["id"] == "preparation")

    assert preparation()["state"] == "software_verified"
    monkeypatch.setattr(service.settings, "simulation", False)
    generic = preparation()
    assert generic["state"] == "degraded" and generic["reason_codes"] == ["APPROVED_FALLBACK_ONLY"]
    assert generic["blocks"] == "none", "The approved generic script keeps calling available"
    monkeypatch.setattr(service, "inference", lambda *args: object())
    configured = preparation()
    assert configured["state"] == "configured"
    assert configured["reason_codes"] == ["LIVE_INFERENCE_ACCEPTANCE_REQUIRED"]


def test_forecast_refresh_ignores_dead_and_older_generation_jobs(workspace, app):
    service, p = app.state.service, principal(workspace)
    campaign = workspace["campaign_id"]
    with service.db.transaction() as s:
        generation = service.report_revision(s, p)
        for key, status in (
            (f"planning:{campaign}:configured:{generation}:0", "dead"),
            (f"planning:{campaign}:configured:{generation - 1}:0", "queued"),
        ):
            job = service.jobs.enqueue_in(
                s,
                "ml",
                {"kind": "forecast_refresh", "seller_id": p["seller_id"]},
                key,
                tenant_id=p["tenant_id"],
            )
            s.get(Job, job["id"]).status = status
    value = service.proposal(p, campaign)
    with service.db.transaction(False) as s:
        current = s.get(Job, value["projection"]["job_id"])
        assert current.status == "queued"
        assert current.key.startswith(f"planning:{campaign}:configured:{generation}:")
        assert not current.key.endswith(":0")
    assert value["projection"]["refresh_status"] == "queued"


@pytest.mark.asyncio
async def test_inventory_reads_the_largest_documented_page():
    from app.providers.telnyx import TelnyxCarrier

    sizes = []

    def response(request):
        sizes.append(request.url.params["page[size]"])
        return httpx.Response(200, json={"data": [], "meta": {"page_number": 1, "total_pages": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert await TelnyxCarrier("fixture", "c", http=http).list_numbers() == []
    assert sizes == ["250"]


def test_dead_carrier_stubs_are_removed():
    from app.providers.telnyx import TelnyxCarrier

    assert not hasattr(TelnyxCarrier, "rnd") and not hasattr(TelnyxCarrier, "lookup")
