"""E09/C035: operational reports use replaceable source contributions."""

from datetime import timedelta

import pytest
from app.db import MODELS, doc, get, put, update
from conftest import completed_report
from test_remediation_privacy import principal


@pytest.mark.asyncio
async def test_contribution_replay_oracle(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    # Independent set arithmetic: three imported leads, duplicate evidence for one.
    ids = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"]
    events = []
    for lead in ids[:2]:
        with service.db.transaction() as s:
            for i in range(2):
                row = put(
                    s,
                    "conversion_events",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "lead_id": lead["id"],
                        "kind": "meeting_held",
                        "occurred_at": clock.now.isoformat(),
                        "observed_at": clock.now.isoformat(),
                        "label_available_at": clock.now.isoformat(),
                        "status": "confirmed",
                        "retracted": False,
                    },
                    key=f"oracle:{lead['id']}:{i}",
                    now=clock.now,
                )
                events.append(doc(row))
    first = completed_report(workspace).json()
    expected = len({event["lead_id"] for event in events if not event["retracted"]})
    assert first["metrics"]["unique_conversions"] == expected == 2
    with service.db.transaction(False) as session:
        contributions = [
            r for r in service.scoped(session, "metric_contributions", p) if r.data["source_kind"] == "lead"
        ]
        assert len(contributions) == 3, "Every lead needs one current source contribution"
        assert sum(r.data["values"]["unique_conversions"] for r in contributions) == 2
    with service.db.transaction() as s:
        for event in events:
            if event["lead_id"] == ids[0]["id"]:
                update(
                    get(s, "conversion_events", event["id"]),
                    retracted=True,
                    retracted_at=clock.now.isoformat(),
                )
                event["retracted"] = True
    second = completed_report(workspace).json()
    assert (
        second["metrics"]["unique_conversions"]
        == len({e["lead_id"] for e in events if not e["retracted"]})
        == 1
    )
    with service.db.transaction() as s:
        for event in events:
            if event["lead_id"] == ids[1]["id"]:
                s.delete(get(s, "conversion_events", event["id"]))
    third = completed_report(workspace).json()
    assert third["metrics"]["unique_conversions"] == 0
    assert {"metric_contributions", "metric_buckets"} <= MODELS.keys(), (
        "A saved whole report is not an incremental contribution ledger"
    )
    with service.db.transaction(False) as s:
        before = [(r.id, r.version, r.data) for r in service.scoped(s, "metric_contributions", p)]
    await service.run_jobs(p["tenant_id"], queues=["ml"])
    with service.db.transaction(False) as s:
        assert [(r.id, r.version, r.data) for r in service.scoped(s, "metric_contributions", p)] == before


@pytest.mark.asyncio
async def test_normal_correction_does_not_rebuild_entire_operational_dataset(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    completed_report(workspace)
    rebuilds = []
    original = service.operational_dataset

    def counted(*args, **kwargs):
        rebuilds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "operational_dataset", counted)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        update(lead, next_attempt_at=(clock.now + timedelta(hours=1)).isoformat())
    completed_report(workspace)
    assert not rebuilds, "Changing one due time cannot reconstruct historical training/report rows"


def test_lead_and_call_vectors_keep_distinct_units_and_asof_evidence(clock):
    from app.optimization.projections import call_values, lead_values

    now = clock.now
    lead = {"id": "l1", "status": "eligible", "attempt_count": 2, "originally_eligible": True}
    calls = [
        {
            "id": f"c{i}",
            "lead_id": "l1",
            "started_at": (now - timedelta(days=2)).isoformat(),
            "submitted_at": (now - timedelta(days=2)).isoformat(),
            "outcome": "right_party",
            "outcome_available_at": (now - timedelta(days=2)).isoformat(),
        }
        for i in range(2)
    ]
    events = [
        {
            "id": "e",
            "lead_id": "l1",
            "kind": "meeting_held",
            "status": "confirmed",
            "occurred_at": (now - timedelta(days=1)).isoformat(),
            "label_available_at": now.isoformat(),
        }
    ]
    config = {"conversion_goal": "meeting_held", "window_days": 14}
    values = lead_values(lead, calls, events, suppressed=False, config=config, now=now)
    assert values["original_cohort"] == values["right_party_unique"] == values["unique_conversions"] == 1
    assert "attempts" not in values, "Attempt counts belong only to call units"
    assert sum(call_values(c, config=config, now=now)["attempts"] for c in calls) == 2
    events[0]["history"] = [dict(events[0])]
    events[0].update(
        retracted=True,
        retracted_at=(now + timedelta(hours=1)).isoformat(),
        label_available_at=(now + timedelta(hours=1)).isoformat(),
    )
    assert (
        lead_values(lead, calls, events, suppressed=False, config=config, now=now)["unique_conversions"] == 1
    )
    assert (
        lead_values(lead, calls, events, suppressed=False, config=config, now=now + timedelta(hours=1))[
            "unique_conversions"
        ]
        == 0
    )
    hidden = lead_values(
        {**lead, "originally_eligible": False}, calls, events, suppressed=True, config=config, now=now
    )
    assert hidden["original_cohort"] == hidden["suppressed"] == 1
    assert hidden["right_party_unique"] == hidden["unique_conversions"] == hidden["eligible_now"] == 0


