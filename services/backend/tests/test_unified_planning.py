"""E06/C022-C025: one demand meaning and an independent tiny capacity oracle."""

import itertools
import math
from datetime import timedelta

import pytest
from app.optimization.numbering import capacity_forecast
from test_time_capacity import NOW, lead, number, window


def plan(leads, inventory, **overrides):
    options = dict(
        now=NOW,
        horizon_hours=24,
        rep_minutes=120,
        handling_seconds=120,
        daily_capacity_per_number=5,
        policy={"max_attempts": 3, "min_retry_seconds": 3600, "local_start_hour": 9, "local_end_hour": 11},
    )
    options.update(overrides)
    return capacity_forecast(leads, inventory, **options)


def test_same_availability_same_demand():
    leads = [lead(str(i)) for i in range(12)]
    inventory = [number(daily_limit=100)]
    simple = plan(leads, inventory)
    anonymous = plan(leads, inventory, staffed_windows=[window(minutes=120)])
    named = plan(leads, inventory, staffed_windows=[{**window(minutes=120), "rep_ids": ["alice"]}])
    assert simple["forecast_attempts"] == anonymous["forecast_attempts"] == named["forecast_attempts"] == 12
    assert (
        simple["existing_only_capacity"]
        == anonymous["existing_only_capacity"]
        == named["existing_only_capacity"]
    )
    for result in (simple, anonymous, named):
        assert result.get("demand_definition") == "known_next_attempts"
        assert result.get("known_next_attempts") == 12
        retries = result.get("contingent_retry_low_mean_high")
        assert retries and 0 <= retries["low"] <= retries["mean"] <= retries["high"] <= 24


@pytest.mark.parametrize("reverse", [False, True])
def test_conservation_known_and_contingent(reverse):
    leads = [lead(str(i), region="415" if i % 2 else "212") for i in range(160)]
    if reverse:
        leads.reverse()
    result = plan(
        leads,
        [number(daily_limit=80)],
        rep_minutes=360,
        staffed_windows=[window(minutes=360)],
        policy={"max_attempts": 4, "local_start_hour": 9, "local_end_hour": 20},
    )
    assert result["existing_only_capacity"] == 80
    assert len({item["lead_id"] for item in result["existing_schedule"]}) == 80
    assert result.get("known_next_attempts") == 160
    assert result["forecast_attempts"] == 160, (
        "Uncertain retries are a separate forecast, not current obligations"
    )
    assert result.get("contingent_retry_low_mean_high", {}).get("mean", 0) > 0
    assert result["purchase_allowed"] is False


def exhaustive_maximum(regions, general, regional, rep_slots):
    """Enumerate actual assignments; no production feasibility function is reused."""
    best = 0
    for choices in itertools.product((None, "general", "regional"), repeat=len(regions)):
        if any(
            choice == "regional" and region != "415" for choice, region in zip(choices, regions, strict=True)
        ):
            continue
        count = sum(c is not None for c in choices)
        if (
            choices.count("general") <= general
            and choices.count("regional") <= regional
            and count <= rep_slots
        ):
            best = max(best, count)
    return best


@pytest.mark.parametrize("general,regional,rep_slots", list(itertools.product(range(3), range(3), (2, 4))))
def test_minimum_additions_independent_oracle(general, regional, rep_slots):
    regions = ["415", "415", "212", "212"]
    result = plan(
        [lead(str(i), region=r) for i, r in enumerate(regions)],
        [number("g", daily_limit=general), number("r", region="415", daily_limit=regional)],
        staffed_windows=[window(minutes=rep_slots * 2)],
        daily_capacity_per_number=2,
        policy={"max_attempts": 1, "local_start_hour": 9, "local_end_hour": 20},
    )
    expected = exhaustive_maximum(regions, general, regional, rep_slots)
    assert result["existing_only_capacity"] == expected
    target = min(len(regions), rep_slots)
    addition = next(
        n for n in range(3) if exhaustive_maximum(regions, general + n * 2, regional, rep_slots) == target
    )
    assert result["minimum_additional_numbers"] == addition
    assert result["lower_bound"] <= addition
    assert result.get("search_operations", math.inf) <= 200000


