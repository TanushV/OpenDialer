"""A24/A26/A49: replay measures withheld expectations and bounded paid attempts."""

import asyncio
import json

import pytest
from app.coaching.replay import evaluate
from app.db import get, update
from test_remediation_privacy import principal


def case(identity="replay"):
    return {
        "id": identity,
        "state_version": 1,
        "expected_action": "NO_ACTION",
        "recent_dialogue": [],
        "approved_cards": [],
    }


class Quiet:
    model = last_model = "fixture"
    last_provider = "Cerebras"
    last_usage, last_request_id = {}, None

    async def suggest(self, request, deadline_ms):
        return {"action": "NO_ACTION"}


async def test_replay_withholds_all_review_targets_from_model_and_leaves_input_unchanged():
    source = {
        **case(),
        "expected_card_ids": [],
        "review_criteria": "Withheld assessment",
        "expected_explanation": "Do not reveal this target",
        "reviewed_answer": "Secret answer",
    }
    before = json.dumps(source, sort_keys=True)
    seen = []

    class Observer(Quiet):
        async def suggest(self, request, deadline_ms):
            seen.append(request)
            return {"action": "NO_ACTION"}

    report = await evaluate([source], Observer(), model="fixture")
    assert seen == [{k: source[k] for k in ("id", "state_version", "recent_dialogue", "approved_cards")}]
    assert json.dumps(source, sort_keys=True) == before
    assert report["expected_action_fraction"] == 1
    assert report["semantic_quality"] == "not_reviewed"


async def test_replay_records_content_fingerprints_and_never_saves_generated_raw_text():
    source = {**case(), "expected_action": "SHOW_CARD", "private_question": "Private raw scenario"}

    class Answer(Quiet):
        async def suggest(self, request, deadline_ms):
            return {
                "action": "SHOW_CARD",
                "kind": "help",
                "say_or_ask": "Which constraint matters most?",
                "reason": "Private raw explanation",
                "state_version": 1,
                "expires_in_ms": 1000,
                "evidence_segment_ids": [],
                "playbook_card_ids": [],
            }

    report = await evaluate([source], Answer(), model="fixture")
    row = report["results"][0]
    assert row["valid"]
    assert len(row["context_sha256"]) == len(row["output_sha256"]) == 64
    assert "Private raw" not in json.dumps(report) and "Which constraint" not in json.dumps(report)


@pytest.mark.parametrize(
    "source",
    [
        [case(), case()],
        [{**case(), "expected_action": "INVALID"}],
        [{**case(), "private_question": "x" * 70000}],
        [{**case(str(i)), "private_question": "x" * 10000} for i in range(100)],
    ],
)
async def test_invalid_or_oversized_replay_is_rejected_before_any_provider_request(source):
    called = []

    class Observer(Quiet):
        async def suggest(self, request, deadline_ms):
            called.append(request)
            return {"action": "NO_ACTION"}

    with pytest.raises(ValueError):
        await evaluate(source, Observer(), model="fixture")
    assert not called


async def test_replay_total_deadline_stops_new_work_and_counts_timeouts_in_latency():
    called = []

    class Slow(Quiet):
        async def suggest(self, request, deadline_ms):
            called.append(request)
            await asyncio.sleep(0.1)
            return {"action": "NO_ACTION"}

    report = await evaluate(
        [case("one"), case("two")], Slow(), model="fixture", deadline_ms=100, max_duration_ms=20
    )
    assert len(called) == 1
    assert report["attempted_samples"] == 1 and report["unattempted_samples"] == 1
    assert report["valid_samples"] == 0
    assert report["p95_inference_ms"] >= 15
    assert report["expected_action_fraction"] == 0


def post(workspace, cases):
    return workspace["client"].post(
        "/v1/admin/coaching/replay",
        headers=workspace["headers"],
        json={"cases": cases, "review_reference": "Synthetic authorized replay, no real provider"},
    )


