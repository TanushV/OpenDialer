"""Post-call and coaching over HTTP: rundown, scores, rubrics, notes, trackers, next-call brief, goals and replay."""

from uuid import uuid4

from journey import answered_by_human, call, get, post, run_jobs, start, wrap_up

PASSWORD = "a-test-only-password-123"


def send(ws, method, path, **kwargs):
    return ws["client"].request(method, path, headers=ws["headers"], **kwargs)


def chunk(ws, call_id, segment_id, text, *, side="remote"):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": 1, "side": side, "is_final": True, "text": text},
    )


def finished_call(ws, *, outcome="not_interested"):
    """An answered call with an opener and one budget remark, hung up and wrapped up."""
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    wrap_up(ws, call_id, outcome=outcome, contact_role="right_party")
    return call_id


def test_rundown_finalizes_honestly_when_no_model_is_configured(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    live = get(ws, f"/v1/calls/{cid}")
    assert live["state"] == "FINALIZED" and live["outcome"] == "not_interested"
    assert live["rundown_status"] == "partial"
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert an["finalization"]["status"] == "partial" and an["finalization"]["reason"] == "ai_unavailable"
    assert an["notes"]["review_required"] is True and "No model rundown" in an["notes"]["summary"]
    assert an["notes"]["outcome"] == "not_interested" and live["suggested_outcome"] is None
    assert an["interpretation_coverage"]["total_segments"] == 2
    assert an["interpretation_coverage"]["interpreted_segments"] == 0
    assert {t["side"] for t in an["transcript"]} == {"rep", "remote"}
    items = an["scorecard"]["items"]
    assert len(items) == 7 and all(i["status"] == "INSUFFICIENT_EVIDENCE" for i in items)
    assert an["scorecard"]["overall"]["scorable_items"] == 0


def test_human_notes_save_against_the_current_edit_version(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    saved = call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 0, "text": "Wants a pricing sheet."},
    )
    assert saved["notes_edit_version"] == 1 and saved["human_notes"] == "Wants a pricing sheet."
    stale = send(
        ws, "PATCH", f"/v1/calls/{cid}/notes", json={"expected_edit_version": 0, "text": "Overwrite attempt"}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "NOTES_VERSION_CONFLICT", "current_edit_version": 1}
    edited = call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 1, "text": "Wants pricing by Friday."},
    )
    assert edited["notes_edit_version"] == 2
    run_jobs(ws)
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert an["human_notes"] == "Wants pricing by Friday." and an["status"] == "human_confirmed"
    assert an["notes_edit_version"] == 2, "Machine finalization never moves the human edit version"


def test_trackers_and_comments_join_the_call_review(workspace):
    ws = workspace
    tracker = call(
        ws,
        "POST",
        "/v1/admin/trackers",
        json={
            "kind": "keyword",
            "name": "Budget mention",
            "definition": "Prospect mentions budget",
            "phrases": ["budget"],
        },
    )
    refused = [
        send(
            ws,
            "POST",
            "/v1/admin/trackers",
            json={"kind": "keyword", "name": "No phrases", "definition": "x"},
        ),
        send(
            ws,
            "POST",
            "/v1/admin/trackers",
            json={"kind": "semantic", "name": "No examples", "definition": "x"},
        ),
    ]
    assert [r.status_code for r in refused] == [422, 422]
    cid = finished_call(ws)
    run_jobs(ws)
    matches = {m["tracker_id"]: m for m in get(ws, f"/v1/calls/{cid}/analysis")["trackers"]}
    assert matches[tracker["id"]]["evidence"] == ["s1"]
    comment = call(ws, "POST", f"/v1/calls/{cid}/comments", json={"text": "Good discovery"})
    assert comment["text"] == "Good discovery" and comment["call_id"] == cid
    assert [c["text"] for c in get(ws, f"/v1/calls/{cid}/analysis")["comments"]] == ["Good discovery"]


