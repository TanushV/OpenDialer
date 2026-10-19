"""Open concepts, observed responses and honest later-outcome associations (tests first)."""

from copy import deepcopy
from datetime import timedelta

import pytest
from app.db import update
from conftest import disposition_body
from test_grounding_review import review_response
from test_postcall_completion import ConversationProvider, setup_analysis
from test_remediation_rep_workflow import p


def cohort(clock, count=80, *, later=False, inverted=False):
    rows = []
    for i in range(count):
        exposure = i % 2 == 0
        at = clock.now - timedelta(days=25 if later else 60, minutes=count - i)
        rows.append(
            {
                "call_id": f"{'later' if later else 'early'}-{i}",
                "lead_id": f"{'later' if later else 'early'}-{i}",
                "at": at.isoformat(),
                "label_available_at": (at + timedelta(days=14)).isoformat(),
                "window_ends_at": (at + timedelta(days=14)).isoformat(),
                "window_closed": True,
                "conversion": int(exposure != inverted),
                "segment": "small-business",
                "features": {"requested_contact": 0, "attempt_count": 0},
                "observations": [
                    {
                        "concept": "Procurement review",
                        "approach": "Clarify approval steps" if exposure else "Describe features",
                        "observed_at": (at + timedelta(minutes=10)).isoformat(),
                        "status": "active",
                        "review_status": "model_reviewed",
                    }
                ],
            }
        )
    return rows


def report(rows, clock, **kwargs):
    from app.optimization.learning import conversation_associations

    return conversation_associations(rows, as_of=clock.now, min_support=10, **kwargs)


def pattern(result, approach="Clarify approval steps"):
    return next(x for x in result["patterns"] if x["approach"] == approach)


def test_association_needs_later_independent_support_not_only_a_good_discovery_fit(clock):
    early = cohort(clock)
    result = report(early + cohort(clock, later=True), clock)
    found = pattern(result)
    assert found["status"] == "supported_correlation"
    assert found["samples"] == 80
    assert found["validation"]["exposed"] == 40
    assert found["validation"]["reference"] == 40
    assert found["validation"]["difference_low"] > 0
    assert found["automatic_policy_change"] is False
    assert result["multiple_comparison"] == "bonferroni_simultaneous_intervals"
    assert result["unit"] == "one_first_eligible_conversation_per_lead_or_endpoint"
    # Labels for this single time cohort are not available before its own later calls.
    assert pattern(report(early, clock))["status"] != "supported_correlation"
    reversed_result = report(early + cohort(clock, later=True, inverted=True), clock)
    assert pattern(reversed_result)["status"] != "supported_correlation"


def test_repeated_calls_or_shared_endpoint_do_not_inflate_support(clock):
    rows = cohort(clock, count=8)
    repeated = [dict(row, call_id=f"{row['call_id']}-{i}") for row in rows for i in range(20)]
    assert report(repeated, clock)["patterns"] == []
    rows = cohort(clock)
    for row in rows:
        row["endpoint_id"] = "shared-switchboard"
    assert report(rows, clock)["patterns"] == []


@pytest.mark.parametrize(
    "bad", ["future_observation", "unreviewed", "invalidated", "pending", "future_label", "after_label"]
)
def test_unavailable_or_unreliable_observations_cannot_become_supported_evidence(clock, bad):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows:
        observation = row["observations"][0]
        if bad == "future_observation":
            observation["observed_at"] = (clock.now + timedelta(days=1)).isoformat()
        elif bad == "unreviewed":
            observation["review_status"] = "unreviewed"
        elif bad == "invalidated":
            observation["status"] = "invalidated"
        elif bad == "pending":
            row.update(window_closed=False, conversion=None)
        elif bad == "future_label":
            row["label_available_at"] = (clock.now + timedelta(days=1)).isoformat()
        else:
            observation["observed_at"] = clock.now.isoformat()
    assert report(rows, clock)["patterns"] == []


