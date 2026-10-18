"""A27/A29/A50: one rep's context must not decrypt the seller's full history."""

from datetime import timedelta

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

    monkeypatch.setattr("process_result_value", counted)
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
