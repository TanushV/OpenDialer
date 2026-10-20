"""E02/C005-C006: public transcript ingestion and one bounded long-call state."""

import json
from datetime import timedelta

import pytest
from app.db import update
from test_live_memory import setup
from test_remediation_privacy import persisted_content


@pytest.mark.parametrize("writer", ["analysis", "human_note"])
@pytest.mark.postgres
def test_analysis_writers_wait_for_the_same_call_without_blocking_other_calls(
    workspace, app, clock, monkeypatch, writer
):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import current_thread

    from app.db import MODELS, put
    from conftest import blocked_by, start
    from sqlalchemy import func, select, text
    from test_control_completion import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    initial = service.analysis(p, call["id"])
    with service.db.transaction() as s:
        original = service.required(s, "call_attempts", call["id"], p)
        other = put(s, "call_attempts", p["tenant_id"], p["seller_id"], dict(original.data), now=clock())
        other_id = other.id
    service.analysis(p, other_id)
    transaction, analysis = service.db.transaction, service.analysis
    writers = []

    @contextmanager
    def observed_transaction(*args, **kwargs):
        with transaction(*args, **kwargs) as s:
            if current_thread().name.startswith("waiting-note"):
                writers.append(s.execute(text("SELECT pg_backend_pid()")).scalar())
            yield s

    monkeypatch.setattr(service.db, "transaction", observed_transaction)
    if writer == "human_note":
        # The initial read has already completed: exercise the human write's
        # own boundary, independently of the preceding analysis refresh.
        monkeypatch.setattr(service, "analysis", lambda *args, **kwargs: initial)

    def finalize_views(call_id):
        # The finalization writer materializes views under the call lock; reads never do.
        with service.db.transaction() as s:
            row = service.required(s, "call_attempts", call_id, p)
            s.refresh(row, with_for_update=True)
            service._materialize_views_in(s, p, row, finalized=True)
        return analysis(p, call_id)

    def write():
        if writer == "analysis":
            return finalize_views(call["id"])
        return service.edit_notes(p, call["id"], initial["notes_version"], "Keep the rep's correction.")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="waiting-note") as pool:
        with transaction() as s:
            locked = service.required(s, "call_attempts", call["id"], p)
            s.refresh(locked, with_for_update=True)
            holder = s.execute(text("SELECT pg_backend_pid()")).scalar()
            future = pool.submit(write)
            # Observed lock state, not a lock_timeout race: the writer waits on this call's owner.
            blocked_by(service.db.engine, writers, holder, future)
            assert finalize_views(other_id)["status"] == "ai_finalized"
            assert not future.done(), "Another call finished while this call's writer still waits"
            blocked_by(service.db.engine, writers, holder, future)
        result = future.result(timeout=120)
    if writer == "analysis":
        assert result["status"] == "ai_finalized"
    else:
        assert result["human_notes"] == "Keep the rep's correction."
    final = analysis(p, call["id"])
    if writer == "human_note":
        assert final["human_notes"] == "Keep the rep's correction."
    with transaction(False) as s:
        for kind in ("notes", "scorecards"):
            model = MODELS[kind]
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.external_key == call["id"],
                    )
                )
                # Never duplicated; a human note alone materializes no scorecard.
                == (1 if kind == "notes" or writer == "analysis" else 0)
            )


@pytest.mark.asyncio
async def test_only_one_canonical_state_is_written_and_legacy_arrays_are_read_views(
    workspace, app, clock, monkeypatch
):
    from test_live_memory import turn

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "canonical")
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction(False) as s:
        state = service._by_key(s, p, "conversation_states", call["id"]).data
        assert state.get("schema_version") == 2
        assert "rundown_points" not in state, "Compatibility views must not become duplicate writers"
        assert state["observations"][0]["epistemic_status"] == "model_interpreted"
        assert state["observations"][0]["sources"][0]["id"] == "canonical"
        assert state["purpose"] and state["rubric_items"]
    assert service.analysis(p, call["id"])["call_memory"]["rundown_points"]


@pytest.mark.postgres
def test_finalization_and_preparation_release_preserve_each_others_lead_changes(workspace, app, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event, current_thread

    from conftest import blocked_by, start
    from sqlalchemy import text
    from test_control_completion import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    workspace["client"].post(
        f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"]
    ).raise_for_status()
    owner = service._begin_finalization(p, call["id"])
    preparation_owner = service.claim_preparation(p, call["lead_id"])
    assert owner and preparation_owner
    entered, release = Event(), Event()
    transaction, refresh = service.db.transaction, service.refresh_packet_in
    finalizers, preparers = [], []

    def held_refresh(s, principal, lead):
        if current_thread().name.startswith("finishing-call"):
            finalizers.append(s.execute(text("SELECT pg_backend_pid()")).scalar())
            entered.set()
            assert release.wait(120), "Finalizer was not released"
        return refresh(s, principal, lead)

    @contextmanager
    def observed_transaction(*args, **kwargs):
        with transaction(*args, **kwargs) as s:
            if current_thread().name.startswith("waiting-preparation"):
                preparers.append(s.execute(text("SELECT pg_backend_pid()")).scalar())
            yield s

    monkeypatch.setattr(service, "refresh_packet_in", held_refresh)
    monkeypatch.setattr(service.db, "transaction", observed_transaction)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="finishing-call") as finalizer,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="waiting-preparation") as preparer,
    ):
        finished = finalizer.submit(service._finish_finalization, p, call["id"], owner, True)
        try:
            assert entered.wait(120)
            prepared = preparer.submit(service.release_preparation, p, call["lead_id"], preparation_owner)
            # Observed lock state, not a lock_timeout race: preparation cannot publish over the
            # finalizer's already-read lead and waits on that exact transaction.
            blocked_by(service.db.engine, preparers, finalizers[-1], prepared)
        finally:
            release.set()
        finished.result(timeout=120)
        prepared.result(timeout=120)
    with transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], p)
        assert lead.data["preparation_lease"] is None
        assert lead.data["preparation_revision"] > 0
        state = service._by_key(s, p, "conversation_states", call["id"])
        assert state.data["finalization"]["status"] == "ready"


@pytest.mark.asyncio
async def test_provider_context_contains_one_bounded_schema_without_flattened_legacy_views(
    workspace, app, monkeypatch
):
    from app.coaching.context import STATE_FIELDS
    from app.coaching.contracts import ConversationStateV2
    from test_live_memory import turn

    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "prior")
    await service.coach_turn(p, call["id"], urgent=True)
    turn(service, p, call, "current")
    await service.coach_turn(p, call["id"], urgent=True)
    contexts = [requests[-1]] + [
        service._conversation_context(p, call["id"], mode) for mode in ("finalize", "review")
    ]
    for request in contexts:
        state = request["state"]
        assert state["observations"], "Retained context must still reach each model role"
        assert not set(STATE_FIELDS).intersection(state), "Compatibility arrays duplicate model context"
        assert set(state) == set(ConversationStateV2.model_fields)
        assert len(json.dumps(state, separators=(",", ":")).encode()) <= 65536


