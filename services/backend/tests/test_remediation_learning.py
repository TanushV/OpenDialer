"""A31-A35/A47-A49: sparse learning, chronology and accounting contracts."""

from datetime import timedelta

import pytest
from app.providers.inference import InferenceResult
from conftest import start


def test_conversation_associations_require_support_and_keep_correlation_label(clock):
    from app.optimization.learning import conversation_associations

    rows = [
        {
            "lead_id": str(i),
            "at": (clock.now - timedelta(days=20)).isoformat(),
            "label_available_at": (clock.now - timedelta(days=1)).isoformat(),
            "conversion": int(i % 2 == 0),
            "observations": [{"concept": "Procurement review", "approach": "Clarify approval steps"}],
        }
        for i in range(30)
    ]
    rows.append(
        {
            "lead_id": "rare",
            "at": (clock.now - timedelta(days=20)).isoformat(),
            "label_available_at": clock.now.isoformat(),
            "conversion": 1,
            "observations": [{"concept": "Unicorn budget", "approach": "Offer magic"}],
        }
    )
    report = conversation_associations(rows, as_of=clock.now, min_support=10)
    assert report["evidence_type"] == "observational_correlation"
    assert not any(x["concept"] == "Unicorn budget" for x in report["patterns"])
    pattern = next(x for x in report["patterns"] if x["concept"] == "Procurement review")
    assert pattern["samples"] == 30
    assert 0 <= pattern["conversion_low"] < pattern["conversion_high"] <= 1
    assert pattern["automatic_policy_change"] is False


def test_future_labels_and_feedback_clicks_cannot_create_conversion_associations(clock):
    from app.optimization.learning import conversation_associations

    rows = [
        {
            "lead_id": str(i),
            "at": clock.now.isoformat(),
            "label_available_at": (clock.now + timedelta(days=1)).isoformat(),
            "conversion": 1,
            "useful": True,
            "observations": [{"concept": "Need", "approach": "Ask"}],
        }
        for i in range(30)
    ]
    assert conversation_associations(rows, as_of=clock.now, min_support=10)["patterns"] == []
    for row in rows:
        row["label_available_at"] = clock.now.isoformat()
        row["conversion"] = None
    assert conversation_associations(rows, as_of=clock.now, min_support=10)["patterns"] == []


def test_short_call_health_uses_answer_to_end_not_ringing(clock):
    from app.optimization.numbering import number_health

    rows = [{"outcome": "right_party", "duration_seconds": 60, "connected_seconds": 4} for _ in range(25)]
    result = number_health(rows, pooled_attempts=rows, now=clock.now)
    assert result["short_connected_calls"] == 25


def test_two_distinct_provider_attempts_settle_only_their_matching_holds(workspace, app):
    service = app.state.service
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    for key in ["first", "second"]:
        assert service.reserve_optional_budget(p, key, 0.01, object_id="same-call", work_kind="live_coach")
    service.usage(
        p,
        "openrouter",
        "live_coach",
        InferenceResult({}, {"cost": 0.001}, "provider-first"),
        object_id="same-call",
        reserved=0,
        request_key="first",
    )
    service.usage(
        p,
        "openrouter",
        "live_coach",
        InferenceResult({}, {"cost": 0.001}, "provider-first"),
        object_id="same-call",
        reserved=0,
        request_key="first",
    )
    with service.db.transaction(False) as s:
        entries = [r.data for r in service.scoped(s, "usage_ledger", p)]
    assert sum(float(r.get("cost_usd") or 0) for r in entries) == pytest.approx(0.001)
    assert sum(float(r.get("reserved_usd") or 0) for r in entries) == pytest.approx(0.01)
    assert any(r.get("request_key") == "second" and r.get("status") == "reserved" for r in entries)


def test_goal_change_preserves_booked_hold_and_optout(workspace, app):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/calls/" + call["id"] + "/hangup", headers=h)
    c.post("/v1/calls/" + call["id"] + "/disposition", headers=h, json={"outcome": "meeting_booked"})
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    lead = next(r for r in c.get("/v1/workspace", headers=h).json()["leads"] if r["id"] == call["lead_id"])
    assert lead["outreach_hold"] == "meeting_booked"


def test_batch_prediction_verifies_artifact_only_on_load_and_preserves_rows(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    import joblib
    import numpy as np
    from app.ml import training
    from sklearn.dummy import DummyClassifier

    root = tmp_path / "models"
    root.mkdir()
    path = root / "fixture.joblib"
    estimator = DummyClassifier(strategy="prior").fit(np.array([[0], [1]]), [0, 1])
    joblib.dump({"human": estimator}, path)
    model = {
        "artifact": str(path),
        "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_order": ["local_hour"],
    }
    original = Path.read_bytes
    reads = []

    def read(p):
        if p == path:
            reads.append(str(p))
        return original(p)

    monkeypatch.setattr(Path, "read_bytes", read)
    batch = [{"local_hour": i} for i in range(10)]
    training._MODELS.clear()
    for _ in range(2):
        result = training.predict_funnel_batch(model, batch, root)
        assert len(result) == 10
        assert all(r["p_human"] == pytest.approx(0.5) for r in result)
    assert len(reads) == 1