def test_human_scores_and_appeals_are_appended_and_resolved_once(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    an = get(ws, f"/v1/calls/{cid}/analysis")
    card, version = an["scorecard_id"], an["scorecard_version"]
    scored = {
        "status": "SCORED",
        "dimension": "identity_relevance",
        "score": 2,
        "evidence": ["r1"],
        "reason": "Named the company and purpose",
        "expected_version": version,
        "request_id": str(uuid4()),
    }
    path = f"/v1/scorecards/{card}/review"
    review = call(ws, "POST", path, json=scored)
    assert review["status"] == "SCORED" and review["score"] == 2 and review["reviewer_id"]
    assert call(ws, "POST", path, json=scored)["id"] == review["id"]
    assert send(ws, "POST", path, json={**scored, "score": 1}).status_code == 409
    assert send(ws, "POST", path, json={**scored, "request_id": str(uuid4())}).status_code == 409
    current = get(ws, f"/v1/calls/{cid}/analysis")["scorecard_version"]
    unknown = {
        **scored,
        "request_id": str(uuid4()),
        "dimension": "not_a_dimension",
        "expected_version": current,
    }
    assert send(ws, "POST", path, json=unknown).status_code == 422

    an = get(ws, f"/v1/calls/{cid}/analysis")
    human = next(i for i in an["reviewed_scorecard"]["items"] if i["dimension"] == "identity_relevance")
    assert human["score"] == 2 and human["source"] == "human"
    model = next(i for i in an["scorecard"]["items"] if i["dimension"] == "identity_relevance")
    assert model["status"] == "INSUFFICIENT_EVIDENCE", "The model assessment is never overwritten"

    appeal = call(ws, "POST", path, json={"reason": "The opener named the company"})
    an = get(ws, f"/v1/calls/{cid}/analysis")
    listed = next(r for r in an["reviews"] if r["id"] == appeal["id"])
    assert listed["status"] == "disputed" and listed["resolution_id"] is None
    resolved = call(
        ws,
        "POST",
        path,
        json={
            "status": "resolved",
            "resolves_review_id": appeal["id"],
            "reason": "Upheld: the company was named",
            "expected_version": an["scorecard_version"],
            "request_id": str(uuid4()),
        },
    )
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert next(r for r in an["reviews"] if r["id"] == appeal["id"])["resolution_id"] == resolved["id"]
    again = send(
        ws,
        "POST",
        path,
        json={
            "status": "resolved",
            "resolves_review_id": appeal["id"],
            "reason": "Second decision",
            "expected_version": an["scorecard_version"],
            "request_id": str(uuid4()),
        },
    )
    assert again.status_code == 409


def test_rubric_editor_saves_versioned_anchors_and_a_purpose_change_rescores_the_call(workspace):
    ws = workspace
    config = get(ws, f"/v1/admin/rubrics?campaign_id={ws['campaign_id']}")
    assert config["expected_version"] == 1
    assert set(config["rubrics"]) == {"cold_introduction", "qualification", "discovery", "follow_up"}
    config["rubrics"]["cold_introduction"]["title"] = "Opening introduction"
    saved = call(ws, "PUT", "/v1/admin/rubrics", json=config)
    assert saved["expected_version"] == 2
    reread = get(ws, f"/v1/admin/rubrics?campaign_id={ws['campaign_id']}")
    assert reread["rubrics"]["cold_introduction"]["title"] == "Opening introduction"
    assert send(ws, "PUT", "/v1/admin/rubrics", json=config).status_code == 409

    cid = finished_call(ws)
    run_jobs(ws)
    version = get(ws, f"/v1/calls/{cid}")["version"]
    changed = call(
        ws,
        "PUT",
        f"/v1/calls/{cid}/rubric",
        json={
            "expected_version": version,
            "call_type": "qualification",
            "reason": "Rep reclassified the call",
        },
    )
    assert changed["rubric_snapshot"]["call_type"] == "qualification"
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert an["call"]["rubric_snapshot"]["title"] == "Qualification"
    assert an["scorecard"]["call_type"] == "qualification"
    stale = send(
        ws,
        "PUT",
        f"/v1/calls/{cid}/rubric",
        json={"expected_version": version, "call_type": "discovery", "reason": "Stale view"},
    )
    assert stale.status_code == 409


def test_next_call_brief_carries_prior_conversation_before_the_next_call(workspace):
    ws = workspace
    cid = finished_call(ws)
    lead_id = get(ws, f"/v1/calls/{cid}")["lead_id"]
    call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 0, "text": "Wants a pricing sheet."},
    )
    brief = get(ws, f"/v1/leads/{lead_id}/packet")
    assert [c["id"] for c in brief["previous_calls"]] == [cid]
    assert brief["previous_calls"][0]["outcome"] == "not_interested"
    assert brief["prospect_brief"]["human_notes"] == [{"call_id": cid, "text": "Wants a pricing sheet."}]

    run_jobs(ws)
    refreshed = get(ws, f"/v1/leads/{lead_id}/packet")["prospect_brief"]
    assert cid in [v["call_id"] for v in refreshed["source_versions"]]
    assert refreshed["evidence_status"] == "observed"
    assert [g["call_id"] for g in refreshed["interpretation_gaps"]] == [cid], (
        "The unavailable rundown is disclosed"
    )

    call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 1, "text": "Wants pricing by Friday."},
    )
    updated = get(ws, f"/v1/leads/{lead_id}/packet")["prospect_brief"]
    assert [n["text"] for n in updated["human_notes"]] == ["Wants pricing by Friday."]


