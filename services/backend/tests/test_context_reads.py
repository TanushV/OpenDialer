"""A27/A29/A50: one rep's context must not decrypt the seller's full history."""

from datetime import timedelta

import pytest
from app.db import MODELS, get, put, update
from conftest import ready, start
from sqlalchemy import inspect, select
from test_live_memory import turn
from test_remediation_privacy import principal


def history(workspace, app, clock, count=150):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        lead = get(s, "leads", call["lead_id"])
        domain = lead.data["domain"]
        for i in range(count):
            at = clock.now - timedelta(minutes=count - i + 1)
            old = put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "rep_id": p["user_id"],
                    "started_at": at.isoformat(),
                    "state": "FINALIZED",
                    "outcome": "right_party",
                },
                now=at,
            )
            put(
                s,
                "notes",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": old.id,
                    "human_text": f"Prior human note {i}",
                    "notes": {"summary": f"Prior conversation {i}", "problems": [], "objections": []},
                },
                key=old.id,
                now=at,
            )
            put(
                s,
                "commitments",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "status": "completed",
                    "action": f"Old promise {i}",
                },
                now=at,
            )
            for kind in ("research_facts", "research_sources"):
                put(
                    s,
                    kind,
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "domain": f"unrelated-{i}.example",
                        "status": "supported",
                        "fact_id": f"unrelated-{i}",
                        "expires_at": (clock.now + timedelta(days=1)).isoformat(),
                        "statement": "Unrelated company fact",
                        "text": "Unrelated source",
                    },
                    now=at,
                )
            put(
                s,
                "script_versions",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": f"unrelated-{i}",
                    "opener": "Unrelated opener",
                },
                now=at,
            )
        source = put(
            s,
            "research_sources",
            p["tenant_id"],
            p["seller_id"],
            {
                "domain": domain,
                "url": f"https://{domain}/services",
                "text": "Our services include repairs.",
            },
            now=clock.now,
        )
        fact = put(
            s,
            "research_facts",
            p["tenant_id"],
            p["seller_id"],
            {
                "domain": domain,
                "fact_id": "current",
                "status": "supported",
                "source_id": source.id,
                "expires_at": (clock.now + timedelta(days=1)).isoformat(),
                "statement": "The firm offers repairs.",
            },
            now=clock.now,
        )
        commitment = put(
            s,
            "commitments",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "status": "confirmed",
                "action": "Discuss the next step",
            },
            now=clock.now,
        )
    turn(service, p, call, "live")
    return service, p, call, fact.id, commitment.id


def count_decrypted(app, monkeypatch):
    counts = []
    original = app.state.db.cipher.decrypt

    def counted(*args, **kwargs):
        counts.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.db.cipher, "decrypt", counted)
    return counts


def test_helper_reads_bounded_related_context_including_latest_prior_notes(
    workspace, app, clock, monkeypatch
):
    service, p, call, fact, promise = history(workspace, app, clock)
    decrypted = count_decrypted(app, monkeypatch)
    request = service._help_context(p, call["id"], True, "Help clarify the next step")[0]
    assert [f["id"] for f in request["facts"]] == [fact]
    assert [c["id"] for c in request["prior_commitments"]] == [promise]
    assert [c["human_notes"] for c in request["prior_call_context"]] == [
        f"Prior human note {i}" for i in (147, 148, 149)
    ]
    assert len(decrypted) <= 60, f"Live help decrypted {len(decrypted)} records for one call"


def test_lead_packet_keeps_recent_history_and_discloses_omissions(workspace, app, clock, monkeypatch):
    service, p, call, fact, _ = history(workspace, app, clock)
    decrypted = count_decrypted(app, monkeypatch)
    packet = service.packet(p, call["lead_id"])
    assert [f["id"] for f in packet["research"]["facts"]] == [fact]
    assert len(packet["previous_calls"]) <= 20
    assert packet["context_coverage"]["calls_total"] == 151
    assert packet["context_coverage"]["calls_omitted"] == 131
    assert packet["prospect_brief"]["summary"] == "Prior conversation 149"
    assert len(decrypted) <= 100, f"Lead packet decrypted {len(decrypted)} records"


def test_raw_context_does_not_scan_expired_long_call_metadata(workspace, app, clock, monkeypatch):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(600):
            put(
                s,
                "transcript_segments",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": call["id"],
                    "segment_id": str(i),
                    "revision": 1,
                    "is_final": True,
                    "raw_expires_at": (clock.now - timedelta(seconds=1)).isoformat(),
                },
                key=f"{call['id']}:{i}",
                now=clock.now - timedelta(minutes=6),
            )
    turn(service, p, call, "recent")
    decrypted = count_decrypted(app, monkeypatch)
    with service.db.transaction(False) as s:
        segments = service.transcript_rows(s, p, call["id"])
    assert [item["segment_id"] for item in segments] == ["recent"]
    assert len(decrypted) <= 2, f"Loaded {len(decrypted)} metadata rows for a single live chunk"


