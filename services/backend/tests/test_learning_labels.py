"""R8.1 and R8.2: test calls never become training rows."""

from datetime import UTC, datetime, timedelta

from app.optimization.learning import training_rows

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def call(call_id, **extra):
    started = NOW - timedelta(days=30)
    return {
        "id": call_id,
        "lead_id": "lead-" + call_id,
        "started_at": started.isoformat(),
        "submitted_at": started.isoformat(),
        "feature_snapshot_id": "snap-" + call_id,
        "outcome": "not_interested",
        "outcome_available_at": (started + timedelta(minutes=5)).isoformat(),
        "outcome_source": "rep_reported",
        **extra,
    }


def snapshot(call_id):
    return {"id": "snap-" + call_id, "as_of": (NOW - timedelta(days=31)).isoformat(), "features": {}}


def test_test_calls_and_delivery_tests_never_become_training_rows():
    calls = [
        call("real"),
        call("owner-line", is_test=True),
        call("delivery", disposition_input={"delivery_test": True}),
    ]
    rows = training_rows(
        calls, [snapshot(c["id"]) for c in calls], [], now=NOW, goal="meeting_held", window_days=14
    )
    assert [r["call_id"] for r in rows] == ["real"]


def test_a_booked_meeting_is_labeled_at_wrap_up_and_a_correction_retracts_it(workspace):
    """R8.1: the meeting_booked goal is known when the rep saves the outcome."""
    from app.platform.db import MODELS
    from conftest import disposition_body, start

    client, headers = workspace["client"], workspace["headers"]
    call = start(workspace, key="booked-label")
    for name in ("call.answered", "call.hangup"):
        client.post(
            "/v1/simulation/event",
            headers=headers,
            json={"event_id": f"bl-{name}", "event_type": name, "call_id": call["id"], "payload": {}},
        )

    def labels():
        with client.app.state.service.db.transaction(False) as s:
            rows = s.query(MODELS["conversion_events"]).all()
            return [(r.data["kind"], r.data["retracted"]) for r in rows if r.data["kind"] == "meeting_booked"]

    body = disposition_body(client, headers, call["id"], outcome="meeting_booked")
    assert client.post(f"/v1/calls/{call['id']}/disposition", headers=headers, json=body).status_code == 200
    assert labels() == [("meeting_booked", False)]
    body = disposition_body(client, headers, call["id"], outcome="not_interested")
    body["expected_edit_version"] = 1
    assert client.post(f"/v1/calls/{call['id']}/disposition", headers=headers, json=body).status_code == 200
    assert labels() == [("meeting_booked", True)]


def test_lead_features_read_seniority_size_source_and_trigger():
    """R8.3: who the lead is, where the number came from and why now."""
    from app.optimization.learning import lead_features, size_bucket, title_seniority

    assert [
        title_seniority(t)
        for t in ("Chief Revenue Officer", "VP Sales", "Sales Director", "Account Manager", "AE", "")
    ] == [
        "c_level",
        "vp",
        "director",
        "manager",
        "individual",
        "unknown",
    ]
    assert [size_bucket(v) for v in ("8", "51-200", "1,200 employees", "")] == [
        "1-10",
        "51-200",
        "1000+",
        "unknown",
    ]
    lead = {
        "row": {
            "Title": "VP of Sales",
            "Industry": "Software",
            "Employees": "120",
            "Number source": "LeadOcean",
        },
        "list_name": "Agencies",
        "trigger": {"type": "hiring_sdrs", "strength": "A", "date": (NOW - timedelta(days=10)).isoformat()},
    }
    features = lead_features(lead, NOW)
    assert {
        k: features[k] for k in ("title_seniority", "industry", "company_size", "number_source", "list")
    } == {
        "title_seniority": "vp",
        "industry": "software",
        "company_size": "51-200",
        "number_source": "leadocean",
        "list": "Agencies",
    }
    assert (features["trigger_type"], features["trigger_strength"], round(features["trigger_age_days"])) == (
        "hiring_sdrs",
        "A",
        10,
    )
    assert lead_features({}, NOW)["trigger_strength"] == "none"


def test_new_fields_are_one_hot_and_old_rows_read_as_unknown():
    from app.ml.training import feature_order, fitted_vocabulary, vector_v2

    rows = [{"features": {"title_seniority": "vp", "segment": "general"}}] * 3
    vocabulary = fitted_vocabulary(rows)
    assert vocabulary["categories"]["title_seniority"] == ["vp"]
    order = feature_order(vocabulary, conversation=False)
    assert "cat:title_seniority=vp" in order and "cat:title_seniority=unknown" in order
    new = dict(zip(order, vector_v2({"title_seniority": "vp"}, order, vocabulary), strict=True))
    old = dict(zip(order, vector_v2({"feature_schema_version": 2}, order, vocabulary), strict=True))
    assert new["cat:title_seniority=vp"] == 1.0 and new["cat:title_seniority=unknown"] == 0.0
    assert old["cat:title_seniority=unknown"] == 1.0 and old["cat:industry=unknown"] == 1.0