def conversation_fixture():
    from datetime import UTC, datetime

    from app.coaching.context import build_context

    at = datetime(2026, 10, 18, 18, tzinfo=UTC)
    segment = {
        "segment_id": "source",
        "revision": 1,
        "side": "remote",
        "is_final": True,
        "text": "We need to reduce the approval delay for our purchasing team.",
        "server_received_at": at.isoformat(),
        "raw_expires_at": (at + timedelta(seconds=300)).isoformat(),
    }
    state = build_context([segment], version=1, facts=[], commitments=[])
    state["call_id"] = "01a13df6-144b-7d51-98d9-4067607f313e"
    return state, segment, at


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_outcome_suggestion_is_reviewed_with_its_contact_role_but_never_confirms_a_label(
    workspace, app, clock, monkeypatch, supported
):
    from app.grounding import accept_review
    from test_grounding_review import review_response
    from test_live_memory import turn

    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(
        service,
        p,
        call,
        "classification",
        text="I handle purchase approvals. Please call me next Tuesday to discuss the process.",
    )
    source = {"kind": "segment", "id": "classification", "version": 1}
    changes["result"] = {
        "action": "NO_ACTION",
        "state_update": {
            "stage": "next_step",
            "observations": [
                {
                    "kind": "signal",
                    "text": "The contact requested another discussion about purchasing.",
                    "state": "confirmed",
                    "sources": [source],
                    "replaces": [],
                    "quote": "",
                    "commitment": None,
                    "outcome": {"code": "callback_requested", "contact_role": "right_party"},
                }
            ],
            "rubric_items": [],
            "tracker_matches": [],
            "interpreted_segments": [source],
        },
    }
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction(False) as s:
        state = service._by_key(s, p, "conversation_states", call["id"]).data
        assert state["observations"][0]["review_status"] == "pending"
    assert service.analysis(p, call["id"])["notes"].get("suggested_outcome") is None
    request = service._conversation_context(p, call["id"], "review")
    owner, review = service._state_review_snapshot(p, call["id"], request)
    assert review["fields"][0]["value"]["outcome"] == {
        "code": "callback_requested",
        "contact_role": "right_party",
    }
    receipt = accept_review(review, review_response(review), "outcome-review") if supported else None
    service._publish_state_review(p, call["id"], request, owner, receipt)
    analysis = service.analysis(p, call["id"])
    suggestion = analysis["notes"]["suggested_outcome"]
    assert bool(suggestion) == supported
    current = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"]).json()
    assert current.get("suggested_outcome") == suggestion
    assert current.get("rundown_status") == analysis["finalization"]["status"]
    assert not analysis["call"].get("outcome"), "Model interpretation is not a confirmed training label"
    with service.db.transaction(False) as s:
        assert not service.lead_records(s, p, "followup_tasks", call["lead_id"])
    if supported:
        assert suggestion["code"] == "callback_requested" and suggestion["source_observation_id"]
        clock.now += timedelta(seconds=301)
        assert service.analysis(p, call["id"])["notes"]["suggested_outcome"] == suggestion
        turn(
            service,
            p,
            call,
            "classification",
            revision=2,
            text="I cannot speak for purchasing and did not request a follow-up.",
        )
        assert service.analysis(p, call["id"])["notes"]["suggested_outcome"] is None


async def pending_review(workspace, app, clock, monkeypatch):
    from app.grounding import accept_review
    from test_grounding_review import review_response
    from test_live_memory import turn

    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "source")
    changes["result"] = {
        "action": "NO_ACTION",
        "state_update": {
            "stage": "next_step",
            "commitments": [
                {
                    "text": "The buyer wants another procurement discussion.",
                    "status": "confirmed",
                    "evidence_segment_ids": ["source"],
                    "quote": "",
                }
            ],
        },
    }
    await service.coach_turn(p, call["id"], urgent=True)
    request = service._conversation_context(p, call["id"], "review")
    owner, review = service._state_review_snapshot(p, call["id"], request)
    receipt = accept_review(review, review_response(review), "review-fixture")
    return service, call, p, request, owner, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "lease", "note", "purpose", "ownership", "revision", "capture"])