def test_replay_without_a_receipt_retains_uncertain_charge(workspace, app, monkeypatch):
    class Failing(Quiet):
        async def suggest(self, request, deadline_ms):
            raise TimeoutError("Private provider error text must not be persisted")

    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service, "configured_coach", lambda p: (Failing(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    response = post(workspace, [case()])
    assert response.status_code == 200, response.text
    with service.db.transaction(False) as s:
        usage = [r.data for r in service.scoped(s, "usage_ledger", p)]
    assert len(usage) == 1 and usage[0]["status"] == "uncertain"
    assert usage[0]["provider"] == "openrouter"
    assert "Private provider" not in str(usage) + response.text


def test_replay_does_no_sql_on_the_provider_event_loop(workspace, app, monkeypatch):
    from sqlalchemy import event

    service, on_loop = app.state.service, []
    monkeypatch.setattr(service, "configured_coach", lambda p: (Quiet(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)

    def observed(conn, cursor, statement, parameters, context, many):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        on_loop.append(statement.split()[0])

    event.listen(service.db.engine, "before_cursor_execute", observed)
    try:
        result = post(workspace, [case()])
        assert result.status_code == 200, result.text
    finally:
        event.remove(service.db.engine, "before_cursor_execute", observed)
    assert on_loop == []


def test_replay_stops_before_next_charge_after_admin_revocation(workspace, app, monkeypatch):
    service, p, called = app.state.service, principal(workspace), []

    class Revoking(Quiet):
        async def suggest(self, request, deadline_ms):
            called.append(request["id"])

            def revoke():
                with service.db.transaction() as s:
                    update(get(s, "users", p["user_id"]), role="rep")

            await asyncio.to_thread(revoke)
            return {"action": "NO_ACTION"}

    monkeypatch.setattr(service, "configured_coach", lambda p: (Revoking(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    result = post(workspace, [case("first"), case("second")])
    assert called == ["first"]
    assert result.status_code == 403


async def test_provider_mutation_cannot_change_evidence_or_expected_answer():
    source = {**case(), "facts": [{"statement": "Only documented capabilities are supported."}]}
    original = json.dumps(source, sort_keys=True)

    class Mutating(Quiet):
        async def suggest(self, request, deadline_ms):
            request["facts"][0]["statement"] = "Changed source"
            return {"action": "NO_ACTION"}

    await evaluate([source], Mutating(), model="fixture")
    assert json.dumps(source, sort_keys=True) == original


async def test_response_returned_after_cancelled_deadline_cannot_be_counted_valid():
    class Late(Quiet):
        async def suggest(self, request, deadline_ms):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return {"action": "NO_ACTION"}

    result = await evaluate([case()], Late(), model="fixture", deadline_ms=10)
    assert result["valid_fraction"] == 0
    assert result["results"][0]["failure"] == "TimeoutError"


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_action", []),
        ("expected_card_ids", 1),
        ("recent_dialogue", "not a dialogue"),
    ],
)
def test_malformed_replay_returns_a_validation_error_without_spending(
    workspace, app, monkeypatch, field, value
):
    called = []

    class Observer(Quiet):
        async def suggest(self, request, deadline_ms):
            called.append(request)
            return {"action": "NO_ACTION"}

    monkeypatch.setattr(app.state.service, "configured_coach", lambda p: (Observer(), "openrouter"))
    monkeypatch.setattr(app.state.service.settings, "simulation", False)
    result = post(workspace, [{**case(), field: value}])
    assert result.status_code == 422, result.text
    assert not called


def test_cli_does_not_offer_a_route_that_bypasses_required_privacy():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(  # noqa: S603 - fixed owned CLI and help-only argument
        [sys.executable, str(root / "scripts/evaluate_coach.py"), "--help"],
        env={**os.environ, "PYTHONPATH": str(root / "services/backend")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "groq" not in result.stdout.lower()


def test_synthetic_replay_includes_novel_context_and_withheld_review_criteria():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    cases = [
        json.loads(line)
        for line in (root / "packages/evaluation-fixtures/coach.jsonl").read_text().splitlines()
    ]
    indexed = {value["id"]: value for value in cases}
    for identity in (
        "novel-committee",
        "qualified-refusal",
        "budget-negation",
        "changed-interest",
        "sarcasm",
        "unknown-pricing",
        "already-answered",
        "uncertain-speaker",
        "knowledge-contradiction",
    ):
        item = indexed[identity]
        assert item["review_criteria"] and item["recent_dialogue"]
        assert item["approved_cards"] == []
