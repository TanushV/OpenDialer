"""Maintained October 16 audit regressions; committed before October 17 product repairs."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from app.db import doc, update
from app.optimization.numbering import capacity_forecast
from app.providers.inference import InferenceResult
from app.transcripts import normalize_transcription
from conftest import disposition_body, start
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def test_shared_general_capacity_must_not_be_consumed_twice():
    now = datetime(2026, 10, 14, 10, tzinfo=UTC)
    leads = [
        dict(id=region + str(i), region=region, timezone="UTC", status="eligible")
        for region in ("415", "general")
        for i in range(80)
    ]
    inventory = [dict(id="general-number", region="general", lifecycle="healthy", daily_limit=80)]
    result = capacity_forecast(
        leads, inventory, now=now, rep_minutes=320, policy={"max_attempts": 1}, daily_capacity_per_number=80
    )
    allocated = sum(r["usable_existing_capacity"] + r["general_fallback_capacity"] for r in result["regions"])
    assert allocated <= 80, result


def test_documented_telnyx_transcription_track_is_recognized():
    result = normalize_transcription(
        {
            "transcription_data": {
                "transcription_track": "inbound",
                "transcript": "Please stop calling me.",
                "is_final": True,
            }
        },
        "call",
        "event",
    )
    assert result[0]["side"] == "remote", result


def test_ended_agent_leg_cannot_be_revived_by_late_answer(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    agent = c.post("/v1/telephony/agent", headers=h).json()["agent_call_control_id"]
    payload = {"call_control_id": agent}
    app.state.service.agent_event(workspace["tenant_id"], payload, "call.hangup")
    app.state.service.agent_event(workspace["tenant_id"], payload, "call.answered")
    result = c.get("/v1/telephony/agent/" + agent, headers=h).json()
    assert result["state"] == "ended", result


def test_transcript_processing_retries_after_durable_event_receipt(workspace, app, clock, monkeypatch):
    call = start(workspace)
    service = app.state.service
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    service.vault.write(workspace["tenant_id"], {"telnyx_public_key": public})
    body = json.dumps(
        {
            "data": {
                "id": "audit-transcript-retry",
                "event_type": "call.transcription",
                "payload": {
                    "call_control_id": call["provider_id"],
                    "transcription_data": {
                        "track": "inbound",
                        "is_final": True,
                        "transcript": "We have no budget.",
                    },
                },
            }
        }
    ).encode()
    timestamp = str(int(clock.now.timestamp()))
    headers = {
        "telnyx-timestamp": timestamp,
        "telnyx-signature-ed25519": base64.b64encode(private.sign(timestamp.encode() + b"|" + body)).decode(),
    }
    original = service._accept_transcript

    def temporary_failure(*args, **kwargs):
        raise HTTPException(503, "Injected temporary processing failure")

    monkeypatch.setattr(service, "_accept_transcript", temporary_failure)
    c = workspace["client"]
    # Admission now acknowledges durable receipt; processing failure is retried by its job.
    assert c.post("/webhooks/telnyx", content=body, headers=headers).status_code == 200
    failed = asyncio.run(service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"]))
    assert failed["processed"] == 0
    monkeypatch.setattr(service, "_accept_transcript", original)
    retried = c.post("/webhooks/telnyx", content=body, headers=headers)
    assert retried.status_code == 200
    clock.now += timedelta(seconds=11)
    asyncio.run(service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"]))
    saved = c.get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()["transcript"]
    assert len(saved) == 1, {"retry_receipt": retried.json(), "transcript": saved}


@pytest.mark.asyncio
async def test_interrupted_control_command_has_a_recovery_path(workspace, app):
    call = start(workspace)
    p, service = principal(workspace), app.state.service
    with service.db.transaction() as s:
        command = service.control_intent(
            s,
            p,
            call["id"],
            "transcription_start",
            {"transcription_engine": "Soniox", "transcription_tracks": "both"},
        )
        update(command, status="sending")  # Process died after intent fence, before receipt persistence.
        identifier = command.id
    await service.recover(p)
    await service.dispatch_controls(p)
    with service.db.transaction(False) as s:
        current = service.required(s, "command_outbox", identifier, p)
        assert current.data["status"] != "sending", doc(current)


def test_uncertain_purchase_cannot_be_resubmitted_with_new_ui_key(workspace, carrier, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.put(
            "/v1/admin/budget",
            headers=h,
            json={"daily_limit_usd": 10, "monthly_limit_usd": 100, "number_purchase_limit_usd": 10},
        ).status_code
        == 200
    )
    quote = c.post("/v1/admin/numbers/search", headers=h, json={"region": "415"}).json()["numbers"][0]
    submitted = []

    async def uncertain(numbers, command_id):
        submitted.append((numbers, command_id))
        raise TimeoutError("Response lost after provider acceptance")

    monkeypatch.setattr(carrier, "purchase", uncertain)
    for key in ("first-click", "second-click"):
        c.post(
            "/v1/admin/numbers/purchase",
            headers=h,
            json={"phone_numbers": [quote["phone"]], "approved_budget_usd": 1, "idempotency_key": key},
        )
    assert len(submitted) <= 1, submitted  # No-subscription policy may reject both before purchase.


@pytest.mark.asyncio
async def test_postcall_grounding_rejects_a_contradictory_claim_with_a_real_quote(
    workspace, app, monkeypatch
):
    from test_postcall_completion import setup_analysis

    call, state = setup_analysis(workspace, app, monkeypatch)
    service, who = app.state.service, principal(workspace)
    source = "We are happy with our current provider and have no problems."
    service._receive_transcript(
        who,
        call["id"],
        {"segment_id": "s", "revision": 2, "is_final": True, "side": "remote", "text": source},
        None,
        True,
    )
    state["body"].update(
        summary="The prospect is unhappy with their current provider.",
        commitments=[],
        problems=[
            {
                "text": "The prospect is unhappy with their current provider.",
                "quote": source,
                "evidence": ["s"],
                "review_required": True,
            }
        ],
    )
    state["verdict"] = "CONTRADICTED"
    await service.analyze_postcall(who, call["id"])
    result = service.analysis(who, call["id"])
    assert "unhappy" not in result["notes"]["summary"]
    assert result["notes"]["problems"] == []
    assert result["grounding_status"] == "review_required"


def test_corrected_transcript_invalidates_old_ai_scorecard(workspace, app):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"]
    segment = {
        "segment_id": "s1",
        "revision": 1,
        "side": "rep",
        "is_final": True,
        "text": "What business problem would you like us to help solve?",
    }
    assert c.post(path + "/transcript", headers=h, json=segment).status_code == 200
    c.get(path + "/analysis", headers=h)
    p, service = principal(workspace), app.state.service
    with service.db.transaction() as s:
        card = next(r for r in service.scoped(s, "scorecards", p) if r.external_key == call["id"])
        update(
            card,
            items=[
                {
                    "dimension": "problem_discovery",
                    "status": "SCORED",
                    "score": 2,
                    "max_score": 2,
                    "weight": 1,
                    "evidence": ["s1"],
                }
            ],
            overall={"score": 100, "coverage": 1},
            model_request_id="old-evaluated-request",
        )
    c.post(
        path + "/transcript",
        headers=h,
        json={**segment, "revision": 2, "text": "Hello? I cannot hear you. Goodbye."},
    )
    result = c.get(path + "/analysis", headers=h).json()
    assert result["scorecard"]["overall"].get("score") != 100, result["scorecard"]


def test_new_conversion_goal_reopens_previously_completed_goal(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/calls/" + call["id"] + "/hangup", headers=h)
    c.post(
        "/v1/calls/" + call["id"] + "/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], **{"outcome": "meeting_held"}),
    )
    changed = c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    assert changed.status_code == 200
    lead = next(r for r in c.get("/v1/workspace", headers=h).json()["leads"] if r["id"] == call["lead_id"])
    assert lead["status"] == "eligible", lead


def test_recorded_provider_cost_reconciles_the_optional_budget_hold(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(service.scoped(s, "budgets", p)[0], daily_limit_usd=0.01, monthly_limit_usd=1)
    assert service.reserve_optional_budget(p, "one-request", 0.01, object_id="call", work_kind="live_coach")
    service.usage(
        p,
        "openrouter",
        "live_coach",
        InferenceResult({}, {"cost": 0.0001}, "receipt"),
        object_id="call",
        reserved=0,
        request_key="one-request",
    )
    with service.db.transaction(False) as s:
        assert service.budget_ok(s, p, estimated=0.001), [
            r.data for r in service.scoped(s, "usage_ledger", p)
        ]


def test_forecast_handles_a_region_without_sharing_capacity():
    now = datetime(2026, 10, 14, 10, tzinfo=UTC)
    report = capacity_forecast(
        [{"id": str(i), "region": "415", "timezone": "UTC"} for i in range(80)],
        [{"id": "n", "region": "415", "lifecycle": "healthy", "daily_limit": 80}],
        now=now,
        policy={"max_attempts": 1},
        daily_capacity_per_number=80,
    )
    assert report["regions"][0]["proposed_additional"] == 0


def test_legacy_fixture_track_is_still_recognized():
    result = normalize_transcription(
        {"transcription_data": {"track": "inbound", "transcript": "We have no budget.", "is_final": True}},
        "call",
        "event",
    )
    assert result[0]["side"] == "remote"


def test_delayed_labels_do_not_leak_past_the_validation_start():
    from app.ml.training import independent_partitions

    now = datetime(2026, 9, 1, 10, tzinfo=UTC)
    rows = [
        {
            "lead_id": str(i),
            "endpoint_id": "e" + str(i),
            "at": (now + timedelta(minutes=i)).isoformat(),
            "outcome_available_at": (now + timedelta(minutes=i, seconds=1)).isoformat(),
            "label_available_at": (now + timedelta(minutes=i, days=14)).isoformat(),
            "window_ends_at": (now + timedelta(minutes=i, days=14)).isoformat(),
            "window_closed": True,
            "conversion": 0,
        }
        for i in range(100)
    ]
    train, calibration, validation = independent_partitions(rows)
    # Every training/calibration label matures after both cutoffs; neither partition
    # may use it, even when the eventual full-dataset snapshot says window_closed.
    assert train == calibration == []
    assert validation == rows[80:]


def test_app_score_coverage_counts_all_rubric_dimensions():
    from app.postcall.analysis import DIMENSIONS, score_summary

    segments = [
        {"segment_id": "s", "text": "Hello and thanks for taking my call.", "is_final": True, "side": "rep"}
    ]
    data = {
        "summary": "Hello and thanks for taking my call.",
        "summary_evidence": ["s"],
        "problems": [],
        "objections": [],
        "commitments": [],
        "unknowns": [],
        "score_items": [
            {
                "dimension": "identity_relevance",
                "score": 2,
                "status": "SCORED",
                "evidence": ["s"],
                "explanation": "Rep introduces the call",
            }
        ],
    }
    # Incremental ratings preserve full configured coverage; legacy views do too.
    from app.postcall.analysis import build_analysis
    from test_postcall_completion import merge_test_analysis

    merged = merge_test_analysis(data, segments)
    assert build_analysis("fixture", segments, derived=merged)["scorecard"]["overall"]["coverage"] == 0
    coverage = score_summary(data["score_items"])["coverage"]
    assert coverage <= 1 / len(DIMENSIONS), {
        "reported_coverage": coverage,
        "returned_items": len(data["score_items"]),
        "rubric_items": len(DIMENSIONS),
    }


def test_script_cannot_hide_personalization_by_omitting_claims():
    from app.research.validation import ValidationFailure, validate_script

    candidate = {
        "lead_id": "lead",
        "opener": "Your company just opened a new office in Seattle.",
        "screening_intro": "Alex from Example Seller with a business introduction.",
        "discovery_questions": [],
        "fact_ids_used": [],
        "claims": [],
    }
    with pytest.raises(ValidationFailure):
        validate_script(candidate, [], "example.test", "Example Seller", datetime(2026, 10, 14, tzinfo=UTC))


def test_negated_budget_approval_does_not_resolve_an_objection():
    from app.coaching.context import build_context

    segments = [
        {
            "segment_id": "s1",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "We do not have any budget for this.",
        },
        {
            "segment_id": "s2",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "The budget is not approved.",
        },
    ]
    prior = {
        "objections": [
            {
                "id": "prior-budget-objection",
                "text": "Spending remains unapproved.",
                "status": "unresolved",
                "evidence_segment_ids": ["s1"],
                "evidence_revisions": {"s1": 1},
            }
        ]
    }
    result = build_context(segments, version=2, facts=[], commitments=[], prior=prior)
    # Removing the regex classifier must not erase or resolve the earlier evidenced objection.
    assert len(result["objections"]) == 1
    objection = result["objections"][0]
    assert {key: objection[key] for key in prior["objections"][0]} == prior["objections"][0]
    assert objection["state"] == "unresolved"
    assert objection["sources"][0]["id"] == "s1"
    assert objection["sources"][0]["version"] == 1
    assert result["trigger"] == "conversation"


@pytest.mark.asyncio
async def test_six_independent_coaching_jobs_fit_the_shared_latency_budget(workspace, app, monkeypatch):
    import asyncio
    import math

    service, p = app.state.service, principal(workspace)
    work_seconds, budget_seconds = 0.7, 2.5  # Modelled 700 ms work, not a measured provider latency.
    active, peak, completed = 0, 0, []
    all_admitted = asyncio.Event()

    async def bounded_provider_work(principal, call_id):
        # Each job holds its slot until all six overlap, so concurrency is observed, not raced.
        # Only a narrower admission reaches the generous window; its peak is then measured.
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 6:
            all_admitted.set()
        try:
            await asyncio.wait_for(all_admitted.wait(), 5)
        except TimeoutError:
            pass
        finally:
            active -= 1
        completed.append(call_id)

    monkeypatch.setattr(service, "coach_turn", bounded_provider_work)
    for i in range(6):
        service.jobs.enqueue(
            "live_coach",
            {"call_id": f"isolated-call-{i}", "seller_id": p["seller_id"], "user_id": p["user_id"]},
            f"audit-coach-{i}",
            tenant_id=p["tenant_id"],
        )
    result = await service.run_jobs(queues=["live_coach"])
    assert result["processed"] == 6 and len(completed) == 6
    # Shared-budget model: six jobs at `peak` concurrency need ceil(6 / peak) sequential waves.
    waves = math.ceil(6 / peak)
    assert waves * work_seconds < budget_seconds, {"peak_concurrency": peak, "waves": waves}