async def test_review_publication_rechecks_sources_and_owner_inside_commit(
    workspace, app, clock, monkeypatch, change
):
    from test_live_memory import turn

    service, call, p, request, owner, receipt = await pending_review(workspace, app, clock, monkeypatch)
    if change in {"expiry", "lease"}:
        clock.now += timedelta(seconds=301 if change == "expiry" else 31)
    elif change == "revision":
        turn(service, p, call, "source", revision=2, text="The earlier understanding was mistaken.")
    else:
        service.analysis(p, call["id"])
        with service.db.transaction() as s:
            if change == "note":
                update(
                    service._materialize_views_in(
                        s, p, service.required(s, "call_attempts", call["id"], p), scorecard=False
                    )[0],
                    human_text="Rep correction takes priority.",
                )
            elif change == "capture":
                update(service.required(s, "call_attempts", call["id"], p), transcription_disabled=True)
            elif change == "purpose":
                update(service.required(s, "call_attempts", call["id"], p), rubric_revision="new-purpose")
            else:
                update(service.required(s, "users", p["user_id"], p), role="rep")
                update(service.required(s, "call_attempts", call["id"], p), rep_id="other-rep")
    # This boundary is after a successful network-result check and before the commit.
    service._publish_state_review(p, call["id"], request, owner, receipt)
    with service.db.transaction(False) as s:
        state = service._by_key(s, p, "conversation_states", call["id"]).data
        assert all(i["epistemic_status"] != "model_reviewed" for i in state["observations"])
        assert not service.lead_records(s, p, "followup_tasks", call["lead_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "note", "ownership"])
async def test_finalizer_publication_cannot_accept_context_changed_after_response(
    workspace, app, clock, monkeypatch, change
):
    from test_live_memory import turn

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "final")
    service.analysis(p, call["id"])
    owner = service._begin_finalization(p, call["id"])
    request = service._conversation_context(p, call["id"], "finalize")
    result = {
        "action": "NO_ACTION",
        "state_version": request["state_version"],
        "state_update": {
            "stage": "discovery",
            "rundown_points": [
                {
                    "text": "The buyer described a process delay.",
                    "status": "confirmed",
                    "evidence_segment_ids": ["final"],
                    "quote": "",
                }
            ],
        },
    }
    if change == "expiry":
        clock.now += timedelta(seconds=301)
    else:
        with service.db.transaction() as s:
            if change == "note":
                note, _ = service._materialize_views_in(
                    s, p, service.required(s, "call_attempts", call["id"], p), scorecard=False
                )
                update(note, human_text="Corrected after generation.")
            else:
                update(service.required(s, "users", p["user_id"], p), role="rep")
                update(service.required(s, "call_attempts", call["id"], p), rep_id="other-rep")
    assert service._merge_finalization(p, call["id"], owner, request, result) is False
    service._finish_finalization(p, call["id"], owner, False)
    if change == "note":
        assert service._begin_finalization(p, call["id"]), "Changed context still needs its own finalization"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SUPPORTED", "CONTRADICTED", "UNCERTAIN"])
async def test_live_commitment_stays_provisional_until_selective_review(
    workspace, app, clock, monkeypatch, verdict
):
    from test_grounding_review import review_response
    from test_live_memory import turn

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, seen = app.state.service, []

    class Provider:
        last_usage, last_request_id = {}, None
        last_provider, last_model = "Cerebras", "fixture"

        async def suggest(self, request, **kwargs):
            seen.append("generation")
            self.last_usage, self.last_request_id = {"cost": 0.001}, "promise-generation"
            segment = request["recent_dialogue"][-1]
            source = {"kind": "segment", "id": segment["segment_id"], "version": segment["revision"]}
            return {
                "state_version": request["state_version"],
                "context_fingerprint": request["context_fingerprint"],
                "guidance": None,
                "field_audit": [],
                "state_delta": {
                    "stage": "next_step",
                    "observations": [
                        {
                            "kind": "commitment",
                            "text": "The rep should reconnect about procurement.",
                            "state": "confirmed",
                            "sources": [source],
                            "replaces": [],
                            "quote": segment["text"],
                            "commitment": {
                                "kind": "callback",
                                "owner": "rep",
                                "due_at": "2026-10-20T21:00:00+00:00",
                                "needs_time_confirmation": False,
                                "verbatim_time_expression": "October 20, 2026 at 2 PM Pacific time",
                            },
                        }
                    ],
                    "rubric_items": [],
                    "tracker_matches": [],
                    "interpreted_segments": [source],
                },
            }

        async def review(self, request, **kwargs):
            seen.append("review")
            assert request["kind"] == "conversation"
            assert len(request["fields"]) <= 20
            self.last_usage, self.last_request_id = {"cost": 0.002}, "promise-review"
            return review_response(request, verdict=verdict)

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    turn(service, p, call, "promise", text="Please call me on October 20, 2026 at 2 PM Pacific time.")
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction(False) as s:
        memory = service._by_key(s, p, "conversation_states", call["id"]).data
        assert memory["observations"][0]["review_status"] == "pending"
        assert not service.lead_records(s, p, "followup_tasks", call["lead_id"])
    clock.now += timedelta(seconds=31)
    await service.run_jobs(p["tenant_id"], queues=["postcall"])
    assert seen == ["generation", "review"]
    with service.db.transaction(False) as s:
        tasks = service.lead_records(s, p, "followup_tasks", call["lead_id"])
        charges = [
            r.data
            for r in service.scoped(s, "usage_ledger", p)
            if r.data.get("work_kind") in {"live_coach", "conversation_review"}
        ]
        assert len(charges) == 2 and sum(r["cost_usd"] for r in charges) == pytest.approx(0.003)
        assert bool(tasks) is (verdict == "SUPPORTED")
        if tasks:
            assert len(tasks) == 1 and tasks[0].data["status"] == "open"
            assert tasks[0].data["due_at"] == "2026-10-20T21:00:00+00:00"


def test_observation_ids_do_not_depend_on_paraphrase_and_replay_is_a_noop():
    from uuid import UUID

    from app.coaching.context import merge_semantic_state, persistent_state

    state, segment, at = conversation_fixture()
    delta = {
        "stage": "discovery",
        "rundown_points": [
            {
                "text": "Procurement is delayed by approval steps.",
                "quote": segment["text"],
                "evidence_segment_ids": ["source"],
                "status": "confirmed",
            }
        ],
    }
    merged = merge_semantic_state(state, delta, [segment], request_id="fixed-request", at=at)
    observation = persistent_state(merged)["observations"][0]
    assert str(UUID(observation["id"])) == observation["id"]
    delta["rundown_points"][0]["text"] = "The buyer wants a faster procurement process."
    alternate = merge_semantic_state(state, delta, [segment], request_id="fixed-request", at=at)
    assert persistent_state(alternate)["observations"][0]["id"] == observation["id"]
    replay = merge_semantic_state(merged, delta, [segment], request_id="fixed-request", at=at)
    assert persistent_state(replay) == persistent_state(merged)


def test_conversation_global_bound_preserves_promises_and_reports_omissions():
    from app.coaching.context import merge_semantic_state, persistent_state

    state, segment, at = conversation_fixture()
    keys = ("needs", "business_impact", "objections", "answered_questions", "buying_signals", "commitments")
    for batch in range(12):
        delta = {"stage": "discovery"}
        for key in keys:
            delta[key] = [
                {
                    "text": f"Derived {key} observation number {batch}.",
                    "quote": segment["text"],
                    "evidence_segment_ids": ["source"],
                    "status": "open",
                }
            ]
        state = merge_semantic_state(state, delta, [segment], request_id=f"bounded-{batch}", at=at)
    stored = persistent_state(state)
    assert len(stored["observations"]) <= 48
    assert len(json.dumps(stored).encode()) <= 65536
    assert sum(i["kind"] == "commitment" for i in stored["observations"]) == 12
    assert stored["coverage"]["omitted_items"] >= 24
    assert segment["text"] not in json.dumps(stored)


def test_reviewed_checkpoint_keeps_early_lineage_for_correction_without_unbounded_direct_refs():
    from app.coaching.context import (
        invalidate_state,
        lineage_key,
        lineage_metadata,
        merge_semantic_state,
        segment_revisions,
    )

    state, segment, at = conversation_fixture()
    previous = None
    for i in range(12):
        source = {**segment, "segment_id": f"turn-{i}"}
        state = merge_semantic_state(
            state,
            {
                "stage": "discovery",
                "needs": [
                    {
                        "text": f"The review process has unresolved dependency {i}.",
                        "quote": "",
                        "status": "open",
                        "evidence_segment_ids": [source["segment_id"]],
                        "replaces": [previous] if previous else [],
                    }
                ],
            },
            [source],
            request_id=f"checkpoint-{i}",
            at=at,
        )
        item = state["observations"][-1]
        item.update(
            epistemic_status="model_reviewed", review_status="reviewed", review_receipt_id=f"review-{i}"
        )
        state["_lineage"][lineage_key(item["id"], item["version"])] = lineage_metadata(item)
        previous = item["id"]
        state["observations"] = [item]
        assert len(item["sources"]) <= 32 and item["lineage_depth"] <= 8
    assert set(segment_revisions(item, state["_lineage"])) == {f"turn-{i}" for i in range(12)}
    corrected = invalidate_state(state, "turn-0", 2, at=at, lineage=state["_lineage"])
    assert corrected["observations"][0]["state"] == "stale"
    again = invalidate_state(corrected, "turn-0", 2, at=at, lineage=state["_lineage"])
    assert again["version"] == corrected["version"], "Reading an already invalidated observation is a no-op"


@pytest.mark.asyncio
async def test_review_cannot_turn_unknown_speaker_into_an_automatic_callback(workspace, app, monkeypatch):
    from test_postcall_completion import finalize, setup_analysis
    from test_remediation_privacy import principal

    call, _ = setup_analysis(workspace, app, monkeypatch)
    service, p = app.state.service, principal(workspace)
    service._receive_transcript(
        p,
        call["id"],
        {
            "segment_id": "s",
            "revision": 2,
            "side": "unknown",
            "is_final": True,
            "text": "Please call me on October 20, 2026 at 2 PM Pacific time. The speaker attribution is unclear.",
        },
        None,
        True,
    )
    await finalize(service, p, call["id"])
    with service.db.transaction(False) as s:
        tasks = service.lead_records(s, p, "followup_tasks", call["lead_id"])
        assert tasks and all(t.data["status"] == "needs_confirmation" for t in tasks)
        assert not service.required(s, "leads", call["lead_id"], p).data.get("callback_due_at")


def test_observed_approach_needs_rep_evidence_not_only_a_suggestion():
    from app.coaching.context import merge_semantic_state
    from app.coaching.engine import CardRejected

    state, segment, at = conversation_fixture()
    delta = {
        "stage": "discovery",
        "observations": [
            {
                "kind": "approach",
                "text": "Approval complexity",
                "approach": "Clarify who approves",
                "state": "confirmed",
                "sources": [{"kind": "segment", "id": "source", "version": 1}],
                "replaces": [],
                "quote": "",
                "commitment": None,
            }
        ],
        "rubric_items": [],
        "tracker_matches": [],
        "interpreted_segments": [],
    }
    with pytest.raises(CardRejected, match="rep"):
        merge_semantic_state(state, delta, [segment], request_id="unsupported-use", at=at)


@pytest.mark.parametrize("replacement", ["foreign", "forward"])
def test_replacement_requires_an_existing_scoped_observation(replacement):
    from app.coaching.context import merge_semantic_state
    from app.coaching.engine import CardRejected

    state, segment, at = conversation_fixture()
    delta = {
        "stage": "discovery",
        "needs": [
            {
                "text": "Approval delay remains unresolved.",
                "quote": segment["text"],
                "evidence_segment_ids": ["source"],
                "status": "open",
                "replaces": [replacement],
            }
        ],
    }
    with pytest.raises(CardRejected, match="replacement|reference"):
        merge_semantic_state(state, delta, [segment], request_id="invalid", at=at)


def test_v1_migration_preserves_ids_sources_and_human_notes_without_fabricated_review(workspace, app):
    from app.db import put
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        old = put(
            s,
            "conversation_states",
            p["tenant_id"],
            p["seller_id"],
            {
                "privacy_contract": 1,
                "call_id": call["id"],
                "version": 4,
                "stage": "discovery",
                "needs": [
                    {
                        "id": "existing-interpretation",
                        "text": "Procurement needs less delay.",
                        "status": "open",
                        "evidence_segment_ids": ["early"],
                        "evidence_revisions": {"early": 3},
                    }
                ],
            },
            key=call["id"],
            now=service.clock(),
        )
        identity, version = old.id, old.version
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "privacy_contract": 1,
                "human_text": "Keep my correction.",
                "status": "human_confirmed",
                "notes": {"summary": "", "transcript_version": 4},
            },
            key=call["id"],
            now=service.clock(),
        )
        note_identity = note.id
    service.migrate_conversation_states()
    service.migrate_conversation_states()
    with service.db.transaction(False) as s:
        row = service._by_key(s, p, "conversation_states", call["id"])
        assert (row.id, row.version) == (identity, version)
        assert row.data["schema_version"] == 2 and "needs" not in row.data
        item = row.data["observations"][0]
        assert item["id"] == "existing-interpretation" and item["sources"][0]["version"] == 3
        assert item["review_receipt_id"] is None and item["epistemic_status"] == "model_interpreted"
        assert service._conversation_lineage(s, p, call["id"])
        note = service._by_key(s, p, "notes", call["id"])
        assert note.id == note_identity and note.data["human_text"] == "Keep my correction."