@pytest.mark.parametrize("ramp", [20, 50])
def test_staffing_binding_and_time_constraints(ramp):
    leads = [lead(str(i), region="415" if i < 120 else "212") for i in range(240)]
    result = plan(
        leads,
        [number("regional", region="415", daily_limit=60), number("general", daily_limit=80)],
        rep_minutes=360,
        daily_capacity_per_number=ramp,
        staffed_windows=[window(minutes=360)],
        policy={"max_attempts": 1, "local_start_hour": 9, "local_end_hour": 20},
    )
    assert result["existing_only_capacity"] == 140
    assert result["rep_capacity_attempts"] == 180
    assert result["staffing_shortfall"] == 60
    assert result["number_shortfall"] == 40
    assert result["minimum_additional_numbers"] == math.ceil(40 / ramp)
    assert result["existing_plus_addition_capacity"] == 180
    # A known callback cannot consume somebody else's anonymous headcount.
    held = plan(
        [lead("held", callback_owner_id="alice", callback_due_at=NOW.isoformat())],
        [number()],
        staffed_windows=[window(minutes=10)],
    )
    assert held["existing_only_capacity"] == 0
    bound = plan(
        [lead("bound", bound_number_id="owned")],
        [number(daily_limit=1, used_today=1), number("spare")],
        staffed_windows=[window(minutes=10)],
    )
    assert bound["minimum_additional_numbers"] == 0 and bound["existing_only_capacity"] == 0
    pending = plan(
        [lead("future")],
        [number(lifecycle="purchased", available_at=(NOW + timedelta(days=2)).isoformat())],
        staffed_windows=[window(minutes=10)],
    )
    assert pending["existing_only_capacity"] == 0