def test_comparable_mix_is_required_and_sparse_support_is_visible(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows:
        row["segment"] = "warm" if row["conversion"] else "cold"
    found = pattern(report(rows, clock))
    assert found["status"] == "descriptive_only"
    assert found["validation"]["comparable_strata"] == 0
    assert found["reason"]


def test_vocabulary_normalizes_surface_forms_but_never_erases_negation(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for i, row in enumerate(rows):
        row["observations"][0]["concept"] = "PROCUREMENT  REVIEW!" if i % 3 else "Procurement review"
    result = report(rows, clock)
    assert len({x["concept_key"] for x in result["patterns"]}) == 1
    for row in rows[:40]:
        row["observations"][0]["concept"] = "No procurement review"
    result = report(rows, clock)
    assert len({x["concept_key"] for x in result["patterns"]}) == 2


def test_human_reviewed_aliases_are_versioned_and_do_not_create_new_observations(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows[:80]:
        row["observations"][0]["concept"] = "Purchasing signoff"
    vocabulary = {
        "version": 3,
        "aliases": [{"field": "concept", "source": "Purchasing signoff", "target": "Procurement review"}],
    }
    result = report(rows, clock, vocabulary=vocabulary)
    assert result["vocabulary_version"] == 3
    assert pattern(result)["status"] == "supported_correlation"
    assert len({x["concept_key"] for x in result["patterns"]}) == 1
    assert report([], clock, vocabulary=vocabulary)["patterns"] == []


def observed_body():
    return {
        "summary": "Approval requirements were discussed.",
        "summary_evidence": ["concern", "response"],
        "problems": [],
        "objections": [],
        "unknowns": [],
        "score_items": [],
        "commitments": [],
        "observations": [
            {
                "concept": "Procurement approval",
                "approach": "Clarify approval steps",
                "evidence": ["concern", "response"],
                "quote": "Our purchasing committee has to sign off before we can proceed.",
            }
        ],
    }


def observed_call(workspace, app, monkeypatch):
    call, state = setup_analysis(workspace, app, monkeypatch)
    c, h = workspace["client"], workspace["headers"]
    for identifier, side, text in [
        ("concern", "remote", "Our purchasing committee has to sign off before we can proceed."),
        ("response", "rep", "Which people are involved, and what must they review before approving?"),
    ]:
        response = c.post(
            f"/v1/calls/{call['id']}/transcript",
            headers=h,
            json={"segment_id": identifier, "side": side, "text": text, "revision": 1, "is_final": True},
        )
        assert response.status_code == 200, response.text
    state["body"] = observed_body()
    return call, state


@pytest.mark.asyncio
async def test_reviewed_observations_survive_expiry_without_raw_text_and_feed_dataset(
    workspace, app, monkeypatch, clock
):
    service = app.state.service
    call, _ = observed_call(workspace, app, monkeypatch)
    await service.analyze_postcall(p(workspace), call["id"])
    result = service.analysis(p(workspace), call["id"])
    observation = result["observations"][0]
    assert observation["concept"] == "Procurement approval"
    assert observation["review_status"] == "model_reviewed"
    assert observation["status"] == "active"
    assert observation["source_revisions"] == {"concern": 1, "response": 1}
    assert observation["observed_at"] == clock.now.isoformat()
    assert "quote" not in observation
    assert "purchasing committee" not in str(observation)
    # An observed right-party call is not yet a conversion; a later confirmed label is separate.
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.post(
            f"/v1/calls/{call['id']}/disposition",
            headers=h,
            json=disposition_body(c, h, call["id"], **{"outcome": "right_party"}),
        ).status_code
        == 200
    )
    clock.now += timedelta(minutes=6)
    expired = service.analysis(p(workspace), call["id"])
    assert not expired["transcript"]
    assert expired["observations"][0]["id"] == observation["id"]
    with service.db.transaction(False) as s:
        row = next(r for r in service.operational_dataset(s, p(workspace)) if r["call_id"] == call["id"])
    assert row["observations"][0]["id"] == observation["id"]
    assert row["conversion"] is None
    assert "Procurement approval" not in str(row["features"]), (
        "current-call text cannot predict its earlier pickup"
    )


@pytest.mark.asyncio
async def test_observation_rephrase_keeps_identity_but_rep_retraction_wins(workspace, app, monkeypatch):
    call, state = observed_call(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    result = service.analysis(p(workspace), call["id"])
    first = result["observations"][0]
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/calls/{call['id']}/observations/{first['id']}/review"
    response = c.post(
        path,
        headers=h,
        json={
            "expected_version": first["version"],
            "decision": "retract",
            "reason": "This approach was a hypothetical, not actually used.",
        },
    )
    assert response.status_code == 200, response.text
    assert (
        c.post(
            path,
            headers=h,
            json={"expected_version": first["version"], "decision": "confirm", "reason": "Stale screen"},
        ).status_code
        == 409
    )
    state["body"]["observations"][0]["concept"] = "Purchasing permission"
    await service.analyze_postcall(p(workspace), call["id"])
    second = service.analysis(p(workspace), call["id"])["observations"]
    assert len(second) == 1
    assert second[0]["id"] == first["id"]
    assert second[0]["status"] == "retracted"
    assert second[0]["reviewer_id"] == workspace["user_id"]


@pytest.mark.asyncio
async def test_source_correction_invalidates_observations_and_cached_guidance(workspace, app, monkeypatch):
    call, _ = observed_call(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p(workspace))
        update(
            campaign, conversation_report={"patterns": [{"status": "supported_correlation"}], "stale": False}
        )
    r = workspace["client"].post(
        f"/v1/calls/{call['id']}/transcript",
        headers=workspace["headers"],
        json={
            "segment_id": "concern",
            "side": "remote",
            "text": "We do not use a purchasing committee.",
            "revision": 2,
            "is_final": True,
        },
    )
    assert r.status_code == 200
    observation = service.analysis(p(workspace), call["id"])["observations"][0]
    assert observation["status"] == "invalidated"
    with service.db.transaction(False) as s:
        assert service.required(s, "campaigns", workspace["campaign_id"], p(workspace)).data[
            "conversation_report"
        ]["stale"]


@pytest.mark.asyncio
async def test_observations_are_in_meaning_review_and_rejected_as_a_unit(workspace, app, monkeypatch):
    call, state = observed_call(workspace, app, monkeypatch)
    seen = []

    class Rejecting(ConversationProvider):
        async def review(self, context, **kwargs):
            seen.extend(item["field"] for item in context["fields"])
            self.last_usage, self.last_request_id = {"cost": 0.001}, "reject-observation-review"
            return review_response(context, verdict="CONTRADICTED")

    monkeypatch.setattr(app.state.service, "configured_coach", lambda *args: (Rejecting(state), "openrouter"))
    await app.state.service.analyze_postcall(p(workspace), call["id"])
    assert any(i.startswith("observations/") for i in seen)
    assert app.state.service.analysis(p(workspace), call["id"])["observations"] == []


def test_suggested_response_is_not_evidence_the_rep_used_it():
    from test_postcall_completion import merge_test_analysis

    data = observed_body()
    with pytest.raises(ValueError, match="rep"):
        merge_test_analysis(
            data,
            [
                {
                    "segment_id": "concern",
                    "text": data["observations"][0]["quote"],
                    "side": "remote",
                    "is_final": True,
                },
                {"segment_id": "response", "text": "Who is involved?", "side": "remote", "is_final": True},
            ],
        )


def test_cached_associations_have_explicit_versioned_admin_correction_and_no_paid_get(
    workspace, app, monkeypatch
):
    c, h = workspace["client"], workspace["headers"]
    before = c.get("/v1/optimization/conversations", headers=h)
    assert before.status_code == 200, before.text
    assert before.json()["report"]["status"] == "awaiting_refresh"
    assert before.json()["vocabulary"]["version"] == 1
    response = c.put(
        "/v1/admin/optimization/conversation-vocabulary",
        headers=h,
        json={
            "expected_version": 1,
            "aliases": [
                {"field": "concept", "source": "Purchasing signoff", "target": "Procurement approval"}
            ],
            "reason": "Same concept reviewed against call evidence",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert (
        c.put(
            "/v1/admin/optimization/conversation-vocabulary",
            headers=h,
            json={"expected_version": 1, "aliases": [], "reason": "Stale edit"},
        ).status_code
        == 409
    )
    monkeypatch.setattr(
        app.state.service, "operational_dataset", lambda *args: pytest.fail("GET rebuilt full history")
    )
    result = c.get("/v1/optimization/conversations", headers=h).json()
    assert result["vocabulary"]["version"] == 2
    assert result["report"]["patterns"] == []
    for aliases in [
        [
            {"field": "concept", "source": "A", "target": "B"},
            {"field": "concept", "source": "B", "target": "A"},
        ],
        [
            {"field": "concept", "source": "A", "target": "B"},
            {"field": "concept", "source": "a!", "target": "C"},
        ],
    ]:
        assert (
            c.put(
                "/v1/admin/optimization/conversation-vocabulary",
                headers=h,
                json={"expected_version": 2, "aliases": aliases, "reason": "Invalid normalization"},
            ).status_code
            == 422
        )


def test_associations_feed_next_call_context_only_when_fresh_validated_and_same_goal(workspace, app):
    from conftest import start

    call = start(workspace)
    service = app.state.service
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p(workspace))
        report_value = {
            "as_of": service.clock().isoformat(),
            "goal": "meeting_held",
            "window_days": 14,
            "vocabulary_version": 1,
            "stale": False,
            "patterns": [
                {
                    "concept": "Procurement approval",
                    "approach": "Clarify approval steps",
                    "status": "supported_correlation",
                    "samples": 80,
                    "automatic_policy_change": False,
                },
                {
                    "concept": "Do not disclose",
                    "approach": "Unsafe sparse tip",
                    "status": "descriptive_only",
                    "samples": 2,
                },
            ],
        }
        update(campaign, conversation_report=report_value)
    request = service._help_context(p(workspace), call["id"], True, "What should I consider?")[0]
    assert request["conversation_learning"]["evidence_type"] == "observational_correlation"
    assert len(request["conversation_learning"]["patterns"]) == 1
    assert "Do not disclose" not in str(request["conversation_learning"])
    for change in [{"stale": True}, {"goal": "sale_won"}, {"vocabulary_version": 0}]:
        with service.db.transaction() as s:
            campaign = service.required(s, "campaigns", workspace["campaign_id"], p(workspace))
            update(campaign, conversation_report={**report_value, **change})
        assert (
            service._help_context(p(workspace), call["id"], True, "What should I consider?")[0][
                "conversation_learning"
            ]["patterns"]
            == []
        )


def test_helper_uses_the_frozen_prepared_script_the_rep_is_reading(workspace, app):
    from conftest import start

    call = start(workspace)
    service = app.state.service
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call["id"], p(workspace))
        script = deepcopy(row.data["script_snapshot"])
        script["opener"] = "The approved active-call version."
        update(row, script_snapshot=script)
    context = service._help_context(p(workspace), call["id"], True, "Can you help?")[0]
    assert context["prepared_script"]["opener"] == script["opener"]


def test_multiplicity_widens_intervals_instead_of_reusing_the_default_z():
    from app.optimization.numbering import wilson

    low, high = wilson(20, 40)
    wider_low, wider_high = wilson(20, 40, z=3.5)
    assert wider_low < low < high < wider_high


def test_legacy_call_without_snapshot_still_gives_the_helper_a_script_object(workspace, app):
    from conftest import start

    call = start(workspace)
    service = app.state.service
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call["id"], p(workspace))
        update(row, script_snapshot=None)
    context = service._help_context(p(workspace), call["id"], True, "Help please")[0]
    assert isinstance(context["prepared_script"], dict)
    assert context["prepared_script"]["opener"]


def test_transitively_shared_contacts_are_one_independent_unit(clock):
    rows = cohort(clock, count=40)
    for index, row in enumerate(rows):
        row["lead_id"] = f"lead-{index // 2}"
        row["endpoint_id"] = f"endpoint-{(index + 1) // 2}"
    assert report(rows, clock)["patterns"] == []


def test_background_report_cannot_publish_an_outdated_dataset_after_a_correction(workspace, app, monkeypatch):
    service = app.state.service
    original = service.save_operational_dataset

    def changed(principal):
        dataset = original(principal)
        with service.db.transaction() as s:
            campaign = service.required(s, "campaigns", workspace["campaign_id"], principal)
            update(campaign, conversation_revision=campaign.data.get("conversation_revision", 0) + 1)
        return dataset

    monkeypatch.setattr(service, "save_operational_dataset", changed)
    service.refresh_optimization(p(workspace))
    result = workspace["client"].get("/v1/optimization/conversations", headers=workspace["headers"]).json()
    assert result["report"]["status"] != "ready"


def test_background_report_uses_the_selected_campaign_only(workspace, app, clock, monkeypatch):
    service = app.state.service
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p(workspace))
        update(campaign, optimization={**campaign.data.get("optimization", {}), "auto_train": False})
    other = cohort(clock) + cohort(clock, later=True)
    for row in other:
        row["campaign_id"] = "a-different-campaign"
    monkeypatch.setattr(service, "operational_dataset", lambda *args: other)
    service.refresh_optimization(p(workspace))
    result = workspace["client"].get("/v1/optimization/conversations", headers=workspace["headers"]).json()
    assert result["report"]["patterns"] == []


@pytest.mark.asyncio
async def test_retracted_conversion_marks_old_guidance_stale(workspace, app, monkeypatch, clock):
    call, _ = observed_call(workspace, app, monkeypatch)
    c, h = workspace["client"], workspace["headers"]
    result = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "observed-conversion",
            "kind": "meeting_held",
            "occurred_at": clock.now.isoformat(),
            "source_reference": "Rep confirmed",
            "call_id": call["id"],
        },
    )
    assert result.status_code == 200
    with app.state.service.db.transaction() as s:
        campaign = app.state.service.required(s, "campaigns", workspace["campaign_id"], p(workspace))
        update(campaign, conversation_report={"stale": False, "patterns": []})
    assert (
        c.post(
            f"/v1/leads/{call['lead_id']}/conversions/{result.json()['id']}/retract",
            headers=h,
            json={"reason": "Meeting was actually cancelled"},
        ).status_code
        == 200
    )
    with app.state.service.db.transaction(False) as s:
        campaign = app.state.service.required(s, "campaigns", workspace["campaign_id"], p(workspace))
        assert campaign.data["conversation_report"]["stale"]