@pytest.mark.asyncio
async def test_no_provider_finalization_is_partial_and_never_fabricates_scores(
    workspace, app, carrier, monkeypatch
):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    monkeypatch.setattr(service, "carrier", lambda _: carrier)
    monkeypatch.setattr(service, "configured_coach", lambda _: (None, "unavailable"))
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    await service.analyze_postcall(p, call["id"])
    view = service.analysis(p, call["id"])
    assert view.get("finalization", {}).get("status") in {"partial", "unavailable"}
    assert all(item["score"] is None for item in view["scorecard"]["items"])
    assert not view["notes"]["commitments"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_at_hangup", [False, True])
async def test_early_context_survives_ttl(workspace, app, clock, carrier, monkeypatch, raw_at_hangup):
    call, p, requests, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    monkeypatch.setattr(service, "carrier", lambda _tenant: carrier)
    c, h = workspace["client"], workspace["headers"]
    began = clock.now
    old_roles = []

    def absent_legacy_inference(tenant, role, user=None):
        old_roles.append(role)
        return None

    monkeypatch.setattr(service, "inference", absent_legacy_inference)
    for minute, identity, text, paraphrase in [
        (
            1,
            "early",
            "Our approvals take weeks. Call me Tuesday at two Pacific.",
            "The buyer needs a shorter approval process and requested a Tuesday callback.",
        ),
        (
            6,
            "resolved",
            "Budget is now approved, that earlier concern is resolved.",
            "Funding is available following approval.",
        ),
        (
            9,
            "correction",
            "I meant Wednesday, not Tuesday. Still two Pacific.",
            "The requested follow-up moved to Wednesday afternoon Pacific.",
        ),
    ]:
        clock.now = began + timedelta(minutes=minute)
        changes["result"] = {
            "action": "NO_ACTION",
            "state_update": {
                "stage": "discovery",
                "rundown_points": [
                    {
                        "text": paraphrase,
                        "quote": text,
                        "evidence_segment_ids": [identity],
                        "status": "confirmed",
                    }
                ],
            },
        }
        response = c.post(
            f"/v1/calls/{call['id']}/transcript",
            headers=h,
            json={
                "segment_id": identity,
                "revision": 1,
                "is_final": True,
                "side": "remote",
                "text": text,
            },
        )
        assert response.status_code == 200, response.text
        clock.now += timedelta(milliseconds=301)
        await service.coach_turn(p, call["id"], urgent=True)
    clock.now = began + timedelta(minutes=12 if raw_at_hangup else 15)
    initial = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert "shorter approval" in json.dumps(initial["call_memory"])
    assert "early" not in {s["segment_id"] for s in initial["transcript"]}
    if not raw_at_hangup:
        assert initial["transcript"] == []
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    before = len(requests)
    await service.analyze_postcall(p, call["id"])
    assert len(requests) == before + 1, "Finalization must use the configured Cerebras conversation route"
    assert "postcall_extract" not in old_roles, "Do not invoke the separate recent-window extraction pipeline"
    final_request = requests[-1]
    assert "shorter approval" in json.dumps(final_request)
    assert "Wednesday" in json.dumps(final_request)
    assert "Our approvals take weeks" not in json.dumps(final_request)
    assert "Our approvals take weeks" not in persisted_content(service, p)
    result = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert result.get("finalization", {}).get("status") in {"ready", "partial"}
    # The same state version cannot create another finalizer charge.
    await service.analyze_postcall(p, call["id"])
    assert len(requests) == before + 1


@pytest.mark.asyncio
async def test_merge_correction_and_missing_source(workspace, app, clock, monkeypatch):
    call, p, requests, changes = setup(workspace, app, monkeypatch)
    from test_live_memory import turn

    service = app.state.service
    turn(service, p, call, "original")
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction() as s:
        state = service._by_key(s, p, "conversation_states", call["id"])
        before = dict(state.data)
    clock.now += timedelta(seconds=301)
    turn(service, p, call, "unrelated")
    changes["result"] = {"action": "NO_ACTION", "state_update": {"stage": "discovery"}}
    await service.coach_turn(p, call["id"], urgent=True)
    view = service.analysis(p, call["id"])
    assert "workflow constraint in part 1" in json.dumps(view["call_memory"])
    assert not view["transcript"][0]["segment_id"] == "original"
    # Current human text must survive a late interpretation/correction.
    with service.db.transaction() as s:
        note, _ = service._materialize_views_in(
            s, p, service.required(s, "call_attempts", call["id"], p), scorecard=False
        )
        update(note, human_text="Rep verified the current requirement directly.")
    turn(service, p, call, "original", revision=2, text="The original statement was incorrect.")
    corrected = service.analysis(p, call["id"])
    assert corrected["human_notes"] == "Rep verified the current requirement directly."
    assert corrected["call_memory"] != before
    assert corrected.get("source_conflicts"), (
        "Correction must expose an actionable conflict after evidence expires"
    )


@pytest.mark.asyncio
async def test_finalization_cannot_be_completed_by_an_expired_owner(workspace, app, clock, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    owner = service._begin_finalization(p, call["id"])
    clock.now += timedelta(seconds=31)
    service._finish_finalization(p, call["id"], owner, True)
    with service.db.transaction(False) as s:
        final = service._by_key(s, p, "conversation_states", call["id"]).data["finalization"]
        assert final.get("completed_fingerprint") is None
        assert final["status"] != "ready"


@pytest.mark.parametrize("failure", ["timeout", "unconfigured"])
@pytest.mark.asyncio
async def test_failed_finalization_can_retry_the_same_context_without_recharging_success(
    workspace, app, monkeypatch, failure
):
    from test_postcall_completion import ConversationProvider, finalize, setup_analysis
    from test_remediation_privacy import principal

    call, state = setup_analysis(workspace, app, monkeypatch)
    service, p = app.state.service, principal(workspace)

    class Failing(ConversationProvider):
        async def suggest(self, *args, **kwargs):
            raise TimeoutError("Synthetic lost response")

    monkeypatch.setattr(
        service,
        "configured_coach",
        lambda _: (Failing(state) if failure == "timeout" else None, "openrouter"),
    )
    await service.analyze_postcall(p, call["id"])
    before = service.analysis(p, call["id"])
    assert before["finalization"]["status"] == "partial"
    assert before["finalization"].get("completed_fingerprint") is None
    monkeypatch.setattr(service, "configured_coach", lambda _: (ConversationProvider(state), "openrouter"))
    await finalize(service, p, call["id"])
    result = service.analysis(p, call["id"])
    assert result["finalization"]["status"] == "ready"
    assert result["call"]["transcript_version"] == before["call"]["transcript_version"]
    assert state["requests"] == 2
    await service.analyze_postcall(p, call["id"])
    assert state["requests"] == 2, "An accepted identical finalization must not charge again"


@pytest.mark.asyncio
async def test_review_cannot_certify_a_changed_derived_source(workspace, app, clock, monkeypatch):
    from app.coaching.context import lineage_metadata, merge_semantic_state
    from app.grounding import accept_review
    from test_grounding_review import review_response

    service, call, p, _, first_owner, first_receipt = await pending_review(workspace, app, clock, monkeypatch)
    first_request = service._conversation_context(p, call["id"], "review")
    service._publish_state_review(p, call["id"], first_request, first_owner, first_receipt)
    with service.db.transaction() as s:
        row = service._by_key(s, p, "conversation_states", call["id"])
        state = row.data
        prior = state["observations"][0]
        old_id = prior["id"]
        delta = {
            "stage": "next_step",
            "observations": [
                {
                    "kind": "commitment",
                    "text": "A further evaluation discussion remains due.",
                    "state": "confirmed",
                    "sources": [{"kind": "observation", "id": old_id, "version": 1}],
                    "replaces": [],
                    "quote": "",
                    "commitment": None,
                }
            ],
            "rubric_items": [],
            "tracker_matches": [],
            "interpreted_segments": [],
        }
        state = merge_semantic_state(state, delta, [], request_id="derived-review", at=clock.now)
        service._store_conversation(s, p, service.required(s, "call_attempts", call["id"], p), state, row)
    request = service._conversation_context(p, call["id"], "review")
    owner, review = service._state_review_snapshot(p, call["id"], request)
    receipt = accept_review(review, review_response(review), "second-review")
    with service.db.transaction() as s:
        row = service._by_key(s, p, "conversation_states", call["id"])
        state = row.data
        prior = next(i for i in state["observations"] if i["id"] == old_id)
        prior.update(state="retracted", version=2, epistemic_status="human_confirmed")
        state["version"] += 1
        state["_lineage_updates"] = [lineage_metadata(prior)]
        service._store_conversation(s, p, service.required(s, "call_attempts", call["id"], p), state, row)
    service._publish_state_review(p, call["id"], request, owner, receipt)
    with service.db.transaction(False) as s:
        state = service._by_key(s, p, "conversation_states", call["id"]).data
        assert not any(i.get("review_receipt_id") == "second-review" for i in state["observations"])


def test_legacy_notes_without_live_state_migrate_without_losing_derived_or_human_context(workspace, app):
    from app.db import put
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {
                "privacy_contract": 1,
                "call_id": call["id"],
                "human_text": "Rep-owned follow-up notes.",
                "status": "human_confirmed",
                "notes": {
                    "summary": "Purchasing approval requires a further evaluation.",
                    "summary_evidence": ["early"],
                    "transcript_version": 1,
                    "problems": [],
                    "objections": [],
                    "commitments": [],
                },
                "observations": [
                    {
                        "id": "legacy-observation",
                        "version": 3,
                        "concept": "Procurement approval",
                        "approach": "Clarify approval steps",
                        "source_revisions": {"early": 2},
                        "evidence": ["early"],
                        "status": "active",
                        "review_status": "model_reviewed",
                        "review_request_id": "existing-receipt",
                        "observed_at": service.clock().isoformat(),
                    }
                ],
            },
            key=call["id"],
            now=service.clock(),
        )
        identity, version = note.id, note.version
    service.migrate_conversation_states()
    service.migrate_conversation_states()
    view = service.analysis(p, call["id"])
    assert "further evaluation" in view["notes"]["summary"]
    assert view["human_notes"] == "Rep-owned follow-up notes."
    item = view["observations"][0]
    assert (item["id"], item["version"], item["review_request_id"]) == (
        "legacy-observation",
        3,
        "existing-receipt",
    )
    assert item["source_revisions"] == {"early": 2}
    with service.db.transaction(False) as s:
        note = service._by_key(s, p, "notes", call["id"])
        assert note.id == identity and note.version >= version
        assert "summary" not in note.data["notes"] and "observations" not in note.data
        state = service._by_key(s, p, "conversation_states", call["id"]).data
        assert sum(i["kind"] == "approach" for i in state["observations"]) == 1
        summary = next(i for i in state["observations"] if i["kind"] == "rundown")
        assert summary["epistemic_status"] == "model_interpreted"
        assert summary["review_receipt_id"] is None


def test_legacy_call_without_transcript_version_migrates_as_unknown_provenance(workspace, app):
    from app.db import put
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        record = service.required(s, "call_attempts", call["id"], p)
        record.data = {k: v for k, v in record.data.items() if k != "transcript_version"}
        note = put(
            s,
            "notes",
            p["tenant_id"],
            p["seller_id"],
            {
                "privacy_contract": 1,
                "call_id": call["id"],
                "human_text": "Preserve the rep's decision.",
                "notes": {
                    "summary": "Approval requires further discussion.",
                    "summary_evidence": ["unknown"],
                },
            },
            key=call["id"],
            now=service.clock(),
        )
        note_id = note.id
    service.migrate_conversation_states()
    service.migrate_conversation_states()
    with service.db.transaction(False) as s:
        note = service.required(s, "notes", note_id, p)
        assert note.data["human_text"] == "Preserve the rep's decision."
        state = service._by_key(s, p, "conversation_states", call["id"]).data
        summary = next(i for i in state["observations"] if i["kind"] == "rundown")
        assert summary["epistemic_status"] == "model_interpreted"
        assert summary["review_receipt_id"] is None
        assert not any(ref.get("version") for ref in summary["sources"] if ref["kind"] == "segment")


def test_retained_source_correction_invalidates_every_dependent_observation():
    from app.coaching.context import invalidate_state, lineage_metadata, merge_semantic_state

    state, segment, at = conversation_fixture()
    delta = {
        "stage": "discovery",
        "rundown_points": [
            {
                "text": "Approval steps add delay.",
                "status": "confirmed",
                "evidence_segment_ids": ["source"],
                "quote": "",
            }
        ],
    }
    state = merge_semantic_state(state, delta, [segment], request_id="original", at=at)
    original = state["observations"][0]
    original.update(epistemic_status="human_confirmed", review_status="reviewed")
    old_id = original["id"]
    child = {
        "stage": "discovery",
        "observations": [
            {
                "kind": "need",
                "text": "Procurement could benefit from clearer review steps.",
                "state": "confirmed",
                "sources": [{"kind": "observation", "id": old_id, "version": 1}],
                "replaces": [],
                "quote": "",
                "commitment": None,
            }
        ],
        "rubric_items": [],
        "tracker_matches": [],
        "interpreted_segments": [],
    }
    state = merge_semantic_state(state, child, [], request_id="dependent", at=at)
    original = next(i for i in state["observations"] if i["id"] == old_id)
    original.update(state="retracted", version=2)
    state["_lineage"][f"{old_id}:2"] = lineage_metadata(original)
    invalidated = invalidate_state(state, old_id, 2, at=at, source_kind="observation")
    assert next(i for i in invalidated["observations"] if i["id"] != old_id)["state"] == "stale"
    assert next(i for i in invalidated["observations"] if i["id"] == old_id)["state"] == "retracted"


def test_migration_resumes_after_a_committed_page_and_preserves_versions(workspace, app, monkeypatch):
    from app.db import put
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    identities = []
    with service.db.transaction() as s:
        template = service.required(s, "call_attempts", call["id"], p).data
        for _index in range(501):
            copied = put(s, "call_attempts", p["tenant_id"], p["seller_id"], template, now=service.clock())
            row = put(
                s,
                "conversation_states",
                p["tenant_id"],
                p["seller_id"],
                {"call_id": copied.id, "privacy_contract": 1, "version": 0},
                key=copied.id,
                now=service.clock(),
            )
            identities.append((row.id, row.version))
    store, count = service._store_conversation, 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 501:
            raise OSError("Synthetic migration interruption")
        return store(*args, **kwargs)

    monkeypatch.setattr(service, "_store_conversation", interrupted)
    with pytest.raises(OSError):
        service.migrate_conversation_states()
    with service.db.transaction(False) as s:
        assert (
            sum(r.data.get("schema_version") == 2 for r in service.scoped(s, "conversation_states", p)) == 500
        )
    monkeypatch.setattr(service, "_store_conversation", store)
    service.migrate_conversation_states()
    with service.db.transaction(False) as s:
        rows = service.scoped(s, "conversation_states", p)
        assert sorted((r.id, r.version) for r in rows) == sorted(identities)
        assert all(r.data["schema_version"] == 2 for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("human_edit", [False, True])
async def test_human_observation_retraction_invalidates_the_existing_brief(
    workspace, app, monkeypatch, human_edit
):
    from app.coaching.context import merge_semantic_state
    from test_conversation_learning import observed_call
    from test_postcall_completion import finalize
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call, _ = observed_call(workspace, app, monkeypatch)
    await finalize(service, p, call["id"])
    original = service.analysis(p, call["id"])["observations"][0]
    request = service._conversation_context(p, call["id"], "review")
    with service.db.transaction() as s:
        row = service._by_key(s, p, "conversation_states", call["id"])
        state = merge_semantic_state(
            row.data,
            {
                "stage": "discovery",
                "observations": [
                    {
                        "kind": "need",
                        "text": "Clarifying procurement remains useful.",
                        "state": "confirmed",
                        "sources": [
                            {"kind": "observation", "id": original["id"], "version": original["version"]}
                        ],
                        "replaces": [],
                        "quote": "",
                        "commitment": None,
                    },
                    {
                        "kind": "commitment",
                        "text": "Reconnect about the approval process.",
                        "state": "confirmed",
                        "sources": [
                            {"kind": "observation", "id": original["id"], "version": original["version"]},
                            {"kind": "segment", "id": "s", "version": 1},
                        ],
                        "replaces": [],
                        "quote": "",
                        "commitment": {
                            "kind": "callback",
                            "owner": "rep",
                            "due_at": "2026-10-20T21:00:00+00:00",
                            "needs_time_confirmation": False,
                            "verbatim_time_expression": "October 20, 2026 at 2 PM Pacific time",
                        },
                    },
                ],
                "rubric_items": [],
                "tracker_matches": [],
                "interpreted_segments": [],
            },
            request["recent_dialogue"],
            request_id="prior-brief",
            at=service.clock(),
        )
        service._store_conversation(s, p, service.required(s, "call_attempts", call["id"], p), state, row)
    await service.review_conversation(p, call["id"])
    with service.db.transaction() as s:
        task = service.lead_records(s, p, "followup_tasks", call["lead_id"])[0]
        assert task.data["status"] == "open"
        task_id = task.id
        if human_edit:
            update(task, human_edited=True, note="My independent follow-up decision.")
    response = workspace["client"].post(
        f"/v1/calls/{call['id']}/observations/{original['id']}/review",
        headers=workspace["headers"],
        json={
            "expected_version": original["version"],
            "decision": "retract",
            "reason": "The suggested approach was not used.",
        },
    )
    assert response.status_code == 200, response.text
    view = service.analysis(p, call["id"])
    assert not view["call_memory"]["needs"]
    assert view["source_conflicts"]
    with service.db.transaction(False) as s:
        task = service.required(s, "followup_tasks", task_id, p)
        assert task.data["source_stale"] and task.data["review_required"]
        # A human retraction closes untouched inferred work; it never holds outreach waiting.
        assert task.data["status"] == ("open" if human_edit else "cancelled")
        if human_edit:
            assert task.data["note"] == "My independent follow-up decision."
        else:
            assert not service.required(s, "leads", call["lead_id"], p).data.get("callback_due_at")


def test_finalizer_rejects_a_different_context_fingerprint(workspace, app, monkeypatch):
    from test_live_memory import turn

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "fingerprint")
    service.analysis(p, call["id"])
    owner = service._begin_finalization(p, call["id"])
    request = service._conversation_context(p, call["id"], "finalize")
    with pytest.raises(ValueError, match="context"):
        service._merge_finalization(
            p,
            call["id"],
            owner,
            request,
            {
                "guidance": None,
                "field_audit": [],
                "state_delta": None,
                "state_version": request["state_version"],
                "context_fingerprint": "different-input",
            },
        )


def counted_transactions(service, monkeypatch):
    """Records True for every independent write transaction the service opens."""
    from contextlib import contextmanager

    original, seen = service.db.transaction, []

    @contextmanager
    def observed(write=True, **kwargs):
        seen.append(bool(write) and kwargs.get("session") is None)
        with original(write, **kwargs) as s:
            yield s

    monkeypatch.setattr(service.db, "transaction", observed)
    return seen


def postcall_jobs(service, call_id):
    from app.db import Job
    from sqlalchemy import select

    with service.db.transaction(False) as s:
        return [
            (j.key, j.payload.get("kind"), j.status)
            for j in s.scalars(select(Job).where(Job.queue == "postcall"))
            if j.payload.get("call_id") == call_id
        ]


@pytest.mark.asyncio
async def test_analysis_get_writes_nothing_and_never_changes_status(workspace, app, carrier, monkeypatch):
    from test_live_memory import turn

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, c, h = app.state.service, workspace["client"], workspace["headers"]
    monkeypatch.setattr(service, "carrier", lambda _: carrier)
    turn(service, p, call, "read-only")
    writes = counted_transactions(service, monkeypatch)
    first = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert True not in writes, "A first read must not materialize notes or scorecards"
    assert (first["notes_version"], first["scorecard_version"], first["status"]) == (0, 0, "draft")
    assert first["derived_state_revision"] == first["call_memory"]["version"]
    with service.db.transaction(False) as s:
        assert not any(service._by_key(s, p, kind, call["id"]) for kind in ("notes", "scorecards"))
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    await service.analyze_postcall(p, call["id"])
    finalized = service.analysis(p, call["id"])
    assert finalized["status"] == "ai_finalized" and finalized["notes_version"] >= 1
    with service.db.transaction() as s:
        record = service._by_key(s, p, "conversation_states", call["id"])
        state = dict(record.data, version=record.data["version"] + 1)
        service._store_conversation(s, p, service.required(s, "call_attempts", call["id"], p), state, record)
        stored = service._by_key(s, p, "scorecards", call["id"]).data["items"]
        assert all("explanation" not in item for item in stored), "Narrative lives once, in the state"
    writes.clear()
    drifted = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert True not in writes, "Revision drift is reported, never repaired, by a read"
    assert drifted["status"] == "ai_finalized", "A read never demotes a finalized rundown"
    assert drifted["derived_state_revision"] == finalized["derived_state_revision"] + 1
    assert (drifted["notes_version"], drifted["scorecard_version"]) == (
        finalized["notes_version"],
        finalized["scorecard_version"],
    )


def test_note_edit_creates_its_row_in_one_write_without_a_preceding_read_refresh(workspace, app, monkeypatch):
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    view = service.analysis(p, call["id"])
    writes = counted_transactions(service, monkeypatch)
    saved = service.edit_notes(p, call["id"], view["notes_version"], "Rep-confirmed purchasing owner.")
    assert writes.count(True) == 1, "The edit is one write; the GET never materializes for it"
    assert saved["human_notes"] == "Rep-confirmed purchasing owner." and saved["notes_version"] >= 1
    with pytest.raises(Exception) as stale:
        service.edit_notes(p, call["id"], view["notes_version"], "A second editor's older view.")
    assert getattr(stale.value, "status_code", None) == 409
    assert service.analysis(p, call["id"])["human_notes"] == "Rep-confirmed purchasing owner."


def test_owned_finalize_triggers_share_one_signature_keyed_job_with_an_explicit_kind(workspace, app):
    from conftest import disposition_body, start
    from test_call_rubrics import choose
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    call = start(workspace)
    service._receive_transcript(
        p,
        call["id"],
        {"segment_id": "trigger", "revision": 1, "side": "remote", "is_final": True, "text": "Hello."},
        None,
        False,
    )
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call["id"], p)
        first = service.enqueue_finalize(s, p, row, "hangup")
        second = service.enqueue_finalize(s, p, row, "webhook")
    assert first["id"] == second["id"], "Duplicate triggers for one input are one job"
    assert first["key"].startswith(f"conversation-finalize:{call['id']}:")
    assert first["payload"]["kind"] == "conversation_finalize"
    saved = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="right_party"),
    )
    assert saved.status_code == 200, saved.text
    current = c.get(f"/v1/calls/{call['id']}", headers=h).json()
    assert choose(workspace, current, "follow_up").status_code == 200
    jobs = postcall_jobs(service, call["id"])
    assert not [key for key, *_ in jobs if key.startswith(("wrapup:", "rubric:"))]
    owned = [(key, kind) for key, kind, _ in jobs if key.startswith("conversation-finalize:")]
    assert len(owned) == 3 and {kind for _, kind in owned} == {"conversation_finalize"}


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["postcall", "research", "scripts", "ml"])
async def test_unknown_job_kind_fails_closed_instead_of_running_a_default_handler(
    workspace, app, monkeypatch, lane
):
    from test_remediation_privacy import principal

    service, p, ran = app.state.service, principal(workspace), []

    async def forbidden(*args, **kwargs):
        ran.append(args)

    for name in ("analyze_postcall", "review_conversation", "prepare", "train_model_job"):
        monkeypatch.setattr(service, name, forbidden)
    job = service.jobs.enqueue(
        lane,
        {"kind": "invented_work", "call_id": "x", "lead_ids": ["x"], "seller_id": p["seller_id"]},
        "invented-work:" + lane,
        tenant_id=p["tenant_id"],
    )
    result = await service.run_jobs(p["tenant_id"], [lane])
    assert not ran, "A kind without a registered handler must not fall through to another one"
    assert [e["error_type"] for e in result["errors"] if e["job_id"] == job["id"]] == ["UnknownJobKind"]
    assert service.jobs.get(job["id"])["status"] != "completed"