@pytest.mark.parametrize(
    "outcome,role,classified,human,right_n,right_s",
    [
        ("no_answer", None, 1, 0, 0, 0),
        ("temporary_failure", None, 0, 0, 0, 0),
        ("not_interested", None, 1, 1, 0, 0),
        ("wrong_party", None, 1, 1, 1, 0),
        ("right_party", "right_party", 1, 1, 1, 1),
        (None, None, 0, 0, 0, 0),
    ],
)
def test_call_vector_conditioning_is_not_invented(clock, outcome, role, classified, human, right_n, right_s):
    from app.optimization.projections import call_values

    now = clock.now
    call = {
        "id": "c",
        "lead_id": "l",
        "started_at": (now - timedelta(days=14)).isoformat(),
        "submitted_at": (now - timedelta(days=14)).isoformat(),
        "outcome": outcome,
        "contact_role": role,
        "outcome_available_at": now.isoformat(),
    }
    config = {"conversion_goal": "meeting_held", "window_days": 14}
    values = call_values(call, config=config, now=now)
    assert values["human_classified_attempts"] == classified
    assert values["human_pickups"] == human
    assert values["human_unknown_attempts"] == 1 - classified
    assert values["right_party_conditioning_count"] == right_n
    assert values["right_party_successes"] == right_s
    assert values["pending_outcome_windows"] == 0
    assert call_values(call, config={**config, "window_days": 15}, now=now)["pending_outcome_windows"] == 1
    assert (
        call_values({**call, "outcome_source": "llm_provisional"}, config=config, now=now)[
            "human_classified_attempts"
        ]
        == 0
    )


def test_rep_day_contribution_splits_midnight_and_unions_devices_and_calls(clock):
    from datetime import UTC, date, datetime

    from app.optimization.projections import rep_day_values

    now = datetime(2026, 10, 15, 1, tzinfo=UTC)
    intervals = [
        {"rep_id": "r", "started_at": "2026-10-14T23:59:00+00:00", "ended_at": "2026-10-15T00:01:00+00:00"},
        {"rep_id": "r", "started_at": "2026-10-14T23:59:30+00:00", "ended_at": "2026-10-15T00:02:00+00:00"},
        {"rep_id": "other", "started_at": "2026-10-14T23:00:00+00:00", "ended_at": now.isoformat()},
    ]
    calls = [
        {"rep_id": "r", "submitted_at": "2026-10-14T23:59:45+00:00", "ended_at": "2026-10-15T00:00:45+00:00"}
    ]
    assert (
        rep_day_values(intervals, calls, rep_id="r", day=date(2026, 10, 14), now=now)["rep_work_seconds"]
        == 60
    )
    assert (
        rep_day_values(intervals, calls, rep_id="r", day=date(2026, 10, 15), now=now)["rep_work_seconds"]
        == 120
    )
    assert rep_day_values([], [], rep_id="r", day=date(2026, 10, 15), now=now)["work_evidence_count"] == 0
    # Detached playback after rep release must not increase observed rep time.
    calls[0].update(rep_released_at="2026-10-15T00:00:15+00:00", ended_at=now.isoformat(), detached=True)
    assert rep_day_values([], calls, rep_id="r", day=date(2026, 10, 15), now=now)["rep_work_seconds"] == 15


