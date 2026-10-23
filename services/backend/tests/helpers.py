"""Shared fixtures for the unit tests that drive service internals directly."""

import asyncio
import json
from contextlib import contextmanager

from app.db import update
from conftest import disposition_body, start
from sqlalchemy import event

PHONE = "+14155550101"


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def advice(version=1, evidence="s1", text="Which part of that constraint would be useful to clarify first?"):
    return {
        "action": "SHOW_CARD",
        "kind": "DISCOVERY",
        "say_or_ask": text,
        "reason": "Clarify the prospect's stated concern.",
        "evidence_segment_ids": [evidence],
        "playbook_card_ids": [],
        "state_version": version,
        "expires_in_ms": 20000,
        "state_update": None,
    }


def ordinary_advice(request, evidence="s1", **changes):
    """Explicit audited nonassertive fixture; claim tests keep the untyped advice helper."""
    return {
        **advice(request["state_version"], evidence),
        "context_fingerprint": request["context_fingerprint"],
        "field_audit": [
            {"field": field, "classification": "nonassertive", "sources": [], "implied_premises": []}
            for field in ("say_or_ask", "reason")
        ],
        **changes,
    }


def finish(workspace, call, outcome="right_party"):
    c, h = workspace["client"], workspace["headers"]
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    result = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], **{"outcome": outcome}),
    )
    assert result.status_code == 200, result.text


@contextmanager
def async_sql(app):
    found = []

    def inspected(connection, cursor, statement, *rest):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        found.append(statement.split()[0])

    event.listen(app.state.db.engine, "before_cursor_execute", inspected)
    try:
        yield found
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", inspected)


def frame(content="", **fields):
    return (
        "data: "
        + json.dumps(
            {
                "id": "progress-fixture",
                "model": "fixture",
                "provider": "Cerebras",
                "choices": [{"delta": {"content": content}, "finish_reason": None}],
                **fields,
            }
        )
        + "\n\n"
    ).encode()


def voicemail_policy(app, w, maximum):
    with app.state.db.transaction() as s:
        update(
            app.state.service.required(s, "policy_versions", w["policy_id"], p(w)),
            max_attempts=maximum,
            permissions=["live", "prerecorded"],
            voicemail_enabled=True,
            voicemail_optout_supported=True,
            approved_assets=["vm"],
            detached_topology_verified=True,
            late_human_recovery_destination="sip:recovery@example.test",
        )


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


p = principal


def screen(w):
    return w["client"].post("/v1/admin/dnc/screen", headers=w["headers"], json={"phones": [PHONE]})