@pytest.mark.asyncio
async def test_retired_retry_plan_jobs_drain_as_no_ops(workspace, app):
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    assert not hasattr(service, "refresh_schedule_plans"), "Per-lead schedule plans are retired"
    job = service.jobs.enqueue(
        "ml",
        {"kind": "retry_plan_refresh", "campaign_id": workspace["campaign_id"], "seller_id": p["seller_id"]},
        "retired-retry-plan",
        tenant_id=p["tenant_id"],
    )
    result = await service.run_jobs(p["tenant_id"], ["ml"])
    assert service.jobs.get(job["id"])["status"] == "completed" and not result["errors"]


@pytest.mark.asyncio
async def test_no_provider_publishes_one_partial_rundown_without_warnings_or_ledger_churn(
    workspace, app, carrier, monkeypatch, caplog
):
    import logging

    from test_live_memory import turn

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, c, h = app.state.service, workspace["client"], workspace["headers"]
    monkeypatch.setattr(service, "carrier", lambda _: carrier)
    turn(service, p, call, "unconfigured")
    monkeypatch.setattr(service, "configured_coach", lambda _: (None, "unavailable"))
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    ledger, contexts = [], []
    for name in ("reserve_optional_budget", "release_unsubmitted_budget"):
        monkeypatch.setattr(service, name, lambda *args, name=name, **kwargs: ledger.append(name))
    original = service._conversation_context
    monkeypatch.setattr(
        service, "_conversation_context", lambda *args: contexts.append(args) or original(*args)
    )
    with caplog.at_level(logging.WARNING):
        await service.analyze_postcall(p, call["id"])
        view = service.analysis(p, call["id"])
        assert view["finalization"]["status"] == "partial"
        assert view["status"] == "ai_finalized" and view["notes_version"] >= 1
        writes = counted_transactions(service, monkeypatch)
        for _ in range(3):
            await service.analyze_postcall(p, call["id"])
    assert True not in writes, "An unchanged unavailable rundown is published exactly once"
    assert not ledger and not contexts, "No provider means no request context and no budget ledger"
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    final = service.analysis(p, call["id"])["finalization"]
    assert final.get("completed_fingerprint") is None, "Configuring AI later can still finalize"