def test_context_index_tracks_corrections_and_is_scoped_without_plaintext_domain(workspace, app, clock):
    service, p, call, fact, _ = history(workspace, app, clock, count=1)
    with service.db.transaction() as s:
        row = get(s, "research_facts", fact)
        original_domain = row.data["domain"]
        assert hasattr(row, "ctx_key")
        assert row.ctx_key and original_domain not in row.ctx_key
        before = row.ctx_key
        update(row, domain="corrected.example", status="withdrawn")
    with service.db.transaction(False) as s:
        row = get(s, "research_facts", fact)
        assert row.ctx_key != before and row.ctx_status == "withdrawn"
    request = service._help_context(p, call["id"], True, "Context please")[0]
    assert not request["facts"]


def test_expired_or_other_seller_fact_never_enters_bounded_context(workspace, app, clock):
    service, p, call, fact, _ = history(workspace, app, clock, count=0)
    with service.db.transaction() as s:
        original = get(s, "research_facts", fact)
        put(s, "research_facts", p["tenant_id"], "another-seller", dict(original.data), now=clock.now)
        update(original, expires_at=clock.now.isoformat())
    request = service._help_context(p, call["id"], True, "Context please")[0]
    assert not request["facts"]


def test_context_indexes_are_present_for_both_databases(app):
    db = app.state.db
    inspector = inspect(db.engine)
    for kind in (
        "research_facts",
        "research_sources",
        "script_versions",
        "consent_evidence",
        "transcript_segments",
    ):
        assert {"ctx_key", "ctx_index_version"} <= {column["name"] for column in inspector.get_columns(kind)}
        assert any(
            index["column_names"][:3] == ["tenant_id", "seller_id", "ctx_key"]
            for index in inspector.get_indexes(kind)
        )


def test_selection_loads_only_consents_for_candidate_endpoints(workspace, app, monkeypatch):
    p, service = principal(workspace), app.state.service
    with service.db.transaction() as s:
        for i in range(600):
            put(
                s,
                "consent_evidence",
                p["tenant_id"],
                p["seller_id"],
                {
                    "endpoint_id": f"unrelated-{i}",
                    "permissions": ["live"],
                    "revoked": False,
                },
            )
    ready(workspace)
    decrypted = count_decrypted(app, monkeypatch)
    result = service.reserve(p, workspace["campaign_id"])
    assert result["reservation_id"]
    assert len(decrypted) <= 120, f"Reservation decrypted {len(decrypted)} records with three candidates"


def test_context_index_backfill_preserves_identity_version_and_human_data(tmp_path, clock):
    import json
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from app.db import Database
    from app.migrate import migrate_database
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{tmp_path}/prior.db"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0004_work_intervals")
    engine = create_engine(url)
    data = {"domain": "private.example", "statement": "Preserved fact", "status": "supported"}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO research_facts (id,tenant_id,seller_id,data,version,created_at) VALUES ('saved','t','s',:data,7,:at)"
            ),
            {"data": json.dumps(data), "at": clock.now},
        )
    engine.dispose()
    for _ in range(2):
        migrate_database(url)
        db = Database(url, encryption_key="test-context-index")
        with db.transaction(False) as s:
            row = get(s, "research_facts", "saved")
            assert row.version == 7 and row.data == data
            assert row.ctx_key != data["domain"] and row.ctx_index_version == 1
            assert (
                s.scalar(
                    select(MODELS["research_facts"]).where(MODELS["research_facts"].ctx_status == "supported")
                ).id
                == "saved"
            )
        db.engine.dispose()


def test_unknown_commitment_status_stays_ahead_of_completed_history(workspace, app, clock):
    service, p, call, _, _ = history(workspace, app, clock, count=25)
    with service.db.transaction() as s:
        promise = put(
            s,
            "commitments",
            p["tenant_id"],
            p["seller_id"],
            {"lead_id": call["lead_id"], "action": "Confirm the older unclassified promise"},
            now=clock.now - timedelta(days=30),
        )
    assert promise.id in {item["id"] for item in service.packet(p, call["lead_id"])["commitments"]}