@pytest.mark.asyncio
async def test_interpretation_does_not_receive_outcomes_as_evidence_of_the_response(
    workspace, app, monkeypatch
):
    call, state = observed_call(workspace, app, monkeypatch)
    await app.state.service.analyze_postcall(p(workspace), call["id"])
    contexts = state["contexts"]
    assert contexts
    assert not any(i["kind"] == "outcome" and i["id"] == call["id"] for i in contexts[0]["evidence_catalog"])


@pytest.mark.asyncio
async def test_legacy_unreviewed_observations_are_not_operational_training_inputs(
    workspace, app, monkeypatch, clock
):
    call, _ = observed_call(workspace, app, monkeypatch)
    service = app.state.service
    service.analysis(p(workspace), call["id"])
    with service.db.transaction() as s:
        call_row = service.required(s, "call_attempts", call["id"], p(workspace))
        update(
            call_row,
            outcome="right_party",
            outcome_available_at=clock.now.isoformat(),
            outcome_source="rep_reported",
        )
        note = service._by_key(s, p(workspace), "notes", call["id"])
        update(note, observations=[{"concept": "An unchecked legacy guess", "approach": "Invented success"}])
    with service.db.transaction(False) as s:
        row = next(r for r in service.operational_dataset(s, p(workspace)) if r["call_id"] == call["id"])
    assert row["observations"] == []


