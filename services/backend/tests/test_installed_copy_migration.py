"""E11/E14: additive legacy-schema contract and installed acceptance receipt validation."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from app.db import get, put
from app.migrate import migrate_database
from conftest import start
from sqlalchemy import inspect, text
from test_remediation_privacy import RAW, persisted_content, principal


def test_sanitation_commits_bounded_pages_and_resumes(workspace, app, monkeypatch):
    import app.privacy as privacy
    from app.db import MODELS
    from sqlalchemy import select

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(603):
            put(
                s,
                "transcript_segments",
                p["tenant_id"],
                p["seller_id"],
                {"segment_id": str(i), "text": RAW},
                object_id=f"legacy-{i:04}",
            )
    original = privacy.persistent_document
    seen = []

    def crash(kind, data, **kw):
        if kind == "transcript_segments":
            seen.append(data.get("segment_id"))
            if len(seen) == 501:
                raise OSError("Synthetic interrupted migration")
        return original(kind, data, **kw)

    monkeypatch.setattr(privacy, "persistent_document", crash)
    with pytest.raises(OSError):
        service.purge_legacy_raw_content()
    with service.db.transaction(False) as s:
        records = list(s.scalars(select(MODELS["transcript_segments"])))
        assert sum("text" not in r.data for r in records) == 500
    seen.clear()
    monkeypatch.setattr(privacy, "persistent_document", original)
    service.privacy_cutover()
    with service.db.transaction(False) as s:
        assert all("text" not in r.data for r in s.scalars(select(MODELS["transcript_segments"])))


def test_legacy_authorization_journal_drops_raw_and_keeps_recovery_identity(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    entry = {
        "call_id": call["id"],
        "tenant_id": p["tenant_id"],
        "seller_id": p["seller_id"],
        "user_id": p["user_id"],
        "at": service.clock().isoformat(),
        "provider_id": call["provider_id"],
        "command_id": call["id"],
        "command_hash": "f" * 64,
        "intent": {**call, "legacy_model_extension": RAW, "recording_url": "https://example.test/raw"},
        "unknown_model_body": RAW,
    }
    path = service.settings.data_dir / "authorization-journal.enc"
    path.write_bytes(service.vault.cipher.encrypt(json.dumps(entry).encode()) + b"\n")
    service.purge_legacy_raw_content()
    decoded = [json.loads(service.vault.cipher.decrypt(line)) for line in path.read_bytes().splitlines()]
    assert RAW not in str(decoded) and "recording_url" not in str(decoded)
    assert decoded[0]["provider_id"] == call["provider_id"]
    assert service._restore_journal(p)[0][call["id"]]["provider_id"] == call["provider_id"]


def test_legacy_copy_allowlist_continuity(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    human = "Keep the rep-authored relationship correction."
    with service.db.transaction() as s:
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call["id"], "human_text": human, "notes": {"summary": RAW, "transcript_version": 0}},
            key=call["id"],
        )
        identity = note.id
        put(
            s,
            "transcript_segments",
            p["tenant_id"],
            p["seller_id"],
            {"call_id": call["id"], "segment_id": "legacy", "text": RAW, "revision": 1, "is_final": True},
            key="legacy",
        )
    backup = service.backup(p)
    path = service.settings.data_dir / "backups" / (backup["backup_id"] + ".backup")
    decoded = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    assert RAW not in str(decoded)
    assert human in str(decoded)
    assert not {"metric_contributions", "metric_buckets"} & decoded["tables"].keys()
    service.purge_legacy_raw_content()
    service.purge_legacy_raw_content()
    service.db.backfill_operational_columns()
    migrate_database(service.db.engine.url.render_as_string(hide_password=False))
    assert RAW not in persisted_content(service, p)
    with service.db.transaction(False) as s:
        assert (
            hashlib.sha256(get(s, "notes", identity).data["human_text"].encode()).digest()
            == hashlib.sha256(human.encode()).digest()
        )
        assert (
            s.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0017_metric_projections"
        )
        assert {"ctx_key", "ctx_status", "ctx_index_version"} <= {
            c["name"] for c in inspect(s.connection()).get_columns("outcomes")
        }


@pytest.mark.parametrize("platform", ["macos-aarch64", "windows-x86_64"])
def test_actual_update_allowed_data(platform):
    """Executed against E14 receipts; never substitute a fresh install for an update."""
    location = os.getenv("OPENDIALER_NATIVE_COMPLETION_RECEIPTS")
    if not location:
        pytest.skip("E14 installed-update receipt pending; this does not accept the native gate")
    receipt = json.loads((Path(location) / f"{platform}.json").read_text())
    assert receipt["installation_method"] == "signed_in_place_update"
    assert receipt["previous_version"] == "0.2.1" and receipt["installed_version"] == "0.3.0"
    assert receipt["updater_exited"] is True
    assert receipt["installed_backend_sha256"] == receipt["verified_package_backend_sha256"]
    assert receipt["before"]["allowed_identity_sha256"] == receipt["after"]["allowed_identity_sha256"]
    assert receipt["before"]["human_notes_sha256"] == receipt["after"]["human_notes_sha256"]
    assert receipt["after"]["integrity"] == "ok" and receipt["after"]["legacy_raw_found"] is False
    assert receipt["session_restored"] is True
    assert {"local", "connected_postgresql"} <= receipt["native_journeys"].keys()
    assert all(row["passed"] for row in receipt["native_journeys"].values())
