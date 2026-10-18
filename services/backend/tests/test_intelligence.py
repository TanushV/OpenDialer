import pytest
from app.coaching.engine import CardRejected, validate_card
from app.postcall.analysis import behavioral_metrics, build_analysis, score_summary
from app.transcripts import normalize_transcription
from conftest import start


def test_transcript_preserves_missing_timestamps_and_transport_role():
    event = {
        "call_control_id": "cc",
        "transcription_data": {"transcript": "No, do not call again", "is_final": True, "track": "inbound"},
    }
    segment = normalize_transcription(event, "call-1", "event-1")[0]
    assert segment["side"] == "remote"
    assert segment["confidence"] is None
    assert segment["start_ms"] is None
    assert segment["speaker_id"] is None


def test_score_denominator_excludes_na_and_missing_evidence():
    result = score_summary(
        [
            {"score": 2, "max_score": 2, "weight": 1, "status": "SCORED", "evidence": ["s1"]},
            {"score": 0, "max_score": 2, "weight": 1, "status": "N/A", "evidence": []},
            {"score": 0, "max_score": 2, "weight": 1, "status": "INSUFFICIENT_EVIDENCE", "evidence": []},
        ]
    )
    assert result["score"] == 100
    assert result["coverage"] == pytest.approx(1 / 3)
    assert score_summary([])["score"] is None


def test_short_wrong_party_is_not_scored_as_sales_failure():
    result = build_analysis(
        "call",
        [{"segment_id": "s1", "text": "Wrong number.", "side": "remote", "is_final": True}],
        outcome="wrong_party",
        transcript_version=1,
    )
    assert result["scorecard"]["overall"]["score"] is None
    assert result["notes"]["review_required"]


def test_behavioral_metrics_missing_data_is_visible():
    result = behavioral_metrics(
        [{"side": "rep", "text": "Can I ask a question?", "start_ms": None, "end_ms": None}]
    )
    assert result["timed_segments"] == 0
    assert result["rep_talk_ratio"] is None
    assert result["question_count"] == 1
    assert "emotion" not in result


def test_coach_rejects_stale_ungrounded_and_long_cards(clock):
    card = {
        "action": "SHOW_CARD",
        "kind": "DISCOVERY",
        "say_or_ask": "What would need to improve?",
        "reason": "Existing provider mentioned",
        "evidence_segment_ids": ["seg1"],
        "playbook_card_ids": ["card1"],
        "state_version": 3,
        "expires_in_ms": 7000,
    }
    assert validate_card(card, state_version=3, segment_ids={"seg1"}, card_ids={"card1"})
    for modified in [
        {"state_version": 2},
        {"evidence_segment_ids": ["missing"]},
        {"say_or_ask": "word " * 201},
        {"playbook_card_ids": ["unapproved"]},
        {"say_or_ask": "We guarantee to double your revenue"},
    ]:
        with pytest.raises(CardRejected):
            validate_card({**card, **modified}, state_version=3, segment_ids={"seg1"}, card_ids={"card1"})


def test_revision_lineage_optout_and_manual_notes_survive_finalization(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]

    def transcript(text, revision):
        return c.post(
            f"/v1/calls/{call['id']}/transcript",
            headers=h,
            json={
                "segment_id": "seg1",
                "revision": revision,
                "side": "remote",
                "text": text,
                "is_final": True,
                "start_ms": 0,
                "end_ms": 3000,
            },
        )

    assert transcript("We already use another provider.", 1).status_code == 200
    first = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    r = c.patch(
        f"/v1/calls/{call['id']}/notes",
        headers=h,
        json={"expected_version": first["notes_version"], "text": "Human corrected note"},
    )
    assert r.status_code == 200, r.text
    assert transcript("Please do not call me again.", 2).status_code == 200
    assert transcript("old contradiction", 1).status_code == 409
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post("/v1/admin/jobs/run", headers=h)
    final = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert final["human_notes"] == "Human corrected note"
    assert final["notes"]["transcript_version"] == 2
    assert final["suggested_update"] is not None
    assert len(final["transcript"]) == 1
    leads = c.get("/v1/workspace", headers=h).json()["leads"]
    assert next(x for x in leads if x["id"] == call["lead_id"])["suppressed"]


def test_notes_optimistic_concurrency_and_scorecard_appeal(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.patch(
            f"/v1/calls/{call['id']}/notes", headers=h, json={"expected_version": 999, "text": "Lost update"}
        ).status_code
        == 409
    )
    analysis = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    r = c.post(
        f"/v1/scorecards/{analysis['scorecard_id']}/review",
        headers=h,
        json={
            "reason": "Too little evidence for discovery",
            "dimension": "problem_discovery",
            "status": "INSUFFICIENT_EVIDENCE",
            "expected_version": analysis["scorecard_version"],
            "request_id": "insufficient-review",
            "evidence": [],
        },
    )
    assert r.status_code == 200
    assert r.json()["reviewer_id"] == workspace["user_id"]


def test_ended_call_never_delivers_a_stale_coaching_card(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "end-card",
            "revision": 1,
            "side": "remote",
            "text": "We use another provider.",
            "is_final": True,
        },
    )
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"] is None


def test_first_final_postcall_analysis_has_a_final_status(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post("/v1/admin/jobs/run", headers=h)
    assert c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()["status"] == "ai_finalized"


def test_ending_call_suppresses_coaching_before_a_terminal_receipt(workspace, carrier, monkeypatch):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "ending-card",
            "revision": 1,
            "side": "remote",
            "text": "We use another provider.",
            "is_final": True,
        },
    )

    async def unavailable(*args):
        raise TimeoutError("No carrier receipt")

    monkeypatch.setattr(carrier, "hangup", unavailable)
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"] is None
    assert c.post(f"/v1/calls/{call['id']}/coach", headers=h).json()["action"] == "NO_ACTION"