@pytest.mark.asyncio
async def test_prospect_brief_retains_reviewed_concepts_without_showing_retracted_ones(
    workspace, app, monkeypatch
):
    call, _ = observed_call(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    packet = service.packet(p(workspace), call["lead_id"])
    assert packet["prospect_brief"]["observations"][0]["concept"] == "Procurement approval"
    observation = service.analysis(p(workspace), call["id"])["observations"][0]
    response = workspace["client"].post(
        f"/v1/calls/{call['id']}/observations/{observation['id']}/review",
        headers=workspace["headers"],
        json={
            "expected_version": observation["version"],
            "decision": "retract",
            "reason": "Incorrect interpretation",
        },
    )
    assert response.status_code == 200
    assert service.packet(p(workspace), call["lead_id"])["prospect_brief"]["observations"] == []


def test_future_outcome_is_required_before_any_guidance_association(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    for row in rows:
        row["observations"][0]["observed_at"] = row["label_available_at"]
    assert report(rows, clock)["patterns"] == []


@pytest.mark.asyncio
async def test_rep_cannot_review_another_reps_observation_or_change_shared_names(workspace, app, monkeypatch):
    from app.db import put

    call, _ = observed_call(workspace, app, monkeypatch)
    service = app.state.service
    await service.analyze_postcall(p(workspace), call["id"])
    observation = service.analysis(p(workspace), call["id"])["observations"][0]
    with service.db.transaction() as s:
        user = service.required(s, "users", workspace["user_id"], p(workspace))
        update(user, role="rep")
        other = put(
            s,
            "users",
            p(workspace)["tenant_id"],
            p(workspace)["seller_id"],
            {"name": "Different rep", "role": "rep"},
            now=service.clock(),
        )
        row = service.required(s, "call_attempts", call["id"], p(workspace))
        update(row, rep_id=other.id)
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.post(
            f"/v1/calls/{call['id']}/observations/{observation['id']}/review",
            headers=h,
            json={"expected_version": observation["version"], "decision": "retract", "reason": "Not my call"},
        ).status_code
        == 404
    )
    assert (
        c.put(
            "/v1/admin/optimization/conversation-vocabulary",
            headers=h,
            json={"expected_version": 1, "aliases": [], "reason": "Rep cannot publish team names"},
        ).status_code
        == 403
    )


def test_background_association_calculation_does_not_hold_a_write_transaction(workspace, app, monkeypatch):
    from contextlib import contextmanager

    from app.optimization import conversations

    service = app.state.service
    transaction = service.db.transaction
    original = conversations.conversation_associations
    writes = []
    calls = []

    @contextmanager
    def watched(write=True):
        writes.append(write)
        try:
            with transaction(write) as s:
                yield s
        finally:
            writes.pop()

    def calculate(*args, **kwargs):
        assert not any(writes), "Background association scoring held the database writer"
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.db, "transaction", watched)
    monkeypatch.setattr(conversations, "conversation_associations", calculate)
    service.refresh_optimization(p(workspace))
    assert calls


def test_unanswered_prior_call_does_not_hide_the_first_reviewed_conversation(clock):
    rows = cohort(clock) + cohort(clock, later=True)
    earlier = [
        {
            **row,
            "call_id": "unanswered-" + row["call_id"],
            "at": (clock.now - timedelta(days=80)).isoformat(),
            "observations": [],
        }
        for row in rows
    ]
    assert pattern(report(earlier + rows, clock))["status"] == "supported_correlation"


def test_new_synonym_vocabulary_needs_a_later_validation_period(clock):
    vocabulary = {
        "version": 2,
        "reviewed_at": clock.now.isoformat(),
        "aliases": [{"field": "concept", "source": "Purchasing signoff", "target": "Procurement review"}],
    }
    result = report(cohort(clock) + cohort(clock, later=True), clock, vocabulary=vocabulary)
    assert pattern(result)["status"] == "descriptive_only"
    assert result["validation_samples"] == 0
