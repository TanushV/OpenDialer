"""A22/A24/A29/A50: editable current business knowledge, not a hidden phrasebook."""

from datetime import timedelta

import pytest
from app.coaching.context import ground_guidance
from app.db import MODELS, doc, get, put
from sqlalchemy import inspect, text
from test_context_reads import count_decrypted
from test_remediation_coach import advice
from test_remediation_privacy import principal


def body(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        old = doc(service.approved_playbook(s, p))
        seller = get(s, "sellers", p["seller_id"]).data["name"]
    return {
        "name": "Current business knowledge",
        "opener": f"Hi, this is {{rep}} from {seller}.",
        "screening_intro": f"{{rep}} from {seller}, calling about business needs.",
        "cards": [],
        "approved": True,
        "reason": "Reviewed product details",
        "product_description": "We provide workflow training for service businesses.",
        "claims": ["Our product includes three guided onboarding sessions."],
        "prohibited_claims": ["Do not promise a particular revenue increase."],
        "expected_previous_id": old["id"],
        "expected_previous_version": old["version"],
    }


def publish(workspace, data):
    return workspace["client"].post("/v1/admin/playbooks", headers=workspace["headers"], json=data)


def test_published_product_knowledge_round_trips_as_the_current_workspace_context(workspace, app):
    data = body(workspace, app)
    saved = publish(workspace, data)
    assert saved.status_code == 200, saved.text
    for field in ("product_description", "claims", "prohibited_claims"):
        assert saved.json().get(field) == data[field]
    snapshot = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()
    assert snapshot.get("active_playbook", {}).get("id") == saved.json()["id"]
    assert len(snapshot["playbooks"]) == 2, "Retain original reviewed versions"


def test_product_knowledge_cannot_overwrite_a_newer_review_with_a_stale_form(workspace, app):
    data = body(workspace, app)
    assert publish(workspace, data).status_code == 200
    second = publish(workspace, {**data, "product_description": "Older review from another tab"})
    assert second.status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"product_description": "x" * 12001},
        {"claims": ["x" * 1201]},
        {"prohibited_claims": "not-a-list"},
        {"claims": ["   "]},
        {"cards": [{"id": "same", "text": "One"}, {"id": "same", "text": "Two"}]},
    ],
)
def test_knowledge_has_explicit_bounded_validated_inputs(workspace, app, changes):
    assert publish(workspace, {**body(workspace, app), **changes}).status_code == 422


def test_factual_grounding_can_use_current_product_description():
    request = {
        "state_version": 0,
        "private_question": "What is included?",
        "seller": {"knowledge": "Our product includes 3 guided onboarding sessions."},
    }
    card = advice(0, text="Our product includes 3 guided onboarding sessions.")
    card["evidence_segment_ids"] = []
    card["reason"] = "This is included in the approved product description."
    assert ground_guidance(card, request)


def test_current_knowledge_does_not_decrypt_retired_playbooks(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        active = service.approved_playbook(s, p).id
        for i in range(400):
            row = put(
                s,
                "playbooks",
                p["tenant_id"],
                p["seller_id"],
                {
                    "approved": False,
                    "opener": "Retired language",
                    "reason": "Withdrawn",
                },
                now=clock.now - timedelta(days=1),
            )
            row.version = i + 10
    decoded = count_decrypted(app, monkeypatch)
    with service.db.transaction(False) as s:
        assert service.approved_playbook(s, p).id == active
    assert len(decoded) <= 2, len(decoded)


def test_knowledge_index_backfill_preserves_source_data_and_revision(workspace, app):
    service, p = app.state.service, principal(workspace)
    columns = {item["name"] for item in inspect(service.db.engine).get_columns("playbooks")}
    assert {"ctx_key", "ctx_index_version"} <= columns
    with service.db.transaction() as s:
        original = service.approved_playbook(s, p)
        saved = doc(original)
        s.execute(
            text("UPDATE playbooks SET ctx_key=NULL,ctx_index_version=NULL WHERE id=:id"), {"id": original.id}
        )
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        row = get(s, "playbooks", saved["id"])
        assert doc(row) == saved and row.ctx_key == "approved"
    assert "ix_playbooks_current" in {i["name"] for i in inspect(service.db.engine).get_indexes("playbooks")}
    assert hasattr(MODELS["playbooks"], "ctx_key")
