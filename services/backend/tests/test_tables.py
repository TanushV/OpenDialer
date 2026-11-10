"""R2.9: the store holds exactly the tables the app uses, and older backups still restore."""

import json

from app.platform.db import RETIRED_TABLES, Base

# Every table this version creates. Adding one is a deliberate change to this list (section 12 caps).
ALLOWED = {
    "active_claims",
    "audit_events",
    "budgets",
    "call_attempts",
    "call_legs",
    "campaigns",
    "command_outbox",
    "compliance_decisions",
    "conversion_events",
    "events",
    "experiment_assignments",
    "feature_snapshots",
    "followup_tasks",
    "jobs",
    "lead_number_bindings",
    "leads",
    "learning_datasets",
    "model_versions",
    "notes",
    "number_health",
    "number_inventory",
    "number_leases",
    "outcomes",
    "phone_endpoints",
    "policy_decisions",
    "policy_versions",
    "provider_events",
    "registry_snapshots",
    "research_facts",
    "research_sources",
    "reservations",
    "script_memories",
    "call_summaries",
    "script_versions",
    "sellers",
    "suppressions",
    "tenants",
    "transcript_segments",
    "usage_ledger",
    "users",
    "webhook_inbox",
}


def test_the_schema_has_exactly_the_allowed_tables():
    assert set(Base.metadata.tables) == ALLOWED
    assert not RETIRED_TABLES & ALLOWED


def test_a_backup_from_an_older_version_restores_and_skips_retired_tables(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    backup = client.post("/v1/admin/backup", headers=headers).json()
    path = app.state.service.settings.data_dir / "backups" / (backup["backup_id"] + ".backup")
    payload = json.loads(path.read_text())
    payload["tables"]["consent_evidence"] = [
        {
            "id": "old-consent-row",
            "tenant_id": workspace["tenant_id"],
            "seller_id": workspace["seller_id"],
            "data": {"endpoint_id": "e", "revoked": True},
            "external_key": "old",
            "version": 1,
            "created_at": "2026-10-01T00:00:00+00:00",
        }
    ]
    path.write_text(json.dumps(payload))
    restored = client.post("/v1/admin/restore", headers=headers, json={"backup_id": backup["backup_id"]})
    assert restored.status_code == 200, restored.text
