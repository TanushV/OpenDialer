"""A25/A27: quiet, incremental call memory before the rolling raw buffer expires."""

from copy import deepcopy
from datetime import timedelta

import pytest
from app.coaching.context import build_context, merge_semantic_state, persistent_state
from app.db import get, update
from conftest import start
from test_remediation_coach import advice
from test_remediation_privacy import persisted_content, principal


def setup(workspace, app, monkeypatch):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    requests, changes = [], {}

    class Provider:
        last_usage, last_request_id = {}, None
        last_provider, last_model = "Cerebras", "fixture"

        async def suggest(self, request, **kwargs):
            requests.append(deepcopy(request))
            self.last_usage = {"cost": 0.001}
            self.last_request_id = f"memory-{len(requests)}"
            if not request["recent_dialogue"]:
                return {
                    "action": "NO_ACTION",
                    "state_version": request["state_version"],
                    "state_update": None,
                }
            latest = request["recent_dialogue"][-1]
            result = advice(request["state_version"], latest["segment_id"])
            result["state_update"] = {
                "stage": "discovery",
                "rundown_points": [
                    {
                        "text": f"The buyer described a workflow constraint in part {len(requests)}.",
                        "quote": latest["text"],
                        "evidence_segment_ids": [latest["segment_id"]],
                        "status": "confirmed",
                    }
                ],
            }
            if changes.get("during"):
                changes["during"]()
            result.update(changes.get("result", {}))
            return result

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    return call, p, requests, changes


def turn(service, p, call, identity, **changes):
    service._receive_transcript(
        p,
        call["id"],
        {
            "segment_id": identity,
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": f"At this point in our discussion, the workflow concern is described by example {identity} and several teams need to be involved.",
            **changes,
        },
        None,
        True,
    )


@pytest.mark.asyncio
async def test_quiet_interval_defers_the_final_turn_instead_of_losing_it(workspace, app, clock, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "opening")
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    clock.now += timedelta(seconds=1)
    turn(service, p, call, "last")
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 2, (
        "The last coalesced turn needs one deferred interpretation without another utterance"
    )
    assert requests[-1]["recent_dialogue"][-1]["segment_id"] == "last"


@pytest.mark.asyncio
async def test_muting_tips_keeps_context_current_without_displaying_a_card(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), coach_preferences={"muted": True})
    turn(service, p, call, "muted")
    result = await service.coach_turn(p, call["id"])
    assert len(requests) == 1 and requests[0]["automatic_tips_muted"] is True
    assert result["action"] == "NO_ACTION"
    with service.db.transaction(False) as s:
        state = service._by_key(s, p, "conversation_states", call["id"])
        assert state.data["rundown_points"]
        assert not service.scoped(s, "coach_events", p)


@pytest.mark.asyncio
async def test_backlog_includes_old_uninterpreted_turns_as_well_as_the_latest(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    for i in range(40):
        turn(service, p, call, f"turn-{i:03}")
    await service.coach_turn(p, call["id"])
    ids = {item["segment_id"] for item in requests[0]["recent_dialogue"]}
    assert "turn-000" in ids and "turn-039" in ids
    assert len(ids) <= 32, "Bound live input rather than growing a full transcript prompt"


@pytest.mark.asyncio
async def test_observed_turn_is_not_billed_again_merely_because_worker_runs(
    workspace, app, clock, monkeypatch
):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "once")
    await service.coach_turn(p, call["id"])
    clock.now += timedelta(seconds=20)
    await service.coach_turn(p, call["id"])
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_derived_rundown_survives_a_long_call_without_raw_storage(workspace, app, clock, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    for i in range(4):
        turn(service, p, call, f"part-{i}")
        await service.coach_turn(p, call["id"])
        clock.now += timedelta(seconds=240)
    clock.now += timedelta(seconds=61)
    result = service.analysis(p, call["id"])
    assert not result["transcript"]
    assert len(result["call_memory"]["rundown_points"]) == 4
    assert result["interpretation_coverage"]["interpreted_segments"] == 4
    assert result["interpretation_coverage"]["unavailable_segments"] == 0
    assert "workflow constraint in part 1" in result["notes"]["summary"]
    assert "At this point in our discussion" not in persisted_content(service, p)
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_expired_model_response_cannot_publish_retained_interpretation(
    workspace, app, clock, monkeypatch
):
    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "expiry")
    changes["during"] = lambda: setattr(clock, "now", clock.now + timedelta(seconds=301))
    await service.coach_turn(p, call["id"])
    result = service.analysis(p, call["id"])
    assert not result["call_memory"].get("rundown_points")
    assert result["interpretation_coverage"]["unavailable_segments"] == 1


