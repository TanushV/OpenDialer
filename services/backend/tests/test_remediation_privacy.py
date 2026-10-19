"""A04/A07/A27/A41-A44/A54: hard ephemeral-content boundary, authored first."""

import json
from datetime import timedelta

import pytest
from app.db import MODELS, Event, Job, put, update
from conftest import disposition_body, start
from sqlalchemy import select

RAW = "Private raw utterance ALPHA-ZULU-EPHEMERAL: our internal project is called Granite."


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


@pytest.mark.parametrize("seconds,visible", [(299, True), (300, False), (301, False)])
def test_all_boundaries_original_ttl(workspace, app, clock, seconds, visible):
    """C044: exact expiry, revision and repeated views share the original deadline."""
    call = start(workspace)
    post_turn(workspace, call)
    began = clock.now
    clock.now = began + timedelta(seconds=290)
    post_turn(workspace, call, revision=2, text=RAW + " correction")
    clock.now = began + timedelta(seconds=seconds)
    c, h = workspace["client"], workspace["headers"]
    view = c.get(f"/v1/calls/{call['id']}/analysis", headers=h)
    assert (RAW in view.text) is visible
    assert RAW not in persisted_content(app.state.service, principal(workspace))
    backup = app.state.service.backup(principal(workspace))
    path = app.state.service.settings.data_dir / "backups" / (backup["backup_id"] + ".backup")
    assert RAW.encode() not in app.state.service.vault.cipher.decrypt(path.read_bytes())
    if not visible:
        # Wall clock rollback cannot revive an item already expired by a read.
        clock.now -= timedelta(seconds=200)
        assert RAW not in c.get(f"/v1/calls/{call['id']}/analysis", headers=h).text
        assert RAW not in c.get("/v1/events", headers=h).text


def post_turn(w, call, **changes):
    result = w["client"].post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=w["headers"],
        json={
            "segment_id": "privacy-turn",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": RAW,
            **changes,
        },
    )
    assert result.status_code == 200, result.text
    return result


def persisted_content(service, p):
    with service.db.transaction(False) as session:
        data = [r.data for name in MODELS for r in service.scoped(session, name, p)]
        data += [r.payload for r in session.scalars(select(Event).where(Event.tenant_id == p["tenant_id"]))]
        data += [r.payload for r in session.scalars(select(Job).where(Job.tenant_id == p["tenant_id"]))]
    return json.dumps(data)


def test_raw_content_never_enters_permanent_database(workspace, app):
    call = start(workspace)
    post_turn(workspace, call)
    assert RAW not in persisted_content(app.state.service, principal(workspace))
    assert RAW in json.dumps(
        workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()["transcript"]
    )


def test_transcript_expires_during_an_active_long_call(workspace, clock):
    call = start(workspace)
    post_turn(workspace, call)
    clock.now += timedelta(seconds=301)
    result = workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()
    assert result["transcript"] == []
    assert result["raw_evidence_status"] == "expired"
    assert result["state"] not in {"ENDED", "FINALIZED"}


def test_revision_and_repeated_reads_cannot_extend_original_ttl(workspace, clock):
    call = start(workspace)
    post_turn(workspace, call)
    clock.now += timedelta(seconds=290)
    post_turn(workspace, call, revision=2, text=RAW + " corrected")
    clock.now += timedelta(seconds=11)
    response = workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"])
    assert response.json()["transcript"] == []


def test_expired_evidence_is_not_reintroduced_by_analysis_or_event_replay(workspace, app, clock):
    call = start(workspace)
    post_turn(workspace, call)
    c, h = workspace["client"], workspace["headers"]
    c.get("/v1/calls/" + call["id"] + "/analysis", headers=h)
    clock.now += timedelta(seconds=301)
    for route in ["/v1/events", "/v1/workspace", "/v1/calls/" + call["id"] + "/analysis"]:
        response = c.get(route, headers=h)
        assert response.status_code == 200, response.text
        assert RAW not in response.text, route
    assert RAW not in persisted_content(app.state.service, principal(workspace))


def test_restart_cannot_reload_expired_or_previous_raw_content(workspace, app):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    call = start(workspace)
    post_turn(workspace, call)
    settings = Settings(**app.state.service.settings.model_dump())
    restored = create_app(settings, clock=app.state.service.clock)
    with TestClient(restored) as client:
        result = client.get("/v1/calls/" + call["id"], headers=workspace["headers"])
        assert result.status_code == 200
        assert result.json()["transcript"] == []
        assert result.json()["raw_evidence_status"] in {"unavailable_after_restart", "expired"}


def test_recording_remains_disabled_even_when_legacy_permission_exists(workspace, app, carrier):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        policy = service.required(session, "policy_versions", workspace["policy_id"], p)
        update(policy, permissions=["live", "transcription", "recording"])
    service.event(p, "privacy-answer", "call.answered", call["id"])
    workspace["client"].post("/v1/admin/control/run", headers=workspace["headers"])
    assert not any(a["action"] == "record_start" for a in carrier.actions)


def test_legacy_recording_url_is_never_exposed_for_playback(workspace, app):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        update(
            service.required(session, "call_attempts", call["id"], p),
            recording_url="https://example.test/private.mp3",
        )
    response = workspace["client"].get("/v1/calls/" + call["id"] + "/recording", headers=workspace["headers"])
    assert response.status_code in {403, 410}
    assert "private.mp3" not in response.text


