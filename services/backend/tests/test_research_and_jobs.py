from datetime import timedelta

import pytest
from app.jobs.queue import JobQueue
from app.providers.batch import join_batch_results
from app.research.fetch import UnsafeURL, validate_url
from app.research.validation import ValidationFailure, validate_script


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://localhost",
        "http://[::1]/",
        "http://169.254.169.254/latest",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "file:///etc/passwd",
        "https://example.com:8443/",
        "http://user:password@example.com",
        "https://metadata.google.internal",
        "http://2130706433",
    ],
)
def test_ssrf_destinations_are_rejected(url):
    with pytest.raises(UnsafeURL):
        validate_url(url, resolver=lambda host: ["127.0.0.1"])


def test_safe_fetch_rejects_dns_rebinding():
    with pytest.raises(UnsafeURL):
        validate_url("https://example.com", resolver=lambda host: ["93.184.216.34", "10.0.0.1"])
    assert validate_url("https://example.com/services", resolver=lambda host: ["93.184.216.34"])


def test_script_rejects_wrong_company_stale_and_unsupported_claims(clock):
    facts = [
        {
            "fact_id": "f1",
            "entity_domain": "example.test",
            "statement": "Commercial repair",
            "source_id": "s1",
            "source_url": "https://example.test/services",
            "status": "supported",
            "expires_at": (clock.now + timedelta(days=1)).isoformat(),
        }
    ]
    script = {
        "lead_id": "lead-1",
        "opener": "Hi, this is Ada from Seller.",
        "screening_intro": "Ada from Seller calling about repair.",
        "discovery_questions": ["What needs repair?"],
        "fact_ids_used": ["f1"],
        "claims": [{"text": "Commercial repair", "fact_ids": ["f1"]}],
    }
    assert validate_script(script, facts, "example.test", "Seller", clock.now, approved=script)
    with pytest.raises(ValidationFailure):
        validate_script(script, facts, "other.test", "Seller", clock.now)
    facts[0]["expires_at"] = (clock.now - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationFailure):
        validate_script(script, facts, "example.test", "Seller", clock.now)
    script["opener"] = "We guarantee number one Google rankings and 10x revenue."
    with pytest.raises(ValidationFailure):
        validate_script(script, [], "example.test", "Seller", clock.now)


def test_batch_outputs_join_by_id_never_output_order():
    rows = [
        {"custom_id": "lead-b:v1", "response": {"body": {"value": "B"}}},
        {"custom_id": "lead-a:v1", "error": {"code": "failed"}},
    ]
    results = join_batch_results(["lead-a:v1", "lead-b:v1", "lead-c:v1"], rows)
    assert results["completed"] == {"lead-b:v1": {"value": "B"}}
    assert set(results["retry"]) == {"lead-a:v1", "lead-c:v1"}
    with pytest.raises(ValueError):
        join_batch_results(["lead-a:v1"], [{"custom_id": "unknown", "response": {"body": {}}}])


def test_durable_jobs_lease_retry_deadletter_and_idempotency(app, clock):
    q = JobQueue(app.state.db, clock)
    first = q.enqueue("research", {"lead_ids": ["one"]}, "job-key", tenant_id="test-tenant", max_attempts=2)
    assert q.enqueue("research", {}, "job-key", tenant_id="test-tenant")["id"] == first["id"]
    job = q.claim("research", "worker-a", lease_seconds=60)
    assert job["id"] == first["id"]
    assert q.claim("research", "worker-b") is None
    with pytest.raises(ValueError):
        q.complete(job["id"], "worker-b")
    q.fail(job["id"], "worker-a", "temporary provider failure", retry_seconds=0)
    assert q.claim("research", "worker-b")["attempts"] == 2
    q.fail(job["id"], "worker-b", "still failing", retry_seconds=0)
    assert q.get(job["id"])["status"] == "dead"


def test_expired_call_job_is_reconciliation_not_blind_retry(app, clock):
    q = JobQueue(app.state.db, clock)
    job = q.enqueue("call_control", {"call_id": "call"}, "dial-job", tenant_id="test-tenant")
    q.claim("call_control", "worker-a", lease_seconds=1)
    clock.now += timedelta(seconds=2)
    q.recover_expired()
    assert q.get(job["id"])["status"] == "reconcile"