def memory_source(identity="new", revision=1):
    return {
        "segment_id": identity,
        "revision": revision,
        "is_final": True,
        "side": "remote",
        "text": "That earlier deadline no longer applies because we now have approval to discuss the next stage.",
    }


def test_new_evidence_can_resolve_prior_open_memory_without_leaving_contradictory_duplicates():
    previous = {
        "version": 1,
        "stage": "discovery",
        "trigger": "conversation",
        "objections": [
            {
                "id": "prior-objection",
                "text": "Approval was still outstanding.",
                "status": "open",
                "evidence_segment_ids": ["old"],
                "evidence_revisions": {"old": 1},
            }
        ],
    }
    result = merge_semantic_state(
        previous,
        {
            "stage": "next_step",
            "objections": [
                {
                    "text": "The approval constraint has been lifted.",
                    "quote": memory_source()["text"],
                    "status": "resolved",
                    "evidence_segment_ids": ["new"],
                    "replaces": ["prior-objection"],
                }
            ],
        },
        [memory_source()],
    )
    assert len(result["objections"]) == 1
    assert result["objections"][0]["status"] == "resolved"
    assert result["objections"][0]["evidence_revisions"] == {"old": 1, "new": 1}
    # A correction to either supporting source invalidates the merged interpretation.
    corrected = build_context([memory_source("old", 2)], version=3, facts=[], commitments=[], prior=result)
    assert not corrected["objections"]


def test_unknown_memory_replacement_is_rejected():
    with pytest.raises(ValueError):
        merge_semantic_state(
            {"stage": "discovery"},
            {
                "stage": "discovery",
                "needs": [
                    {
                        "text": "A shorter approval process may help.",
                        "quote": memory_source()["text"],
                        "status": "open",
                        "evidence_segment_ids": ["new"],
                        "replaces": ["another-call-item"],
                    }
                ],
            },
            [memory_source()],
        )


def test_authoritative_commitments_are_not_copied_into_generated_call_memory():
    state = build_context(
        [],
        version=1,
        facts=[],
        commitments=[{"id": "prior-task", "action": "Return call", "status": "confirmed"}],
    )
    assert not persistent_state(state)["commitments"], (
        "Prior tasks already have a current authoritative context field; copying them keeps stale promises"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"evidence_segment_ids": "new"},
        {"evidence_segment_ids": [{}]},
        {"quote": 123},
        {"text": {"value": "Not a string"}},
        {"status": {}},
        {"replaces": [None]},
    ],
)
def test_malformed_memory_is_rejected_without_a_worker_crash(change):
    item = {
        "text": "The buyer has a workflow constraint.",
        "quote": memory_source()["text"],
        "evidence_segment_ids": ["new"],
        "status": "open",
        **change,
    }
    with pytest.raises(ValueError):
        merge_semantic_state(
            {"stage": "discovery"}, {"stage": "discovery", "needs": [item]}, [memory_source()]
        )


def test_memory_bounds_keep_unresolved_context_and_report_compression():
    items = [
        {
            "id": str(i),
            "text": f"Observed concern {i}",
            "status": "open" if i == 0 else "resolved",
            "evidence_segment_ids": [str(i)],
            "evidence_revisions": {str(i): 1},
        }
        for i in range(20)
    ]
    stored = persistent_state(
        {"version": 1, "stage": "discovery", "trigger": "conversation", "objections": items}
    )
    assert len(stored["objections"]) <= 12
    assert any(item["id"] == "0" for item in stored["objections"])
    assert stored["memory_omitted_items"] == 8


def test_observable_timing_survives_raw_expiry_without_conversation_text(workspace, app, clock):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    turn(
        service,
        p,
        call,
        "rep-question",
        side="rep",
        start_ms=0,
        end_ms=5000,
        text="Which steps in your approval process would be useful for us to understand together?",
    )
    turn(service, p, call, "reply", start_ms=5000, end_ms=20000)
    before = service.analysis(p, call["id"])["metrics"]
    clock.now += timedelta(seconds=301)
    after = service.analysis(p, call["id"])["metrics"]
    assert after["talk_ms"] == before["talk_ms"] == {"rep": 5000, "remote": 15000}
    assert after["question_count"] == before["question_count"] == 1
    assert after["timed_segments"] == 2


@pytest.mark.asyncio
async def test_new_turn_during_inference_keeps_valid_memory_but_not_stale_advice(workspace, app, monkeypatch):
    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "first")
    changes["during"] = lambda: turn(service, p, call, "later")
    result = await service.coach_turn(p, call["id"])
    assert result["action"] == "NO_ACTION"
    analysis = service.analysis(p, call["id"])
    assert analysis["call_memory"]["rundown_points"]
    assert analysis["interpretation_coverage"]["interpreted_segments"] == 1
    assert analysis["interpretation_coverage"]["pending_segments"] == 1