def test_raw_expiry_preserves_human_notes_outcome_and_optout(workspace, app, clock):
    call = start(workspace)
    post_turn(workspace, call)
    c, h = workspace["client"], workspace["headers"]
    analysis = c.get("/v1/calls/" + call["id"] + "/analysis", headers=h).json()
    response = c.patch(
        "/v1/calls/" + call["id"] + "/notes",
        headers=h,
        json={
            "expected_version": analysis["notes_version"],
            "text": "Rep note: follow up after their review.",
        },
    )
    assert response.status_code == 200, response.text
    c.post("/v1/calls/" + call["id"] + "/hangup", headers=h)
    c.post(
        "/v1/calls/" + call["id"] + "/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], **{"outcome": "opt_out"}),
    )
    clock.now += timedelta(seconds=301)
    review = c.get("/v1/calls/" + call["id"] + "/analysis", headers=h).json()
    assert "Rep note: follow up" in json.dumps(review)
    assert RAW not in json.dumps(review)
    lead = next(x for x in c.get("/v1/workspace", headers=h).json()["leads"] if x["id"] == call["lead_id"])
    assert lead["suppressed"]


@pytest.mark.parametrize("text_bytes,max_bytes", [(100, 200), (1000, 200)])
def test_ephemeral_buffer_is_bounded_and_source_revision_does_not_reset_expiry(clock, text_bytes, max_bytes):
    from app.privacy import EphemeralBuffer

    buffer = EphemeralBuffer(clock=clock, ttl_seconds=300, max_bytes=max_bytes)
    buffer.put("seller:call:s", {"text": "x" * text_bytes, "revision": 1})
    assert buffer.bytes_used <= max_bytes
    clock.now += timedelta(seconds=290)
    buffer.put("seller:call:s", {"text": "y" * min(text_bytes, 50), "revision": 2})
    clock.now += timedelta(seconds=11)
    assert buffer.get("seller:call:s") is None


def test_retention_cannot_be_disabled_or_extended_by_settings(workspace):
    c, h = workspace["client"], workspace["headers"]
    for changes in [
        {"raw_retention_seconds": 86400},
        {"recording_enabled": True},
        {"subscriptions_allowed": True},
    ]:
        response = c.put("/v1/admin/operating-policy", headers=h, json=changes)
        assert response.status_code == 422, response.text


def test_backup_excludes_raw_content_but_keeps_all_allowed_ids(workspace, app):
    call = start(workspace)
    post_turn(workspace, call)
    service = app.state.service
    receipt = service.backup(principal(workspace))
    path = service.settings.data_dir / "backups" / (receipt["backup_id"] + ".backup")
    payload = service.vault.cipher.decrypt(path.read_bytes()).decode()
    assert RAW not in payload
    assert call["id"] in payload and workspace["policy_id"] in payload


def test_legacy_raw_cleanup_is_idempotent_and_preserves_human_notes(workspace, app):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        put(
            s,
            "transcript_segments",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call["id"], "segment_id": "legacy", "text": RAW, "revision": 1, "is_final": True},
            key="legacy",
        )
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "human_text": "Keep my correction",
                "notes": {"summary": RAW, "transcript_version": 0},
            },
            key=call["id"],
        )
        note_id = note.id
    service.purge_legacy_raw_content()
    service.purge_legacy_raw_content()
    assert RAW not in persisted_content(service, p)
    with service.db.transaction(False) as s:
        note = service.required(s, "notes", note_id, p)
        assert note.data["human_text"] == "Keep my correction"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["record_start", "transcription_start"])
async def test_legacy_pending_raw_capture_cannot_bypass_privacy_preflight(
    workspace, app, carrier, action, monkeypatch
):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(
            service.required(s, "policy_versions", workspace["policy_id"], p),
            permissions=["live", "transcription", "recording"],
        )
        put(
            s,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "action",
                "call_id": call["id"],
                "action": action,
                "status": "pending",
                "payload": {"command_id": "legacy-raw-capture"},
            },
            key="legacy-raw-capture",
        )
    service.settings.simulation = False
    from types import SimpleNamespace

    monkeypatch.setattr("app.telephony.workflows.authorize", lambda *args: SimpleNamespace(allowed=True))
    await service.dispatch_controls(p)
    assert not any(item["action"] == action for item in carrier.actions)
    with service.db.transaction(False) as s:
        command = next(
            r for r in service.scoped(s, "command_outbox", p) if r.external_key == "legacy-raw-capture"
        )
        assert command.data["status"] == "cancelled"
        assert "PRIVACY" in command.data["reason"]


def test_restore_sanitizes_legacy_backup_even_after_cutover(workspace, app):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    path = service.settings.data_dir / "backups" / (backup["backup_id"] + ".backup")
    payload = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    payload["tables"]["call_attempts"][0]["data"]["recording_url"] = RAW
    payload["tables"]["transcript_segments"].append(
        {
            "id": "legacy-raw-id",
            "tenant_id": p["tenant_id"],
            "seller_id": p["seller_id"],
            "external_key": call["id"] + ":legacy",
            "version": 1,
            "data": {"call_id": call["id"], "segment_id": "legacy", "revision": 1, "text": RAW},
        }
    )
    path.write_bytes(service.vault.cipher.encrypt(json.dumps(payload).encode()))
    service.restore(p, backup["backup_id"])
    assert RAW not in persisted_content(service, p)
    with service.db.transaction(False) as s:
        assert service.required(s, "transcript_segments", "legacy-raw-id", p).data["segment_id"] == "legacy"


def test_noisy_call_cannot_evict_another_calls_entire_raw_context(clock):
    from app.privacy import EphemeralBuffer

    buffer = EphemeralBuffer(clock=clock, max_bytes=1000, per_call_bytes=300, per_call_entries=2)
    buffer.put("b:one", {"text": "b" * 100}, scope="b")
    for i in range(10):
        buffer.put("a:" + str(i), {"text": "a" * 100}, scope="a")
    assert buffer.get("b:one") is not None
    assert sum(buffer.get("a:" + str(i)) is not None for i in range(10)) <= 2