def test_usage_delta_preserves_gross_credit_and_uncertain_holds():
    from app.optimization.projections import delta_values, usage_values

    original = {"status": "uncertain", "cost_usd": None, "reserved_usd": 0.035, "billing_verified": False}
    before = usage_values(original)
    assert before["reserved_microusd"] == 35000 and before["unverified_usage_count"] == 1
    settled = {
        **original,
        "status": "settled",
        "cost_usd": 0.023,
        "reserved_usd": 0,
        "billing_verified": True,
    }
    after = usage_values(settled)
    credited = usage_values({**settled, "credited_usd": 0.011})
    assert after["gross_microusd"] == 23000 and credited["gross_microusd"] == 23000
    assert credited["credited_microusd"] == 11000 and credited["verified_microusd"] == 12000
    running = dict(before)
    for delta in (delta_values(before, after), delta_values(after, credited)):
        for key, value in delta.items():
            running[key] = running.get(key, 0) + value
    assert running == credited
    assert delta_values(credited, credited) == {}
    assert original["cost_usd"] is None
    assert usage_values({**settled, "cost_usd": 0})["unverified_usage_count"] == 0


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, "not-money", True])
def test_usage_vectors_reject_invalid_money(invalid):
    from app.optimization.projections import usage_values

    with pytest.raises(ValueError):
        usage_values({"cost_usd": invalid, "billing_verified": True})


def test_projection_tables_match_encrypted_index_contract(app):
    from sqlalchemy import inspect

    schema = inspect(app.state.db.engine)
    names = set(schema.get_table_names())
    assert {"metric_contributions", "metric_buckets"} <= names
    contribution = {column["name"]: column for column in schema.get_columns("metric_contributions")}
    bucket = {column["name"]: column for column in schema.get_columns("metric_buckets")}
    assert {
        "source_kind",
        "source_id",
        "campaign_id",
        "definition_version",
        "source_version",
        "next_refresh_at",
    } <= contribution.keys()
    assert {"campaign_id", "bucket_day", "definition_version", "bucket_kind", "group_key"} <= bucket.keys()
    assert str(contribution["source_version"]["type"]).upper() == "BIGINT"
    assert not contribution["source_kind"]["nullable"] and not bucket["group_key"]["nullable"]
    assert {"ix_metric_contributions_due", "ix_metric_contributions_source"} <= {
        index["name"] for index in schema.get_indexes("metric_contributions")
    }
    assert "ix_metric_buckets_group" in {index["name"] for index in schema.get_indexes("metric_buckets")}


def test_projection_vectors_are_encrypted_and_disposable_on_backup_restore(workspace, app, clock):
    import json

    from sqlalchemy import text

    service, p = app.state.service, principal(workspace)
    assert "metric_contributions" in MODELS, "Projection migration has not been implemented"
    with service.db.transaction() as session:
        row = put(
            session,
            "metric_contributions",
            p["tenant_id"],
            p["seller_id"],
            {
                "source_kind": "lead",
                "source_id": "synthetic-source",
                "campaign_id": workspace["campaign_id"],
                "definition_version": "test-v1",
                "source_version": 3,
                "next_refresh_at": None,
                "values": {"private-derived-sentinel": 2},
            },
            key="vtest-v1:lead:synthetic-source",
            now=clock.now,
        )
        identity = row.id
    with service.db.engine.connect() as connection:
        stored = connection.execute(
            text("SELECT data FROM metric_contributions WHERE id=:id"), {"id": identity}
        ).scalar()
        assert "private-derived-sentinel" not in str(stored)
        assert "od_encrypted" in str(stored)
    receipt = service.backup(p)
    blob = service.settings.data_dir / "backups" / (receipt["backup_id"] + ".backup")
    saved = json.loads(service.vault.cipher.decrypt(blob.read_bytes()))
    assert not {"report_projections", "metric_contributions", "metric_buckets"} & saved["tables"].keys()
    service.restore(p, receipt["backup_id"])
    with service.db.transaction(False) as session:
        assert get(session, "metric_contributions", identity) is None
        assert len(service.scoped(session, "leads", p)) == 3


def test_unsubmitted_or_future_call_cannot_increment_attempts(clock):
    from app.optimization.projections import call_values

    config = {"conversion_goal": "meeting_held", "window_days": 14}
    base = {"id": "c", "lead_id": "l", "started_at": clock.now.isoformat(), "state": "AUTHORIZED"}
    assert call_values(base, config=config, now=clock.now)["attempts"] == 0
    future = (clock.now + timedelta(hours=1)).isoformat()
    assert (
        call_values({**base, "submitted_at": future, "started_at": future}, config=config, now=clock.now)[
            "attempts"
        ]
        == 0
    )