@pytest.mark.asyncio
async def test_source_revision_during_inference_rejects_memory(workspace, app, monkeypatch):
    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "first")
    changes["during"] = lambda: turn(
        service, p, call, "first", revision=2, text="That process was not discussed."
    )
    await service.coach_turn(p, call["id"])
    analysis = service.analysis(p, call["id"])
    assert not analysis["call_memory"].get("rundown_points")
    assert analysis["interpretation_coverage"]["interpreted_segments"] == 0


@pytest.mark.asyncio
async def test_slow_budget_admission_does_not_send_expired_raw_text(workspace, app, clock, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "admission")
    original = service.reserve_optional_budget

    def delayed(*args, **kwargs):
        allowed = original(*args, **kwargs)
        clock.now += timedelta(seconds=301)
        return allowed

    monkeypatch.setattr(service, "reserve_optional_budget", delayed)
    await service.coach_turn(p, call["id"])
    assert requests == []
    with service.db.transaction(False) as s:
        entries = service.scoped(s, "usage_ledger", p)
        holds = [item for item in entries if item.data.get("work_kind") == "live_coach"]
        assert holds and all(item.data["status"] == "released" for item in holds)


@pytest.mark.asyncio
async def test_help_hotkey_bypasses_muting_without_requiring_a_typed_question(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), coach_preferences={"muted": True})
    turn(service, p, call, "hotkey")
    result = await service.coach_turn(p, call["id"], urgent=True)
    assert result["action"] == "SHOW_CARD"


@pytest.mark.asyncio
async def test_postcall_meaning_review_also_records_interpreted_source_coverage(
    workspace, app, clock, monkeypatch
):
    from test_postcall_completion import setup_analysis

    call, _ = setup_analysis(workspace, app, monkeypatch)
    service, p = app.state.service, principal(workspace)
    await service.analyze_postcall(p, call["id"])
    clock.now += timedelta(seconds=301)
    coverage = service.analysis(p, call["id"])["interpretation_coverage"]
    assert coverage["interpreted_segments"] == 1
    assert coverage["unavailable_segments"] == 0


@pytest.mark.asyncio
async def test_bounded_backlog_is_drained_without_an_additional_turn(workspace, app, clock, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    for i in range(40):
        turn(service, p, call, f"backlog-{i:03}")
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    clock.now += timedelta(seconds=10)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    result = service.analysis(p, call["id"])["interpretation_coverage"]
    assert result["interpreted_segments"] == 40 and result["pending_segments"] == 0
    assert len(requests) == 2, (
        "Bounded catch-up should consume the remaining chunks, not pay for each queued event"
    )


@pytest.mark.asyncio
async def test_rejected_advice_does_not_drop_valid_interpretation(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    original = service.configured_coach

    def configured(p):
        provider, route = original(p)
        suggest = provider.suggest

        async def invalid_advice(*args, **kwargs):
            result = await suggest(*args, **kwargs)
            result["say_or_ask"] = "Our price is $99999."
            return result

        provider.suggest = invalid_advice
        return provider, route

    monkeypatch.setattr(service, "configured_coach", configured)
    turn(service, p, call, "valid-observation")
    result = await service.coach_turn(p, call["id"])
    assert result["action"] == "NO_ACTION"
    analysis = service.analysis(p, call["id"])
    assert analysis["call_memory"]["rundown_points"]
    assert analysis["interpretation_coverage"]["interpreted_segments"] == 1


@pytest.mark.asyncio
async def test_processed_nearly_expired_raw_does_not_starve_the_new_turn(workspace, app, clock, monkeypatch):
    call, p, requests, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "old")
    await service.coach_turn(p, call["id"])
    clock.now += timedelta(seconds=299)
    turn(service, p, call, "new")
    changes["during"] = lambda: setattr(clock, "now", clock.now + timedelta(seconds=2))
    await service.coach_turn(p, call["id"])
    assert "old" not in {item["segment_id"] for item in requests[-1]["recent_dialogue"]}
    assert requests[-1]["state"]["rundown_points"], "Retain the old meaning without near-expiry raw text"
    assert service.analysis(p, call["id"])["interpretation_coverage"]["interpreted_segments"] == 2


@pytest.mark.asyncio
async def test_no_action_cannot_publish_memory_from_the_wrong_response_version(workspace, app, monkeypatch):
    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "versioned")
    changes["result"] = {"action": "NO_ACTION", "state_version": -1}
    await service.coach_turn(p, call["id"])
    assert service.analysis(p, call["id"])["interpretation_coverage"]["interpreted_segments"] == 0