def test_research_pipeline_publishes_versions_without_duplicates(workspace):
    c, h = workspace["client"], workspace["headers"]
    lead_ids = [lead["id"] for lead in c.get("/v1/workspace", headers=h).json()["leads"]]
    r = c.post("/v1/research/batches", headers=h, json={"lead_ids": lead_ids})
    assert r.status_code == 200, r.text
    assert r.json()["work_units"] >= 1
    c.post("/v1/admin/jobs/run", headers=h)
    packet = c.get(f"/v1/leads/{lead_ids[0]}/packet", headers=h).json()
    assert packet["script"]["ready_for_rep"]
    assert packet["script"]["script_version"]
    assert packet["research"]["facts"]
    assert all(fact["source_id"] for fact in packet["research"]["facts"])


def test_completed_preparation_can_refresh_after_evidence_expires(workspace, clock):
    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    first = c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead["id"]]}).json()
    c.post("/v1/admin/jobs/run", headers=h)
    clock.now += timedelta(days=31)
    token = c.post(
        "/v1/auth/session", json={"email": "operator@example.test", "password": "a-test-only-password-123"}
    ).json()["access_token"]
    h = {"Authorization": "Bearer " + token}
    again = c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead["id"]]}).json()
    assert again["jobs"][0]["id"] != first["jobs"][0]["id"]


def test_packet_falls_back_when_personalization_evidence_expires(workspace, clock):
    from app.db import update

    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead["id"]]})
    c.post("/v1/admin/jobs/run", headers=h)
    packet = c.get(f"/v1/leads/{lead['id']}/packet", headers=h).json()
    service = c.app.state.service
    with service.db.transaction() as s:
        script = next(
            v for v in service.scoped(s, "script_versions", workspace) if v.data["lead_id"] == lead["id"]
        )
        update(
            script,
            fallback=False,
            fact_ids_used=[fact["fact_id"] for fact in packet["research"]["facts"]],
            opener="Your company provides commercial repair services.",
        )
    clock.now += timedelta(days=31)
    token = c.post(
        "/v1/auth/session", json={"email": "operator@example.test", "password": "a-test-only-password-123"}
    ).json()["access_token"]
    h = {"Authorization": "Bearer " + token}
    packet = c.get(f"/v1/leads/{lead['id']}/packet", headers=h).json()
    assert packet["script"]["fallback"] is True
    assert not packet["research"]["facts"]


def test_completed_script_inputs_are_cached_before_another_paid_request(workspace, monkeypatch):
    from app.providers.inference import InferenceResult

    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    service = c.app.state.service
    calls = []

    class Provider:
        async def complete(self, instructions, context, schema):
            calls.append(context["lead_id"])
            baseline = context["approved_fallback"]
            return InferenceResult(
                {k: baseline[k] for k in schema["properties"]},
                {"input_tokens": 20, "output_tokens": 20},
                "cached-response",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    for _ in range(2):
        c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead["id"]]})
        c.post("/v1/admin/jobs/run", headers=h)
    assert calls == [lead["id"]]


def test_partial_preparation_retries_only_failed_objects(workspace, monkeypatch, clock):
    from app.providers.inference import InferenceResult

    c, h = workspace["client"], workspace["headers"]
    leads = c.get("/v1/workspace", headers=h).json()["leads"][:2]
    service = c.app.state.service
    calls = {lead["id"]: 0 for lead in leads}
    failing = leads[1]["id"]

    class Provider:
        async def complete(self, instructions, context, schema):
            ident = context["lead_id"]
            calls[ident] = calls.get(ident, 0) + 1
            if ident == failing and calls[ident] == 1:
                raise TimeoutError("One object has no provider receipt")
            return InferenceResult(
                {k: context["approved_fallback"][k] for k in schema["properties"]}, {}, "independent-response"
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    c.post("/v1/research/batches", headers=h, json={"lead_ids": [lead["id"] for lead in leads]})
    c.post("/v1/admin/jobs/run", headers=h)
    # The job completed with the failure recorded on its lead; nothing replays the batch.
    c.post("/v1/admin/jobs/run", headers=h)
    assert calls == {leads[0]["id"]: 1, failing: 1}
    clock.now += timedelta(minutes=5)
    principal = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    assert failing in {
        i for job in service.prepare_ahead(principal)["jobs"] for i in job["payload"]["lead_ids"]
    }
    c.post("/v1/admin/jobs/run", headers=h)
    assert calls[leads[0]["id"]] == 1 and calls[failing] == 2
