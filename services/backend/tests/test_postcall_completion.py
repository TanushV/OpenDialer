"""Date and inferred-task contracts authored before implementation."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.db import doc
from conftest import NOW, disposition_body, start
from test_remediation_rep_workflow import p


@pytest.mark.parametrize(
    "expression,zone,expected",
    [
        ("Call me tomorrow at 2 PM.", "America/Los_Angeles", "2026-10-15T21:00:00+00:00"),
        ("Call me Tuesday at 14:30 Pacific time.", None, "2026-10-20T21:30:00+00:00"),
        ("Call on 2026-10-20 at 2 PM Eastern time.", None, "2026-10-20T18:00:00+00:00"),
        ("Call on October 20, 2026 at 2 PM Pacific time.", None, "2026-10-20T21:00:00+00:00"),
        ("Call tomorrow at 2 PM.", None, None),
        ("Maybe next Tuesday at 2 PM.", "America/Los_Angeles", None),
        ("Call on November 1, 2026 at 1:30 AM Pacific time.", None, None),
        ("Call on March 8, 2026 at 2:30 AM Pacific time.", None, None),
    ],
)
def test_date_resolution_uses_call_zone_and_rejects_dst_ambiguity(expression, zone, expected):
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {"verbatim_time_expression": expression, "due_at": expected, "needs_time_confirmation": False},
        call_at=datetime(2026, 10, 14, 18, tzinfo=UTC),
        timezone=zone,
    )
    assert result["due_at"] == expected
    assert result["needs_time_confirmation"] is (expected is None)


def test_vague_window_is_preserved_without_an_invented_exact_time():
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {
            "verbatim_time_expression": "Call next Tuesday afternoon.",
            "due_at": None,
            "needs_time_confirmation": True,
        },
        call_at=datetime(2026, 10, 14, 18, tzinfo=UTC),
        timezone="America/Los_Angeles",
    )
    assert result["due_at"] is None
    assert result["time_window"] == {
        "date": "2026-10-20",
        "period": "afternoon",
        "timezone": "America/Los_Angeles",
    }


def test_model_timestamp_must_match_the_evidenced_date_and_zone():
    from app.postcall.dates import resolve_time

    # A mismatch is a suggestion to confirm, not an exception that aborts the whole merge.
    result = resolve_time(
        {
            "verbatim_time_expression": "October 20, 2026 at 2 PM Pacific time",
            "due_at": "2026-10-20T14:00:00Z",
            "needs_time_confirmation": False,
        }
    )
    assert result["due_at"] is None and result["needs_time_confirmation"] is True
    assert result["proposed_due_at"] == "2026-10-20T14:00:00Z"


def test_partial_and_non_scored_rubrics_cannot_publish_an_inflated_score():

    source = {"segment_id": "s", "is_final": True, "text": "Hello from our team."}
    body = {
        "summary": "The seller introduced the call.",
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
                "explanation": "Introduced",
            }
        ],
    }
    from app.postcall.analysis import build_analysis
    from app.postcall.rubrics import effective

    DIMENSIONS = [d["id"] for d in effective({})["dimensions"]]  # The default rubric.

    state = merge_test_analysis(body, [source])
    # Incremental updates may cover one dimension; the saved view always includes all.
    card = build_analysis("fixture", [source], derived=state)["scorecard"]
    assert len(card["items"]) == len(DIMENSIONS)
    assert card["overall"]["coverage"] == 0  # No reviewed score yet.
    for item in state["rubric_items"]:
        if item["status"] == "scored":
            item.update(review_status="reviewed", review_receipt_id="fixture-review")
    card = build_analysis("fixture", [source], derived=state)["scorecard"]
    assert card["overall"]["coverage"] == 1 / len(DIMENSIONS)


def analysis_body(action="Call back"):
    return {
        "summary": "A future conversation was requested.",
        "summary_evidence": ["s"],
        "problems": [],
        "objections": [],
        "unknowns": [],
        "score_items": [],
        "commitments": [
            {
                "kind": "callback",
                "owner": "rep",
                "action": action,
                "due_at": "2026-10-20T21:00:00+00:00",
                "verbatim_time_expression": "October 20, 2026 at 2 PM Pacific time",
                "evidence": ["s"],
                "needs_time_confirmation": False,
            }
        ],
    }


def conversation_turn(body, context):
    """Translate retained scenario data to the sole production interpretation transport."""
    revisions = {i["segment_id"]: i["revision"] for i in context["recent_dialogue"]}

    def refs(ids):
        return [
            {"kind": "segment", "id": identity, "version": revisions.get(identity, 1)} for identity in ids
        ]

    observations = []
    for kind, values in (
        (
            "rundown",
            [{"text": body["summary"], "evidence": body["summary_evidence"], "review_required": True}],
        ),
        ("need", body.get("problems", [])),
        ("objection", body.get("objections", [])),
        ("commitment", body.get("commitments", [])),
        ("approach", body.get("observations", [])),
        (
            "question",
            [
                {"text": value, "evidence": body["summary_evidence"], "review_required": True}
                for value in body.get("unknowns", [])
            ],
        ),
    ):
        for item in values:
            observations.append(
                {
                    "kind": kind,
                    "text": item.get("text", item.get("action", item.get("concept"))),
                    "state": item.get("state", "confirmed"),
                    "sources": refs(item["evidence"]),
                    "replaces": item.get("replaces", []),
                    "quote": item.get("quote", ""),
                    "review_required": item.get("review_required", False),
                    "approach": item.get("approach"),
                    "commitment": {
                        k: item.get(k, "followup" if k == "kind" else None)
                        for k in (
                            "kind",
                            "owner",
                            "due_at",
                            "verbatim_time_expression",
                            "needs_time_confirmation",
                        )
                    }
                    if kind == "commitment" and item.get("state") != "retracted"
                    else None,
                }
            )
    definitions = {i["id"]: i["version"] for i in context["trackers"]}
    return {
        "state_version": context["state_version"],
        "context_fingerprint": context["context_fingerprint"],
        "guidance": None,
        "field_audit": [],
        "state_delta": {
            "stage": "next_step",
            "observations": observations,
            "rubric_items": [
                {
                    "dimension": i["dimension"],
                    "score": i["score"],
                    "status": {
                        "SCORED": "scored",
                        "N/A": "not_applicable",
                        "INSUFFICIENT_EVIDENCE": "insufficient",
                    }[i["status"]],
                    "sources": refs(i["evidence"]),
                    "explanation": i["explanation"],
                }
                for i in body.get("score_items", [])
            ],
            "tracker_matches": [
                {
                    "tracker_id": i["tracker_id"],
                    "definition_version": definitions.get(i["tracker_id"], 1),
                    "sources": refs(i["evidence"]),
                }
                for i in body.get("tracker_matches", [])
            ],
            "interpreted_segments": refs(list(revisions)),
        },
    }


def merge_test_analysis(body, segments):
    """Old scenario bodies drive the actual incremental schema and merge."""
    from app.coaching.context import merge_semantic_state, upgrade_state
    from app.postcall.rubrics import effective

    segments = [{"revision": 1, **item} for item in segments]
    context = {
        "recent_dialogue": segments,
        "trackers": [],
        "state_version": 1,
        "context_fingerprint": "fixture",
    }
    state = upgrade_state({}, call_id="fixture", rubric=effective({}))
    return merge_semantic_state(
        state, conversation_turn(body, context)["state_delta"], segments, request_id="fixture", at=NOW
    )


def stage_review(workspace, app, monkeypatch, body=None):
    """Enter the real finalization/review publication boundary without network work."""
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service, who = app.state.service, p(workspace)
    service.analysis(who, call["id"])
    owner = service._begin_finalization(who, call["id"])
    request = service._conversation_context(who, call["id"], "finalize")
    assert service._merge_finalization(
        who, call["id"], owner, request, conversation_turn(body or analysis_body(), request)
    )
    request = service._conversation_context(who, call["id"], "review")
    owner, review = service._state_review_snapshot(who, call["id"], request)
    return service, who, call, request, owner, review


class ConversationProvider:
    last_usage, last_request_id = {}, None
    last_provider, last_model = "Cerebras", "fixture"

    def __init__(self, state):
        self.state = state

    async def suggest(self, context, **kwargs):
        from copy import deepcopy

        self.state["requests"] += 1
        self.state.setdefault("contexts", []).append(deepcopy(context))
        if callback := self.state.get("during_generation"):
            callback()
        self.last_usage, self.last_request_id = {"cost": 0.001}, f"interpretation-{self.state['requests']}"
        return conversation_turn(self.state["body"], context)

    async def review(self, request, **kwargs):
        from copy import deepcopy

        from test_grounding_review import review_response

        self.state["requests"] += 1
        self.state.setdefault("contexts", []).append(deepcopy(request))
        if callback := self.state.get("during_review"):
            callback()
        self.last_usage = {"cost": 0.002}
        self.last_request_id = (
            "review-receipt" if self.state["requests"] == 2 else f"review-receipt-{self.state['requests']}"
        )
        if self.state.get("missing_review"):
            raise ValueError("Review capability unavailable")
        return review_response(request, verdict=self.state.get("verdict", "SUPPORTED"))


def restate_callback(service, who, call, *, revision=2):
    service._receive_transcript(
        who,
        call["id"],
        {
            "segment_id": "s",
            "revision": revision,
            "is_final": True,
            "side": "remote",
            "text": "Please call me on October 20, 2026 at 2 PM Pacific time. I still want to discuss procurement.",
        },
        None,
        True,
    )


async def finalize(service, who, call_id):
    """The finalize job, then the selective review job it queues (never inline)."""
    await service.analyze_postcall(who, call_id)
    await service.review_conversation(who, call_id)


def setup_analysis(workspace, app, monkeypatch):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=h,
        json={
            "segment_id": "s",
            "revision": 1,
            "is_final": True,
            "side": "remote",
            "text": "Please call me on October 20, 2026 at 2 PM Pacific time.",
        },
    )
    c.post("/v1/calls/" + call["id"] + "/hangup", headers=h)
    state = {"body": analysis_body(), "requests": 0}

    monkeypatch.setattr(
        app.state.service, "configured_coach", lambda *args: (ConversationProvider(state), "openrouter")
    )
    return call, state


@pytest.mark.asyncio
async def test_rephrased_commitment_updates_one_task_and_preserves_rep_edit(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        tasks = [doc(t) for t in service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "open"
    assert tasks[0]["due_at"] == "2026-10-20T21:00:00+00:00"
    service.edit_task(
        p(workspace),
        tasks[0]["id"],
        {
            "request_id": str(uuid4()),
            "expected_version": tasks[0]["version"],
            "due_at": "2026-10-21T21:00:00+00:00",
            "timezone": "America/Los_Angeles",
        },
    )
    state["body"] = analysis_body("Reconnect with the prospect")
    restate_callback(service, p(workspace), call)
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        tasks2 = [doc(t) for t in service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])]
        commitments = [doc(t) for t in service.lead_records(s, p(workspace), "commitments", call["lead_id"])]
    assert len(tasks2) == len(commitments) == 1
    assert commitments[0]["action"] == "Reconnect with the prospect"
    assert tasks2[0]["id"] == tasks[0]["id"]
    assert tasks2[0]["due_at"] == "2026-10-21T21:00:00+00:00"
    assert "verbatim_time_expression" not in commitments[0]


@pytest.mark.asyncio
async def test_corrected_source_holds_inferred_task_without_erasing_it(workspace, app, monkeypatch):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await finalize(service, p(workspace), call["id"])
    c, h = workspace["client"], workspace["headers"]
    before = c.get("/v1/leads/" + call["lead_id"] + "/packet", headers=h).json()["tasks"]
    assert len(before) == 1
    response = c.post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=h,
        json={
            "segment_id": "s",
            "revision": 2,
            "is_final": True,
            "side": "remote",
            "text": "I cannot agree to a callback yet.",
        },
    )
    assert response.status_code == 200
    packet = c.get("/v1/leads/" + call["lead_id"] + "/packet", headers=h).json()
    assert packet["tasks"][0]["id"] == before[0]["id"]
    assert packet["tasks"][0]["status"] == "needs_confirmation"
    assert packet["tasks"][0]["source_stale"] is True
    assert packet["lead"].get("callback_due_at") is None
    # An AI-inferred callback whose source changed defers outreach for a bounded time;
    # it never holds the lead indefinitely (only a rep-stated callback does).
    assert packet["lead"].get("outreach_hold") is None
    assert packet["lead"]["callback_suggestion_until"] == packet["lead"]["next_attempt_at"]
    # The rep's task list shows how long this suggestion holds outreach.
    tasks = c.get("/v1/workspace", headers=h).json()["tasks"]
    shown = next(t for t in tasks if t["id"] == before[0]["id"])
    assert shown.get("callback_suggestion_until") == packet["lead"]["callback_suggestion_until"]


@pytest.mark.parametrize(
    "expression",
    [
        "October 20, 2026 around 2 PM Pacific time",
        "October 20, 2026 at 2 PM or 3 PM Pacific time",
        "October 20 or 21, 2026 at 2 PM Pacific time",
        "October 20, 2026 at 2 PM CST",
        "October 20, 2026 after 2 PM Pacific time",
    ],
)
def test_non_exact_time_cannot_become_an_automatic_callback(expression):
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {"verbatim_time_expression": expression, "due_at": None, "needs_time_confirmation": False}
    )
    assert result["due_at"] is None
    assert result["needs_time_confirmation"] is True


@pytest.mark.parametrize("status", ["completed", "cancelled"])
@pytest.mark.asyncio
async def test_inference_never_reopens_finished_tasks(workspace, app, monkeypatch, status):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = doc(service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0])
    service.edit_task(
        p(workspace),
        task["id"],
        {"request_id": str(uuid4()), "expected_version": task["version"], "status": status},
    )
    state["body"] = analysis_body("Revisit this later")
    restate_callback(service, p(workspace), call)
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        after = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
        assert len(after) == 1
        assert after[0].data["status"] == status


@pytest.mark.asyncio
async def test_source_correction_preserves_human_task_but_flags_conflict(workspace, app, monkeypatch):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = doc(service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0])
    service.edit_task(
        p(workspace),
        task["id"],
        {
            "request_id": str(uuid4()),
            "expected_version": task["version"],
            "due_at": "2026-10-21T21:00:00+00:00",
            "timezone": "America/Los_Angeles",
        },
    )
    workspace["client"].post(
        "/v1/calls/" + call["id"] + "/transcript",
        headers=workspace["headers"],
        json={
            "segment_id": "s",
            "revision": 2,
            "is_final": True,
            "side": "remote",
            "text": "There is no agreed callback.",
        },
    )
    packet = (
        workspace["client"]
        .get("/v1/leads/" + call["lead_id"] + "/packet", headers=workspace["headers"])
        .json()
    )
    assert packet["tasks"][0]["due_at"] == "2026-10-21T21:00:00+00:00"
    assert packet["tasks"][0]["status"] == "open"
    assert packet["tasks"][0]["source_stale"] is True
    assert packet["tasks"][0]["review_required"] is True


@pytest.mark.asyncio
async def test_omission_preserves_promise_and_explicit_reviewed_retraction_closes_it(
    workspace, app, monkeypatch, clock
):
    from datetime import timedelta

    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await finalize(service, p(workspace), call["id"])
    state["body"]["commitments"] = []
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0]
        assert task.data["status"] == "open", "Omission is not evidence of cancellation"
        memory = service._by_key(s, p(workspace), "conversation_states", call["id"]).data
        old = next(i for i in memory["observations"] if i["kind"] == "commitment")
    service._receive_transcript(
        p(workspace),
        call["id"],
        {
            "segment_id": "cancel",
            "revision": 1,
            "is_final": True,
            "side": "remote",
            "text": "Cancel the procurement callback we previously arranged.",
        },
        None,
        True,
    )
    state["body"]["summary_evidence"] = ["cancel"]
    state["body"]["commitments"] = [
        {
            "action": "The previously arranged procurement follow-up is withdrawn.",
            "state": "retracted",
            "evidence": ["cancel"],
            "replaces": [old["id"]],
        }
    ]
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        task = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0]
        assert task.data["status"] == "cancelled"
        assert task.data["source_retracted"] is True
    # Retrying after raw expiry must not replace a valid rundown with the baseline.
    before = service.analysis(p(workspace), call["id"])["notes"]["summary"]
    clock.now += timedelta(seconds=301)
    await finalize(service, p(workspace), call["id"])
    after = service.analysis(p(workspace), call["id"])
    assert after["notes"]["summary"] == before
    assert after["transcript"] == []


@pytest.mark.asyncio
@pytest.mark.database
async def test_postcall_disposition_race_does_not_publish_old_interpretation(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service

    state["during_generation"] = lambda: service.save_disposition(
        p(workspace),
        call["id"],
        disposition_body(workspace["client"], workspace["headers"], call["id"], **{"outcome": "wrong_party"}),
    )
    await finalize(service, p(workspace), call["id"])
    with service.db.transaction(False) as s:
        assert not service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])


@pytest.mark.parametrize(
    "expression",
    [
        "October 20, 2026 at 2 PM London time",
        "October 20, 2026 and November 20, 2026 at 2 PM Pacific time",
    ],
)
def test_ambiguous_timezone_or_multiple_dates_cannot_reuse_a_fallback_zone(expression):
    from app.postcall.dates import resolve_time

    result = resolve_time(
        {"verbatim_time_expression": expression, "due_at": None, "needs_time_confirmation": False},
        timezone="America/Los_Angeles",
    )
    assert result["due_at"] is None
    assert result["needs_time_confirmation"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rep_first", [False, True])
async def test_manual_and_inferred_callback_share_one_task(workspace, app, monkeypatch, rep_first):
    call, state = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service

    def report():
        service.save_disposition(
            p(workspace),
            call["id"],
            disposition_body(
                workspace["client"],
                workspace["headers"],
                call["id"],
                **{"outcome": "callback_requested", "callback_at": "2026-10-21T21:00:00Z"},
            ),
        )

    if rep_first:
        report()
    await finalize(service, p(workspace), call["id"])
    if not rep_first:
        report()
    with service.db.transaction(False) as s:
        tasks = service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])
        assert len(tasks) == 1
        assert tasks[0].data["due_at"] == "2026-10-21T21:00:00+00:00"
        assert tasks[0].data["source"] == "rep_reported"


@pytest.mark.asyncio
async def test_confirming_corrected_task_acknowledges_review(workspace, app, monkeypatch):
    call, _ = setup_analysis(workspace, app, monkeypatch)
    service = app.state.service
    await finalize(service, p(workspace), call["id"])
    await service.transcript(
        p(workspace),
        call["id"],
        {
            "segment_id": "s",
            "revision": 2,
            "is_final": True,
            "side": "remote",
            "text": "Please confirm first.",
        },
        defer_coach=True,
    )
    with service.db.transaction(False) as s:
        task = doc(service.lead_records(s, p(workspace), "followup_tasks", call["lead_id"])[0])
    after = service.edit_task(
        p(workspace),
        task["id"],
        {
            "request_id": str(uuid4()),
            "expected_version": task["version"],
            "status": "open",
            "due_at": "2026-10-21T21:00:00+00:00",
            "confirmation_state": "confirmed",
            "confirmation_reason": "The rep independently confirmed this callback.",
        },
    )
    assert after["review_required"] is False
    assert after["human_edited"] is True


def test_non_scored_dimensions_cannot_carry_a_score():
    from app.postcall.rubrics import effective

    DIMENSIONS = [d["id"] for d in effective({})["dimensions"]]  # The default rubric.

    body = analysis_body()
    body["commitments"] = []
    body["score_items"] = [
        {
            "dimension": dimension,
            "score": None,
            "status": "N/A",
            "evidence": [],
            "explanation": "No observable opportunity",
        }
        for dimension in DIMENSIONS
    ]
    body["score_items"][0]["score"] = 2
    with pytest.raises(ValueError):
        merge_test_analysis(body, [{"segment_id": "s", "is_final": True, "text": "Please call next week."}])


# W3 date verifier contracts. A Pacific-time call on Monday 2026-10-19 at 10:00 local.
MONDAY_CALL = datetime(2026, 10, 19, 17, tzinfo=UTC)


def resolved(expression, zone="America/Los_Angeles", call_at=MONDAY_CALL, due_at=None):
    from app.postcall.dates import resolve_time

    return resolve_time(
        {"verbatim_time_expression": expression, "due_at": due_at, "needs_time_confirmation": False},
        call_at=call_at,
        timezone=zone,
    )


@pytest.mark.parametrize(
    "expression,zone,expected",
    [
        # A bare H:MM is not an agreed clock time; only explicit am/pm or a 24-hour form is.
        ("Tuesday at 2:00", "America/Los_Angeles", None),
        ("Tuesday 1:30", "America/Los_Angeles", None),
        ("Tuesday at 12:30", "America/Los_Angeles", None),
        ("Tuesday at 14:00", "America/Los_Angeles", "2026-10-20T21:00:00+00:00"),
        ("Tuesday at 08:30", "America/Los_Angeles", "2026-10-20T15:30:00+00:00"),
        ("Tuesday at 2 pm", "America/Los_Angeles", "2026-10-20T21:00:00+00:00"),
        # Abbreviations are case-insensitive and override the lead's zone.
        ("Tuesday at 2 pm pt", "America/New_York", "2026-10-20T21:00:00+00:00"),
        ("Tuesday at 2 pm PDT", "America/New_York", "2026-10-20T21:00:00+00:00"),
        ("Tuesday at 2 pm et", "America/Los_Angeles", "2026-10-20T18:00:00+00:00"),
        ("Tuesday at 2 pm HST", "America/Los_Angeles", "2026-10-21T00:00:00+00:00"),
        ("Tuesday at 2 pm BST", "America/Los_Angeles", "2026-10-20T13:00:00+00:00"),
        ("Tuesday at 2 pm CEST", "America/Los_Angeles", "2026-10-20T12:00:00+00:00"),
        ("Tuesday at 2 pm akdt", "America/Los_Angeles", "2026-10-20T22:00:00+00:00"),
        # A standard-time abbreviation during daylight time has two readings: confirm.
        ("Tuesday at 2 pm pst", "America/New_York", None),
        ("Tuesday at 2 pm CET", "America/Los_Angeles", None),
        ("Tuesday at 2 pm AKST", "America/Los_Angeles", None),
        # Unknown or internationally ambiguous abbreviations are never ignored.
        ("Tuesday at 2 pm IST", "America/Los_Angeles", None),
        ("Tuesday at 2 pm jst", "America/Los_Angeles", None),
        ("Tuesday at 2 pm PST or EST", "America/Los_Angeles", None),
        # A stated weekday must agree with the stated date.
        ("Friday, October 27 at 2 pm", "America/Los_Angeles", None),
        ("Tuesday, October 27 at 2 pm", "America/Los_Angeles", "2026-10-27T21:00:00+00:00"),
        ("Friday, 2026-10-27 at 2 pm", "America/Los_Angeles", None),
        ("Tomorrow, Friday, at 2 pm", "America/Los_Angeles", None),
        # "next <weekday>" within the current calendar week has two readings.
        ("next Tuesday 2 pm", "America/Los_Angeles", None),
        ("Tuesday 2 pm", "America/Los_Angeles", "2026-10-20T21:00:00+00:00"),
        ("next Monday 2 pm", "America/Los_Angeles", "2026-10-26T21:00:00+00:00"),
    ],
)
def test_callback_phrases_resolve_only_when_unambiguous(expression, zone, expected):
    result = resolved(expression, zone)
    assert result["due_at"] == expected, result
    assert result["needs_time_confirmation"] is (expected is None)
    assert result["time_resolution"] == ("exact" if expected else "needs_confirmation")


def test_next_weekday_in_the_following_calendar_week_is_that_weekday():
    # Thursday: the coming Tuesday is already in the next calendar week.
    thursday = datetime(2026, 10, 22, 17, tzinfo=UTC)
    assert resolved("next Tuesday 2 pm", call_at=thursday)["due_at"] == "2026-10-27T21:00:00+00:00"
    assert resolved("Tuesday 2 pm", call_at=thursday)["due_at"] == "2026-10-27T21:00:00+00:00"


def test_standard_time_abbreviation_resolves_when_that_time_is_in_effect():
    january = datetime(2027, 1, 18, 17, tzinfo=UTC)
    result = resolved("Tuesday at 2 pm pst", "America/New_York", call_at=january)
    assert result["due_at"] == "2027-01-19T22:00:00+00:00"
    assert result["timezone"] == "America/Los_Angeles"


def test_conflicting_model_timestamp_needs_confirmation_instead_of_aborting_the_merge():
    result = resolved("Tuesday at 2 pm", due_at="2026-10-20T14:00:00Z")
    assert result["due_at"] is None and result["needs_time_confirmation"] is True
    assert result["proposed_due_at"] == "2026-10-20T14:00:00Z", "Never silently discard the model's date"


def test_naive_call_time_cannot_anchor_a_relative_or_exact_callback():
    naive = datetime(2026, 10, 19, 10)
    for expression in ("Tuesday at 2 pm", "October 20, 2026 at 2 PM Pacific time"):
        result = resolved(expression, call_at=naive)
        assert result["due_at"] is None and result["needs_time_confirmation"] is True