def test_unequal_work_occupies_consecutive_grid_buckets_and_full_windows():
    result = plan(
        [
            lead("short", predicted_work_seconds=30, requested_contact=True),
            lead("long", predicted_work_seconds=90),
        ],
        [number()],
        staffed_windows=[window(minutes=2)],
    )
    assert result["existing_only_capacity"] == 2
    rows = result["existing_schedule"]
    assert rows[0]["at"] == NOW.isoformat()
    assert rows[0]["end_at"] == (NOW + timedelta(seconds=30)).isoformat()
    assert rows[1]["at"] == rows[0]["end_at"]
    assert rows[1]["end_at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert result["assumptions"]["staffing_grid_seconds"] == 30


def test_one_roster_pool_cannot_count_named_and_anonymous_members_twice():
    periods = [
        {**window(minutes=2, reps=2), "pool_id": "team"},
        {**window(minutes=2), "rep_ids": ["alice"], "pool_id": "team"},
    ]
    result = plan([lead(str(i)) for i in range(3)], [number(capacity=3)], staffed_windows=periods)
    assert result["existing_only_capacity"] == 2
    assert result["rep_capacity_attempts"] == 2


def test_declared_account_channel_limit_applies_across_numbers():
    result = plan(
        [lead(str(i)) for i in range(3)],
        [number(str(i)) for i in range(3)],
        staffed_windows=[window(minutes=2, reps=3)],
        policy={"max_attempts": 1, "account_channel_limit": 1},
    )
    assert result["existing_only_capacity"] == 1
    assert result["existing_plus_addition_capacity"] == 1
    assert result["minimum_additional_numbers"] == 0


def test_pending_setup_delay_is_used_in_the_conditional_schedule():
    result = plan(
        [lead("due")],
        [number(lifecycle="purchased", available_at=(NOW + timedelta(days=2)).isoformat())],
        staffed_windows=[window(minutes=10)],
    )
    assert sum(r["capacity_waiting_for_review"] for r in result["regions"]) == 0
    assert result["proposed_additional_numbers"] == 1


def test_search_budget_is_global_and_reports_best_feasible_with_unknown_minimum(monkeypatch):
    import app.optimization.capacity as capacity

    monkeypatch.setattr(capacity, "MAX_PLANNING_STEPS", 12)
    result = plan([lead(str(i)) for i in range(40)], [number(daily_limit=2)], staffed_windows=[window()])
    assert result["search_operations"] <= 12
    assert result["search_complete"] is False
    assert result["minimum_additions"] is None
    assert result["best_found_additions"] >= 0
    assert result["known_next_attempts"] == 40
    assert result["omitted_rows"]["work"] >= 0


def test_priority_classes_and_shuffled_input_are_stable():
    rows = [
        lead("z-request", requested_contact=True),
        lead("a-value", expected_conversion=0.9),
        lead("y-coverage", coverage_due_at=NOW.isoformat()),
        lead("x-callback", callback_due_at=NOW.isoformat()),
    ]
    options = {"staffed_windows": [window(minutes=6)], "policy": {"max_attempts": 1}}
    a, b = plan(rows, [number()], **options), plan(rows[::-1], [number()], **options)
    assert a["existing_schedule"] == b["existing_schedule"]
    assert [r["lead_id"] for r in a["existing_schedule"]] == ["x-callback", "z-request", "y-coverage"]


def test_immutable_planning_contract_and_canonical_fingerprint():
    import dataclasses

    import app.optimization.capacity as capacity

    for name in ("PlanningInput", "Availability", "AttemptWork", "NumberCapacity", "PlanningResult"):
        contract = getattr(capacity, name, None)
        assert contract is not None and dataclasses.is_dataclass(contract), name
        assert contract.__dataclass_params__.frozen
    rows = [lead("b"), lead("a")]
    a, b = plan(rows, [number()]), plan(rows[::-1], [number()])
    assert a["input_fingerprint"] == b["input_fingerprint"]
    assert a["source_versions"] and a["demand_definition"] == "known_next_attempts"


@pytest.mark.parametrize("success,total", [(0, 0), (0, 100), (100, 100)])
def test_retry_forecast_uses_only_matured_conditioned_support(success, total):
    rows = [lead("one", segment="same")]
    history = [
        {
            "segment": "same",
            "attempt_index": 1,
            "needs_retry": i < success,
            "available_at": NOW.isoformat(),
            "matured": True,
        }
        for i in range(total)
    ]
    history += [
        {
            "segment": "same",
            "attempt_index": 1,
            "needs_retry": True,
            "available_at": (NOW + timedelta(days=1)).isoformat(),
            "matured": True,
        }
    ] * 50
    result = plan(
        rows,
        [number()],
        retry_observations=history,
        policy={"max_attempts": 3, "min_retry_seconds": 3600, "local_start_hour": 9, "local_end_hour": 20},
    )
    retry = result["contingent_retry_low_mean_high"]
    assert 0 <= retry["low"] <= retry["mean"] <= retry["high"] <= 2
    assert retry["support"] == total
    assert retry["prior"] == [1, 1]
    if total:
        assert (retry["mean"] > 0.8) is bool(success)
    else:
        assert retry["mean"] == pytest.approx(0.75)


def test_unhealthy_binding_requires_explicit_reassignment_evidence():
    result = plan(
        [lead("one", bound_number_id="owned")],
        [number(lifecycle="paused_for_outbound"), number("spare")],
        staffed_windows=[window(minutes=4)],
    )
    assert result["existing_only_capacity"] == 0
    assert result["binding_shortfall"] == 1
    approved = plan(
        [lead("one", bound_number_id="owned", reassignment_approved=True)],
        [number(lifecycle="paused_for_outbound"), number("spare")],
        staffed_windows=[window(minutes=4)],
    )
    assert approved["existing_schedule"][0]["number_id"] == "spare"


def test_scored_slot_choice_uses_supported_time_estimates_without_moving_a_promise():
    estimates = {"0:9:general": 0.1, "0:10:general": 0.8}
    result = plan(
        [lead("flexible", slot_estimates=estimates)], [number()], staffed_windows=[window(minutes=120)]
    )
    row = result["existing_schedule"][0]
    assert row["at"] == (NOW + timedelta(hours=1)).isoformat()
    assert row["predicted_conversions"] == 0.8
    callback = plan(
        [
            lead(
                "callback",
                callback_due_at=NOW.isoformat(),
                callback_window_end=(NOW + timedelta(minutes=2)).isoformat(),
                slot_estimates=estimates,
            )
        ],
        [number()],
        staffed_windows=[window(minutes=120)],
    )
    assert callback["existing_schedule"][0]["at"] == NOW.isoformat()


def test_proposal_fingerprint_includes_retry_evidence_capacity_and_setup():
    rows, inventory = [lead("one")], [number()]
    results = [
        plan(rows, inventory),
        plan(rows, inventory, daily_capacity_per_number=9),
        plan(rows, inventory, additional_ready_after_hours=1),
        plan(
            rows,
            inventory,
            retry_observations=[
                {
                    "segment": "unknown",
                    "attempt_index": 1,
                    "needs_retry": False,
                    "matured": True,
                    "available_at": NOW.isoformat(),
                }
            ],
        ),
    ]
    assert len({r["input_fingerprint"] for r in results}) == 4


def test_new_number_daily_limit_is_not_treated_as_a_parallel_call_allowance():
    result = plan(
        [lead(str(i), predicted_work_seconds=30) for i in range(4)],
        [],
        staffed_windows=[window(minutes=1, reps=4)],
        daily_capacity_per_number=100,
    )
    assert result["existing_plus_addition_capacity"] == 4
    assert result["minimum_additional_numbers"] == 2


def test_burst_block_at_first_bucket_does_not_hide_later_capacity():
    result = plan(
        [lead(str(i), predicted_work_seconds=30, slot_estimates={"0:9:general": 0.5}) for i in range(9)],
        [number(daily_limit=20, burst_limit=1, burst_seconds=60)],
        staffed_windows=[window(minutes=10)],
    )
    assert result["existing_only_capacity"] == 9
    times = [row["at"] for row in result["existing_schedule"]]
    assert len(set(times)) == 9


def test_unknown_candidate_quote_is_not_a_free_number():
    result = plan(
        [lead("one", predicted_work_seconds=30)],
        [],
        staffed_windows=[window(minutes=1)],
        policy={
            "proposed_number_classes": [
                {"id": "unknown", "region": "general", "daily_limit": 5, "capacity": 1},
                {"id": "quoted", "region": "general", "daily_limit": 5, "capacity": 1, "quote_usd": 1},
            ]
        },
    )
    assert result["hypothetical_added_schedule"][0]["number_id"].startswith("proposed:quoted:")


def test_runtime_proposal_reads_saved_plan_and_queues_work_without_calculating(workspace, app, monkeypatch):
    import app.optimization.workflows as workflows

    calls = []
    original = workflows.capacity_forecast

    def no_inline(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflows, "capacity_forecast", no_inline)
    response = workspace["client"].post(
        "/v1/admin/numbers/proposal",
        headers=workspace["headers"],
        json={"campaign_id": workspace["campaign_id"]},
    )
    assert response.status_code == 200
    assert not calls, "Proposal endpoint ran the planner inline"
    assert response.json()["projection"]["status"] == "pending"
    assert response.json()["projection"]["job_id"]


def test_forecast_worker_coalesces_changed_sources_without_consuming_failure_attempts(
    workspace, app, monkeypatch
):
    import asyncio

    from app.db import Job
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    response = service.proposal(p, workspace["campaign_id"])
    identity = response["projection"]["job_id"]
    monkeypatch.setattr(service, "build_forecast", lambda *args: {"status": "superseded"})
    asyncio.run(service.run_jobs(p["tenant_id"], ["ml"]))
    with service.db.transaction(False) as s:
        job = s.get(Job, identity)
        assert job.status == "queued" and job.attempts == 0
        assert job.error is None and job.lease_owner is None


def test_failed_forecast_is_an_actionable_state_and_is_not_restarted_by_a_read(workspace, app):
    from app.db import Job
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    response = service.proposal(p, workspace["campaign_id"])
    identity = response["projection"]["job_id"]
    with service.db.transaction() as s:
        job = s.get(Job, identity)
        job.status, job.error, job.attempts = "dead", "SyntheticPlanningFailure", 5
    response = service.proposal(p, workspace["campaign_id"])
    assert response["projection"]["refresh_status"] == "dead"
    assert response["projection"]["refresh_error"] == "SyntheticPlanningFailure"
    assert response["projection"]["job_id"] == identity


def test_expansion_advice_waits_for_persistence_or_an_explicit_staffing_change(
    workspace, app, clock, monkeypatch
):
    import app.optimization.workflows as workflows
    from app.db import update
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    calculate = workflows.capacity_forecast

    def shortfall(**kwargs):
        return {**calculate(**kwargs), "number_shortfall": 5}

    monkeypatch.setattr(workflows, "capacity_forecast", shortfall)

    def refresh():
        assert service.build_forecast(p, workspace["campaign_id"])["status"] == "ready"
        return service.proposal(p, workspace["campaign_id"])["expansion_review"]

    assert refresh()["status"] == "observing"
    clock.now += timedelta(hours=23)
    assert refresh()["status"] == "observing"
    clock.now += timedelta(hours=1)
    assert refresh()["status"] == "eligible"
    monkeypatch.setattr(
        workflows, "capacity_forecast", lambda **kwargs: {**calculate(**kwargs), "number_shortfall": 0}
    )
    assert refresh()["status"] == "sufficient"
    monkeypatch.setattr(workflows, "capacity_forecast", shortfall)
    assert refresh()["status"] == "observing"
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(campaign, optimization={**campaign.data.get("optimization", {}), "rep_minutes": 100})
    assert refresh()["reason"] == "list_or_staffing_changed"


def test_forecast_bulk_reads_suppressions_and_retains_claimed_list_denominator(workspace, app, monkeypatch):
    from conftest import reserve
    from sqlalchemy import event
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    assert reserve(workspace).status_code == 200
    with service.db.transaction(False) as s:
        count = len(service.scoped(s, "leads", p))
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM suppressions" in statement:
            queries.append(statement)

    event.listen(service.db.engine, "before_cursor_execute", capture)
    try:
        with service.db.transaction(False) as s:
            inputs, metadata = service.forecast_input(s, p)
        assert len(queries) <= 1, "Screening read once per lead"
        assert len(inputs["leads"]) == count
        assert sum(bool(row.get("active_obligation")) for row in inputs["leads"]) == 1
    finally:
        event.remove(service.db.engine, "before_cursor_execute", capture)


def test_simple_staffing_is_explicit_versioned_and_validated(workspace, app):
    from test_remediation_privacy import principal

    body = {
        "staffing_timezone": "America/Los_Angeles",
        "staffing_start_hour": 10,
        "staffing_end_hour": 16,
        "staffing_reps": 2,
        "staffing_weekdays": [0, 1, 2, 3, 4],
        "account_channel_limit": 2,
        "minimum_expansion_age_hours": 24,
    }
    c, h = workspace["client"], workspace["headers"]
    response = c.put("/v1/admin/optimization", headers=h, json=body)
    assert response.status_code == 200, response.text
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        config = service.optimization_config(s, p)
        assert all(config[k] == v for k, v in body.items())
    for invalid in (
        {"staffing_timezone": "Mars/Base"},
        {"staffing_start_hour": 19},
        {"staffing_weekdays": [0, 8]},
        {"account_channel_limit": 0},
    ):
        assert c.put("/v1/admin/optimization", headers=h, json=invalid).status_code == 422


def test_forecast_uses_the_saved_work_estimate_and_prior_versions(workspace, app):
    from app.db import update
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            campaign,
            selection_profile={
                "goal": "meeting_held",
                "version": "observed-v3",
                "pooled": {
                    "p_human": 0.5,
                    "p_right_party_given_human": 0.5,
                    "p_conversion_given_right_party": 0.5,
                    "expected_rep_seconds": 30,
                },
            },
        )
        report = service.forecast(s, p)
    assert report["source_versions"]["estimates"] == "observed-v3"
    assert report["existing_schedule"]
    for row in report["existing_schedule"]:
        from datetime import datetime

        assert (
            datetime.fromisoformat(row["end_at"]) - datetime.fromisoformat(row["at"])
        ).total_seconds() == 30