def test_coaching_goals_and_history_stay_versioned(workspace):
    ws = workspace
    finished_call(ws)
    run_jobs(ws)
    overview = get(ws, "/v1/coaching")
    assert overview["inspected_calls"] == 1 and overview["history_limit"] == 20
    assert overview["goals_version"] == 0 and overview["goals"] == []
    goal = {"id": str(uuid4()), "text": "Ask one discovery question", "status": "active"}
    saved = call(ws, "PUT", "/v1/coaching/goals", json={"expected_version": 0, "goals": [goal]})
    assert saved == {"goals": [goal], "goals_version": 1}
    stale_goals = send(ws, "PUT", "/v1/coaching/goals", json={"expected_version": 0, "goals": []})
    assert stale_goals.status_code == 409
    twin = {**goal, "text": "Another goal with the same identity"}
    clash = send(ws, "PUT", "/v1/coaching/goals", json={"expected_version": 1, "goals": [goal, twin]})
    assert clash.status_code == 422
    assert get(ws, "/v1/coaching")["goals"] == [goal]

    # Practice is the coaching list itself: what to work on and how. There is no AI rehearsal.
    assert send(ws, "POST", "/v1/coaching/practice", json={"scenario": "No budget"}).status_code == 404


def test_example_library_refuses_unreviewed_tips_and_replay_scores_cases(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    assert get(ws, "/v1/coaching")["examples"] == [], "No model-reviewed tip exists on the simulated path"
    version = get(ws, f"/v1/calls/{cid}")["transcript_version"]
    refused = send(
        ws,
        "POST",
        "/v1/admin/snippets",
        json={
            "call_id": cid,
            "coaching_point_id": "missing",
            "expected_transcript_version": version,
            "review_reference": "SIM-REVIEW-1",
        },
    )
    assert refused.status_code == 422
    unknown_example = {"expected_version": 1, "approved": False}
    assert send(ws, "PATCH", "/v1/admin/snippets/missing", json=unknown_example).status_code == 404
    review = {"expected_version": 1, "decision": "confirm", "reason": "Reviewed by the rep"}
    assert send(ws, "POST", f"/v1/calls/{cid}/observations/missing/review", json=review).status_code == 404

    cases = [
        {
            "id": "budget",
            "expected_action": "SHOW_CARD",
            "expected_card_ids": ["budget"],
            "state_version": 1,
            "recent_dialogue": [
                {
                    "segment_id": "s1",
                    "text": "We have no budget for this right now.",
                    "side": "remote",
                    "is_final": True,
                    "revision": 1,
                }
            ],
            "approved_cards": [
                {
                    "id": "budget",
                    "text": "What outcome would make the investment worthwhile?",
                    "tags": ["budget", "price"],
                }
            ],
        },
        {
            "id": "quiet",
            "expected_action": "NO_ACTION",
            "state_version": 1,
            "recent_dialogue": [
                {
                    "segment_id": "s2",
                    "text": "Sounds good, bye.",
                    "side": "remote",
                    "is_final": True,
                    "revision": 1,
                }
            ],
            "approved_cards": [],
        },
    ]
    report = call(
        ws, "POST", "/v1/admin/coaching/replay", json={"cases": cases, "review_reference": "SIM-REPLAY-001"}
    )
    assert report["samples"] == 2 and report["valid_samples"] == 2
    assert report["expected_action_fraction"] == 1.0 and report["expected_card_fraction"] == 1.0
    assert report["semantic_quality"] == "not_reviewed" and report["simulation"] is True
    assert [r["case_id"] for r in report["results"]] == ["budget", "quiet"]

    call(
        ws,
        "POST",
        "/v1/admin/users",
        json={"email": "rep@example.test", "password": PASSWORD, "role": "rep", "name": "Rep"},
    )
    session = call(ws, "POST", "/v1/auth/session", json={"email": "rep@example.test", "password": PASSWORD})
    denied = ws["client"].post(
        "/v1/admin/coaching/replay",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"cases": cases, "review_reference": "SIM-REPLAY-001"},
    )
    assert denied.status_code == 403