@pytest.mark.asyncio
async def test_finalization_queues_its_one_review_instead_of_also_running_it_inline(
    workspace, app, monkeypatch
):
    from test_postcall_completion import setup_analysis
    from test_remediation_privacy import principal

    call, state = setup_analysis(workspace, app, monkeypatch)
    service, p = app.state.service, principal(workspace)
    await service.analyze_postcall(p, call["id"])
    assert state["requests"] == 1, "The finalizer generates; selective review is the queued job"
    queued = [j for j in postcall_jobs(service, call["id"]) if j[1] == "conversation_review"]
    assert [(key, status) for key, _, status in queued] == [(f"conversation-review:{call['id']}", "queued")]
    assert service.analysis(p, call["id"])["finalization"]["status"] == "partial"
    await service.run_jobs(p["tenant_id"], ["postcall"])
    assert state["requests"] == 2
    view = service.analysis(p, call["id"])
    assert view["finalization"]["status"] == "ready"
    await service.run_jobs(p["tenant_id"], ["postcall"])
    await service.analyze_postcall(p, call["id"])
    assert state["requests"] == 2, "A reviewed, finalized input is complete: no second charge"


@pytest.mark.asyncio
async def test_finalizer_request_is_read_only_with_every_live_chunk_and_human_task_corrections(
    workspace, app, monkeypatch
):
    from app.db import put
    from test_live_memory import turn
    from test_postcall_completion import ConversationProvider, analysis_body

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    for index in range(40):
        turn(service, p, call, f"chunk-{index:02d}")
    with service.db.transaction() as s:
        for index in range(6):
            promise = put(
                s,
                "commitments",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": call["lead_id"],
                    "call_id": call["id"],
                    "kind": "followup",
                    "action": f"Send the requested material {index}",
                    "status": "confirmed",
                },
                now=service.clock(),
            )
        put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": call["lead_id"],
                "call_id": call["id"],
                "commitment_id": promise.id,
                "kind": "followup",
                "source": "inferred",
                "status": "cancelled",
                "human_edited": True,
                "note": "The rep withdrew this follow-up.",
            },
            key="commitment:" + promise.id,
            now=service.clock(),
        )
    writes = counted_transactions(service, monkeypatch)
    request = service._conversation_context(p, call["id"], "finalize")
    assert True not in writes, "Building the finalizer input must not write the live state"
    assert [r["segment_id"] for r in request["recent_dialogue"]] == [f"chunk-{i:02d}" for i in range(32)]
    assert len(request["prior_commitments"]) == 6, "Every current promise, not the newest five"
    corrected = [t for t in request["followup_tasks"] if t["human_edited"]]
    assert corrected and corrected[0]["status"] == "cancelled"
    assert corrected[0]["note"] == "The rep withdrew this follow-up."
    # A finalizer that interprets one bounded page leaves the remaining live chunks queued.
    body = {**analysis_body(), "summary_evidence": ["chunk-00"], "commitments": []}
    monkeypatch.setattr(
        service,
        "configured_coach",
        lambda *args: (ConversationProvider({"body": body, "requests": 0}), "openrouter"),
    )
    await service.analyze_postcall(p, call["id"])
    rest = service._conversation_context(p, call["id"], "finalize")
    assert [r["segment_id"] for r in rest["recent_dialogue"]] == [f"chunk-{i:02d}" for i in range(32, 40)]
    assert service.analysis(p, call["id"])["finalization"]["status"] == "pending"
    assert [
        k
        for k, kind, s in postcall_jobs(service, call["id"])
        if s == "queued" and kind == "conversation_finalize"
    ]


def test_late_final_segment_after_hangup_uses_the_single_explicit_finalize_enqueue(workspace, app):
    from conftest import start
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    c, h = workspace["client"], workspace["headers"]
    call = start(workspace)
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    service._receive_transcript(
        p,
        call["id"],
        {"segment_id": "late", "revision": 1, "side": "remote", "is_final": True, "text": "One more thing."},
        None,
        True,  # queue follow-on work, as the HTTP/webhook ingress does
    )
    finalize = [(key, kind) for key, kind, _ in postcall_jobs(service, call["id"]) if "finalize" in key]
    assert finalize, "A late final segment after hangup must queue finalization"
    assert {kind for _, kind in finalize} == {"conversation_finalize"}, finalize
