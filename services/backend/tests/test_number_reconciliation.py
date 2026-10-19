"""Owned-number changes are resumable and cannot purchase or silently lose callbacks."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from app.db import update
from app.providers.telnyx import TelnyxCarrier
from fastapi import HTTPException
from test_remediation_privacy import principal


@pytest.mark.asyncio
async def test_inventory_pages_are_complete_or_explicitly_rejected():
    seen = []

    def response(request):
        page = int(request.url.params["page[number]"])
        seen.append(page)
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(page), "phone_number": "+14155550100"}],
                "meta": {"page_number": page, "total_pages": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert len(await TelnyxCarrier("fixture", "c", http=http).list_numbers()) == 2
    assert seen == [1, 2]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [], "meta": {"total_pages": 101}})
        )
    ) as http:
        with pytest.raises(ValueError, match="inventory|pagination"):
            await TelnyxCarrier("fixture", "c", http=http).list_numbers()


def configure_fixture(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        number = service.scoped(s, "number_inventory", p)[0]
        update(number, provider_id="owned", connection_id="old")
        identifier = number.id
    service.vault.write(p["tenant_id"], {"telnyx_connection_id": "desired"})
    return service, p, identifier


def test_lost_configuration_response_reconciles_before_another_patch(
    workspace, app, carrier, monkeypatch, clock
):
    service, p, identifier = configure_fixture(workspace, app)
    sent, retrieved = [], []

    async def configure(number, connection):
        sent.append((number, connection))
        raise TimeoutError("lost after applied")

    async def retrieve(number):
        retrieved.append(number)
        return {"id": number, "connection_id": "desired"}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    monkeypatch.setattr(carrier, "get_number", retrieve, raising=False)
    path = f"/v1/admin/numbers/{identifier}/configure"
    c, h = workspace["client"], workspace["headers"]
    assert c.post(path, headers=h).status_code == 503
    clock.now += timedelta(seconds=61)
    r = c.post(path, headers=h)
    assert r.status_code == 200, r.text
    assert len(sent) == 1 and retrieved == ["owned"]
    assert r.json()["callback_tested"] is False
    with service.db.transaction(False) as s:
        intents = [
            x for x in service.scoped(s, "command_outbox", p) if x.data.get("kind") == "number_configure"
        ]
        assert len(intents) == 1 and intents[0].data["status"] == "completed"


def test_inventory_routing_change_invalidates_callback_evidence_and_keeps_identity(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)
    with service.db.transaction(False) as s:
        prior = service.required(s, "number_inventory", identifier, p).data
        phone = prior["phone"]

    async def inventory():
        return [{"id": "owned", "phone_number": phone, "connection_id": "different", "status": "active"}]

    monkeypatch.setattr(carrier, "list_numbers", inventory)
    r = workspace["client"].post("/v1/admin/numbers/sync", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    with service.db.transaction(False) as s:
        current = service.required(s, "number_inventory", identifier, p).data
        assert current["phone"] == phone
        assert current["connection_id"] == "different"
        assert not current["callback_tested"]
        assert current["lifecycle"] == "investigating"
    assert not carrier.purchases


def test_absent_number_is_flagged_without_releasing_the_callback_identity(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)

    async def empty():
        return []

    monkeypatch.setattr(carrier, "list_numbers", empty)
    r = workspace["client"].post("/v1/admin/numbers/sync", headers=workspace["headers"])
    assert r.status_code == 200
    with service.db.transaction(False) as s:
        row = service.required(s, "number_inventory", identifier, p)
        assert row.data["provider_present"] is False
        assert row.data["lifecycle"] == "paused_for_outbound"
        assert row.data["callback_retained"] is True


@pytest.mark.parametrize(
    "change", [{"daily_limit": -1}, {"capacity": 0}, {"capacity": 50}, {"daily_limit": True}]
)
def test_edit_number_rejects_invalid_workload_limits(workspace, change):
    c, h = workspace["client"], workspace["headers"]
    number = c.get("/v1/workspace", headers=h).json()["numbers"][0]
    assert c.patch(f"/v1/admin/numbers/{number['id']}", headers=h, json=change).status_code == 422


def test_production_identity_promotion_requires_confirmed_carrier_route(workspace, app, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    number = c.post("/v1/admin/numbers", headers=h, json={"phone": "+14155550198"}).json()
    monkeypatch.setattr(app.state.service.settings, "simulation", False)
    r = c.patch(
        f"/v1/admin/numbers/{number['id']}",
        headers=h,
        json={
            "lifecycle": "identity_configured",
            "identity_configured": True,
            "registration_reference": "reviewed",
        },
    )
    assert r.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [None, {}, {"total_pages": True}, {"total_pages": 2, "page_number": 2}])
async def test_inventory_does_not_treat_incomplete_pagination_as_complete(meta):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [], "meta": meta}))
    ) as http:
        with pytest.raises(ValueError, match="inventory|pagination"):
            await TelnyxCarrier("fixture", "c", http=http).list_numbers()


@pytest.mark.asyncio
async def test_number_configuration_has_one_owner_and_fences_inventory_changes(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)
    started, release = asyncio.Event(), asyncio.Event()

    async def configure(number, connection):
        started.set()
        await release.wait()
        return {"id": number, "connection_id": connection}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    first = asyncio.create_task(service.configure_owned_number(p, identifier))
    await started.wait()
    with pytest.raises(HTTPException) as duplicate:
        await service.configure_owned_number(p, identifier)
    assert duplicate.value.status_code == 409
    with service.db.transaction() as s:
        number = service.required(s, "number_inventory", identifier, p)
        update(
            number,
            provider_id="replacement",
            configuration_status="review_required",
            identity_configured=False,
        )
    release.set()
    with pytest.raises(HTTPException) as stale:
        await first
    assert stale.value.status_code == 409
    with service.db.transaction(False) as s:
        number = service.required(s, "number_inventory", identifier, p)
        assert number.data["configuration_status"] != "confirmed"
        assert number.data["identity_configured"] is False


def test_unconfirmed_route_is_never_promoted_or_resent(workspace, app, carrier, monkeypatch):
    service, p, identifier = configure_fixture(workspace, app)
    sent = []

    async def configure(number, connection):
        sent.append(number)
        return {"id": number, "connection_id": "different"}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    monkeypatch.setattr(carrier, "get_number", lambda number: configure(number, "unused"), raising=False)
    path = f"/v1/admin/numbers/{identifier}/configure"
    assert workspace["client"].post(path, headers=workspace["headers"]).status_code == 503
    with service.db.transaction(False) as s:
        number = service.required(s, "number_inventory", identifier, p)
        assert number.data["configuration_status"] == "uncertain"
        assert number.data["callback_tested"] is False


@pytest.mark.asyncio
async def test_simulated_owned_configuration_preserves_phone_and_cannot_buy(carrier):
    carrier.numbers = {"owned": {"id": "owned", "phone_number": "+14155550198", "status": "active"}}
    assert len(await carrier.list_numbers()) == 1
    receipt = await carrier.configure_number("owned", "route")
    assert receipt["connection_id"] == "route"
    assert (await carrier.get_number("owned"))["phone_number"] == "+14155550198"
    assert not carrier.purchases


def number_edit(workspace, number_id, **changes):
    service, p = workspace["client"].app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        version = service.required(s, "number_inventory", number_id, p).version
    return {"request_id": str(uuid4()), "expected_version": version, **changes}


def connected_callback(workspace, carrier, *, before_bridge=None):
    from test_inbound_completion import configure, incoming, dispatch, target_payload, webhook, saved

    configure(workspace, destination="sip:reception@example.test")
    task = incoming(workspace)
    dispatch(workspace)
    if before_bridge:
        before_bridge()
    webhook(workspace, "call.bridged", target_payload(carrier), "callback-bridge")
    return saved(workspace, "followup_tasks", task["id"])


def test_callback_operator_note_cannot_promote_identity(workspace, app):
    service, p, identifier = configure_fixture(workspace, app)
    with service.db.transaction() as s:
        update(
            service.required(s, "number_inventory", identifier, p),
            lifecycle="identity_configured",
            configuration_status="confirmed",
            identity_configured=True,
            callback_tested=False,
        )
    response = workspace["client"].patch(
        f"/v1/admin/numbers/{identifier}",
        headers=workspace["headers"],
        json={
            "lifecycle": "callback_tested",
            "callback_tested": True,
            "callback_test_reference": "I think it works",
        },
    )
    assert response.status_code == 409


def test_callback_proof_is_current_scoped_identified_and_simulated(workspace, app, carrier, clock):
    task = connected_callback(workspace, carrier)
    assert task.get("callback_evidence"), "A matched bridge must retain its route and receipt identity"
    proof = task["callback_evidence"]
    assert proof["number_id"] == task["number_id"]
    assert proof["bridge_event_id"] == "callback-bridge"
    assert proof["inbound_event_id"] == "incoming-1"
    assert proof["destination"] == "sip:reception@example.test" and proof["simulated"] is True
    c, h, identifier = workspace["client"], workspace["headers"], task["number_id"]
    body = number_edit(workspace, identifier, callback_task_id=task["id"])
    first = c.patch(f"/v1/admin/numbers/{identifier}", headers=h, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["callback_proof"]["bridge_event_id"] == "callback-bridge"
    assert (
        c.patch(f"/v1/admin/numbers/{identifier}", headers=h, json=body).json()["receipt"]
        == first.json()["receipt"]
    )
    assert (
        c.patch(
            f"/v1/admin/numbers/{identifier}", headers=h, json={**body, "reason": "different"}
        ).status_code
        == 409
    )
    clock.now += timedelta(days=31)
    response = c.post(
        f"/v1/admin/numbers/{identifier}/review",
        headers=h,
        json=number_edit(
            workspace, identifier, decision="resume", review_reference="old evidence", daily_limit=5
        ),
    )
    assert response.status_code == 409 and "callback" in response.text.lower()


def test_route_change_during_callback_cannot_certify_new_route(workspace, carrier):
    from test_inbound_completion import configure

    task = connected_callback(
        workspace, carrier, before_bridge=lambda: configure(workspace, destination="sip:other@example.test")
    )
    c, h = workspace["client"], workspace["headers"]
    response = c.patch(
        f"/v1/admin/numbers/{task['number_id']}",
        headers=h,
        json=number_edit(workspace, task["number_id"], callback_task_id=task["id"]),
    )
    assert response.status_code == 409, response.text
    assert "route" in response.text.lower()


def test_other_number_cannot_reuse_callback_and_get_does_not_call_provider(workspace, carrier):
    task = connected_callback(workspace, carrier)
    c, h = workspace["client"], workspace["headers"]
    other = next(
        n for n in c.get("/v1/workspace", headers=h).json()["numbers"] if n["id"] != task["number_id"]
    )
    response = c.patch(
        f"/v1/admin/numbers/{other['id']}",
        headers=h,
        json=number_edit(workspace, other["id"], callback_task_id=task["id"]),
    )
    assert response.status_code == 409, response.text
    before = (list(carrier.actions), list(carrier.purchases))
    result = c.get(f"/v1/admin/numbers/{task['number_id']}/review", headers=h)
    assert result.status_code == 200, result.text
    assert result.json()["callback_candidates"][0]["id"] == task["id"]
    assert (carrier.actions, carrier.purchases) == before


def test_imported_health_preserves_unknown_provenance_and_fences_edits(workspace, app, clock):
    _, _, identifier = configure_fixture(workspace, app)
    c, h = workspace["client"], workspace["headers"]
    evidence = {
        "source": "authorized carrier report",
        "ownership_reference": "account-owned",
        "observed_at": clock.now.isoformat(),
        "expires_at": (clock.now + timedelta(days=1)).isoformat(),
        "spam_label": None,
    }
    body = number_edit(workspace, identifier, external_reputation=evidence)
    result = c.patch(f"/v1/admin/numbers/{identifier}", headers=h, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["external_reputation"] == evidence
    assert c.patch(f"/v1/admin/numbers/{identifier}", headers=h, json=body).status_code == 200
    stale = c.patch(f"/v1/admin/numbers/{identifier}", headers=h, json={**body, "request_id": str(uuid4())})
    assert stale.status_code == 409
    invalid = c.patch(
        f"/v1/admin/numbers/{identifier}",
        headers=h,
        json=number_edit(workspace, identifier, external_reputation={**evidence, "ownership_reference": ""}),
    )
    assert invalid.status_code == 422


def test_existing_number_recording_unknown_cannot_keep_ready(workspace, app, monkeypatch):
    service, p, identifier = configure_fixture(workspace, app)
    monkeypatch.setattr(service.settings, "simulation", False)
    with service.db.transaction(False) as s:
        phone = service.required(s, "number_inventory", identifier, p).data["phone"]
    service._merge_inventory(
        p, [{"id": "owned", "phone_number": phone, "connection_id": "old", "status": "active"}]
    )
    with service.db.transaction(False) as s:
        value = service.required(s, "number_inventory", identifier, p).data
        assert not value["identity_configured"] and not value["callback_tested"]
        assert value["lifecycle"] == "investigating"


@pytest.mark.asyncio
async def test_configure_disables_recording_before_connection_and_reads_exact_result():
    requests = []

    def response(request):
        import json

        requests.append(
            (request.method, request.url.path, json.loads(request.content) if request.content else None)
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "owned",
                    "phone_number": "+14155550100",
                    "connection_id": "route",
                    "call_recording_enabled": False,
                    "status": "active",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        await TelnyxCarrier("fixture", "route", http=http).configure_number("owned", "route")
    assert requests == [
        (
            "PATCH",
            "/v2/phone_numbers/owned/voice",
            {"call_recording": {"inbound_call_recording_enabled": False}},
        ),
        ("PATCH", "/v2/phone_numbers/owned", {"connection_id": "route"}),
        ("GET", "/v2/phone_numbers/owned", None),
    ]


def test_reassignment_records_original_identity_and_requires_current_binding(workspace, app):
    from app.db import put

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        numbers = service.scoped(s, "number_inventory", p)
        old, new = numbers[0], numbers[1]
        binding = put(
            s,
            "lead_number_bindings",
            p["tenant_id"],
            p["seller_id"],
            {"lead_id": lead.id, "number_id": old.id},
            key=lead.id,
            now=service.clock(),
        )
        lead_id, old_id, new_id, version = lead.id, old.id, new.id, binding.version
    c, h = workspace["client"], workspace["headers"]
    body = {
        "request_id": str(uuid4()),
        "expected_version": version,
        "reassignment": {"lead_id": lead_id, "old_number_id": old_id, "reason": "Reviewed route incident"},
    }
    path = f"/v1/admin/numbers/{new_id}"
    result = c.patch(path, headers=h, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["reassignment"]["old_number_id"] == old_id
    assert result.json()["reassignment"]["new_number_id"] == new_id
    assert result.json()["reassignment"]["effective_at"] == service.clock().isoformat()
    assert c.patch(path, headers=h, json=body).json()["receipt"] == result.json()["receipt"]
    with service.db.transaction(False) as s:
        assert service.required(s, "lead_number_bindings", binding.id, p).data["number_id"] == new_id
        assert service.required(s, "number_inventory", old_id, p).data["inbound_retained"] is True
    stale = c.patch(path, headers=h, json={**body, "request_id": str(uuid4())})
    assert stale.status_code == 409


def test_review_supports_sparse_initial_use_without_five_call_milestone(workspace, carrier, app):
    task = connected_callback(workspace, carrier)
    c, h, identifier = workspace["client"], workspace["headers"], task["number_id"]
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(service.required(s, "number_inventory", identifier, p), daily_limit=5)
    verified = c.patch(
        f"/v1/admin/numbers/{identifier}",
        headers=h,
        json=number_edit(workspace, identifier, callback_task_id=task["id"]),
    )
    assert verified.status_code == 200, verified.text
    body = number_edit(
        workspace,
        identifier,
        decision="increase",
        review_reference="Reviewed initial workload and staffing",
        daily_limit=10,
    )
    result = c.post(f"/v1/admin/numbers/{identifier}/review", headers=h, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["daily_limit"] == 10
    assert result.json()["workload_review"]["health_samples"] == 0
    assert result.json()["workload_review"]["previous_limit"] == 5
    assert (
        c.post(f"/v1/admin/numbers/{identifier}/review", headers=h, json=body).json()["receipt"]
        == result.json()["receipt"]
    )
    over = c.post(
        f"/v1/admin/numbers/{identifier}/review",
        headers=h,
        json=number_edit(
            workspace,
            identifier,
            decision="increase",
            review_reference="Beyond reviewed ceiling",
            daily_limit=41,
        ),
    )
    assert over.status_code == 409


def test_expired_callback_proof_blocks_final_authorization(workspace, app, carrier, clock):
    task = connected_callback(workspace, carrier)
    c, h, identifier = workspace["client"], workspace["headers"], task["number_id"]
    result = c.patch(
        f"/v1/admin/numbers/{identifier}",
        headers=h,
        json=number_edit(workspace, identifier, callback_task_id=task["id"]),
    )
    assert result.status_code == 200, result.text
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        number = service.required(s, "number_inventory", identifier, p)
        proof = dict(number.data["callback_proof"], expires_at=clock.now.isoformat())
        update(number, lifecycle="limited_real_use", callback_proof=proof)
        lead = service.scoped(s, "leads", p)[0]
        assert service.context(s, p, lead, number).number_healthy is False


def test_health_policy_is_validated_and_exposed_from_one_owner(workspace):
    c, h = workspace["client"], workspace["headers"]
    changed = {
        "min_comparable": 40,
        "material_drop": 0.15,
        "confidence": 0.95,
        "window_days": 60,
        "callback_proof_days": 30,
    }
    response = c.put("/v1/admin/optimization", headers=h, json={"health_policy": changed})
    assert response.status_code == 200, response.text
    assert response.json()["health_policy"] == changed
    assert (
        c.put("/v1/admin/optimization", headers=h, json={"health_policy": {"min_comparable": 0}}).status_code
        == 422
    )