def test_packet_tasks_are_bounded_with_open_work_ahead_of_closed_history(workspace, app, clock, monkeypatch):
    service, p, call, _, _ = history(workspace, app, clock, count=0)
    with service.db.transaction() as s:
        for i in range(300):
            put(
                s,
                "followup_tasks",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": call["lead_id"],
                    "status": "completed",
                    "action": f"Old task {i}",
                },
                now=clock.now,
            )
        task = put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": call["lead_id"],
                "status": "open",
                "action": "Due callback",
            },
            now=clock.now - timedelta(days=30),
        )
    decrypted = count_decrypted(app, monkeypatch)
    packet = service.packet(p, call["lead_id"])
    assert len(packet["tasks"]) <= 20
    assert packet["tasks"][0]["id"] == task.id
    assert packet["context_coverage"]["tasks_omitted"] == 281
    assert len(decrypted) <= 100


@pytest.mark.parametrize("domain", [None, ""])
def test_missing_company_domain_does_not_merge_unknown_company_evidence(workspace, app, clock, domain):
    service, p, call, fact_id, _ = history(workspace, app, clock, count=0)
    with service.db.transaction() as s:
        update(get(s, "leads", call["lead_id"]), domain=domain)
        update(get(s, "research_facts", fact_id), domain=domain)
    assert not service.packet(p, call["lead_id"])["research"]["facts"]
    assert not service._help_context(p, call["id"], True, "Context please")[0]["facts"]


@pytest.mark.asyncio
async def test_prepared_script_keeps_its_older_support_when_new_facts_fill_the_context_page(
    workspace, app, clock, monkeypatch
):
    from app.providers.inference import InferenceResult
    from test_preparation_completion import configure

    configure(workspace)
    service, p, lead_id = app.state.service, principal(workspace), workspace["lead_ids"][0]
    await service.prepare(p, [lead_id])
    fact = service.packet(p, lead_id)["research"]["facts"][0]

    class Provider:
        async def complete(self, instructions, context, schema):
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": fact["statement"], "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                "old-fact-script",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    await service.prepare(p, [lead_id])
    assert service.packet(p, lead_id)["script"]["fallback"] is False
    with service.db.transaction() as s:
        for i in range(40):
            put(
                s,
                "research_facts",
                p["tenant_id"],
                p["seller_id"],
                {
                    **fact,
                    "fact_id": f"new-{i}",
                    "statement": f"More recent supported fact {i}.",
                },
                now=clock.now + timedelta(seconds=1),
            )
    packet = service.packet(p, lead_id)
    assert packet["script"]["fallback"] is False
    assert fact["fact_id"] in {item["fact_id"] for item in packet["research"]["facts"]}
    # A bound reference must still respect withdrawal and the current source version.
    with service.db.transaction() as s:
        update(get(s, "research_facts", fact["id"]), status="withdrawn")
    assert service.packet(p, lead_id)["script"]["fallback"] is True


def test_helper_keeps_current_support_for_the_frozen_script(workspace, app, clock):
    service, p, call, fact_id, _ = history(workspace, app, clock, count=0)
    with service.db.transaction() as s:
        fact = get(s, "research_facts", fact_id)
        update(fact, fact_id=fact_id)
        row = get(s, "call_attempts", call["id"])
        update(row, script_snapshot={**row.data["script_snapshot"], "fact_ids_used": [fact_id]})
        for i in range(12):
            put(
                s,
                "research_facts",
                p["tenant_id"],
                p["seller_id"],
                {
                    **fact.data,
                    "fact_id": f"new-{i}",
                    "statement": f"A newer company fact {i}",
                },
                now=clock.now + timedelta(seconds=1),
            )
    context = service._help_context(p, call["id"], True, "Clarify this script")[0]
    assert fact_id in {fact["fact_id"] for fact in context["facts"]}
    with service.db.transaction() as s:
        update(get(s, "research_facts", fact_id), status="withdrawn")
    context = service._help_context(p, call["id"], True, "Clarify this script")[0]
    assert fact_id not in {fact["fact_id"] for fact in context["facts"]}


def test_helper_preserves_human_notes_but_does_not_reuse_invalidated_ai_summary(workspace, app, clock):
    service, p, call, _, _ = history(workspace, app, clock, count=1)
    with service.db.transaction() as s:
        note = service.scoped(s, "notes", p)[0]
        update(note, stale=True)
    prior = service._help_context(p, call["id"], True, "Context please")[0]["prior_call_context"][0]
    assert prior["human_notes"] == "Prior human note 0"
    assert not prior["rundown"]
    assert prior["analysis_stale"] is True


def test_context_migration_still_accepts_the_documented_unversioned_bootstrap(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from app.migrate import migrate_database
    from sqlalchemy import create_engine, text

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path}/bootstrap.db"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0001_initial")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()
    migrate_database(url)
    engine = create_engine(url)
    assert "ctx_key" in {c["name"] for c in inspect(engine).get_columns("research_facts")}
    engine.dispose()