def test_help_contribution_keeps_generated_display_use_and_feedback_separate(clock):
    from app.optimization.projections import help_values

    event = {
        "id": "card",
        "action": "SHOW_CARD",
        "call_id": "call",
        "rep_id": "rep",
        "strategy_exposure": {"displayed_at": None},
        "latency": {"inference_ms": 700, "state_ms": 12, "retrieval_ms": 4},
    }
    values = help_values(event, None, now=clock.now)
    assert values["generated_guidance"] == 1 and values["displayed_guidance"] == 0
    assert values["rep_use_unknown"] == 1 and values["reviewed_cards"] == 0
    assert "unique_conversions" not in values
    shown = {**event, "strategy_exposure": {"displayed_at": clock.now.isoformat(), "used_by_rep": False}}
    feedback = {"useful": True, "grounded": None, "timely": False, "distracting": None}
    revised = help_values(shown, feedback, now=clock.now)
    assert revised["displayed_guidance"] == revised["rep_use_no"] == revised["reviewed_cards"] == 1
    assert revised["rep_use_unknown"] == 0
    assert revised["useful_true"] == revised["useful_count"] == 1
    assert revised["grounded_count"] == 0 and revised["timely_count"] == 1 and revised["timely_true"] == 0


def test_same_definition_has_separate_inactive_namespace_keys():
    from app.optimization.projections import unit_key

    definition = "a" * 64
    one = unit_key(definition, "active", "lead", "one")
    two = unit_key(definition, "rebuilding", "lead", "one")
    assert one != two
    assert one == unit_key(definition, "active", "lead", "one")
    assert len(one) <= 512


def test_work_projection_indexes_use_effective_times_not_record_creation(workspace, app, clock):
    from datetime import timedelta

    from sqlalchemy import inspect, select

    service, p = app.state.service, principal(workspace)
    schema = inspect(service.db.engine)
    assert {"op_at", "op_due_at"} <= {c["name"] for c in schema.get_columns("work_intervals")}, (
        "Corrected intervals require indexed start/end overlap reads"
    )
    assert "op_work_end_at" in {c["name"] for c in schema.get_columns("call_attempts")}
    with service.db.transaction() as session:
        interval = put(
            session,
            "work_intervals",
            p["tenant_id"],
            p["seller_id"],
            {
                "rep_id": p["user_id"],
                "started_at": (clock.now - timedelta(minutes=2)).isoformat(),
                "ended_at": clock.now.isoformat(),
            },
            now=clock.now - timedelta(days=40),
        )
        identity = interval.id
        lead = service.scoped(session, "leads", p)[0]
        call = put(
            session,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "rep_id": p["user_id"],
                "started_at": (clock.now - timedelta(minutes=1)).isoformat(),
                "submitted_at": (clock.now - timedelta(minutes=1)).isoformat(),
                "state": "ENDED",
                "ended_at": clock.now.isoformat(),
                "detached": True,
                "rep_released_at": (clock.now - timedelta(seconds=20)).isoformat(),
            },
            now=clock.now,
        )
        call_id = call.id
    with service.db.transaction(False) as session:
        intervals = MODELS["work_intervals"]
        found = list(
            session.scalars(
                select(intervals).where(
                    intervals.op_owner_id == p["user_id"],
                    intervals.op_at < clock.now,
                    intervals.op_due_at > clock.now - timedelta(minutes=3),
                )
            )
        )
        assert [r.id for r in found] == [identity]
        from app.jobs.queue import aware

        assert aware(get(session, "call_attempts", call_id).op_work_end_at) == clock.now - timedelta(
            seconds=20
        )


def test_work_index_backfill_preserves_payload_versions_and_resumes(workspace, app, clock):
    from sqlalchemy import inspect, text

    service, p = app.state.service, principal(workspace)
    assert "op_at" in {c["name"] for c in inspect(service.db.engine).get_columns("work_intervals")}
    with service.db.transaction() as session:
        rows = [
            put(session, "work_intervals", p["tenant_id"], p["seller_id"], data, now=clock.now)
            for data in [
                {
                    "rep_id": p["user_id"],
                    "started_at": clock.now.isoformat(),
                    "lease_until": (clock.now + timedelta(seconds=90)).isoformat(),
                },
                {"rep_id": p["user_id"], "started_at": None},
            ]
        ]
        before = {r.id: (dict(r.data), r.version) for r in rows}
        seller = get(session, "sellers", p["seller_id"])
        contracts = dict(seller.data.get("completion_contracts", {}))
        contracts.pop("work_index_v1", None)
        update(seller, completion_contracts=contracts)
    with service.db.engine.begin() as connection:
        connection.execute(text("UPDATE work_intervals SET op_at=NULL, op_due_at=NULL"))
    service.db.backfill_operational_columns()
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as session:
        for identity, expected in before.items():
            row = get(session, "work_intervals", identity)
            assert (row.data, row.version) == expected
            assert bool(row.op_at) == bool(row.data.get("started_at"))
        marker = get(session, "sellers", p["seller_id"]).data["completion_contracts"]["work_index_v1"]
        assert marker["completed_at"] and marker["last_id"] == max(before)