@pytest.mark.parametrize("invalidate", [False, True])
def test_background_forecast_releases_read_transaction_and_fences_publication(
    workspace, app, monkeypatch, invalidate
):
    from contextlib import contextmanager

    import app.optimization.workflows as workflows
    from app.db import update
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    build = getattr(service, "build_forecast", None)
    assert callable(build), "A background forecast needs a source-fenced publication path"
    active = []
    transaction, calculate = service.db.transaction, workflows.capacity_forecast

    @contextmanager
    def observed(*args, **kwargs):
        with transaction(*args, **kwargs) as s:
            active.append(s)
            try:
                yield s
            finally:
                active.pop()

    def outside(*args, **kwargs):
        assert not active, "Planner retained a database transaction"
        if invalidate:
            with service.db.transaction() as s:
                number = service.scoped(s, "number_inventory", p)[0]
                update(number, daily_limit=0)
        return calculate(*args, **kwargs)

    monkeypatch.setattr(service.db, "transaction", observed)
    monkeypatch.setattr(workflows, "capacity_forecast", outside)
    result = build(p, workspace["campaign_id"])
    assert result["status"] == ("superseded" if invalidate else "ready")
    response = (
        workspace["client"]
        .post(
            "/v1/admin/numbers/proposal",
            headers=workspace["headers"],
            json={"campaign_id": workspace["campaign_id"]},
        )
        .json()
    )
    assert response["projection"]["status"] == ("pending" if invalidate else "ready")
    if not invalidate:
        assert response["input_fingerprint"] and response["source_versions"]
