"""A39/A50: opening one call must not decrypt unrelated review history."""

import pytest
from app.db import doc, get, put
from conftest import start
from sqlalchemy import inspect, text
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


@pytest.mark.parametrize('operation', ['analysis', 'notes'])
def test_review_reads_only_the_selected_calls_notes_ratings_and_comments(workspace, app, clock, monkeypatch, operation):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    first = service.analysis(p, call['id'])
    with service.db.transaction() as s:
        comment = put(s, 'comments', p['tenant_id'], p['seller_id'], {'call_id': call['id'], 'text': 'Human review of this call'}, now=clock.now)
        for i in range(400):
            put(s, 'notes', p['tenant_id'], p['seller_id'], {'call_id': f'old-{i}', 'human_text': 'Unrelated note'}, key=f'old-{i}', now=clock.now)
            put(s, 'scorecards', p['tenant_id'], p['seller_id'], {'call_id': f'old-{i}'}, key=f'old-{i}', now=clock.now)
            put(s, 'reviews', p['tenant_id'], p['seller_id'], {'scorecard_id': f'old-card-{i}', 'reason': 'Unrelated review'}, now=clock.now)
            put(s, 'comments', p['tenant_id'], p['seller_id'], {'call_id': f'old-{i}', 'text': 'Unrelated comment'}, now=clock.now)
    decoded = count_decrypted(app, monkeypatch)
    if operation == 'analysis':
        result = service.analysis(p, call['id'])
        assert [r['id'] for r in result['comments']] == [comment.id]
        assert result['scorecard_id'] == first['scorecard_id']
        assert result['reviews'] == []
    else:
        result = service.edit_notes(p, call['id'], first['notes_version'], 'This human correction survives')
        assert result['human_notes'] == 'This human correction survives'
    assert 'Unrelated' not in str(result)
    assert len(decoded) <= 40, len(decoded)


@pytest.mark.parametrize('kind,field', [('reviews', 'scorecard_id'), ('comments', 'call_id')])
def test_review_relationship_index_backfill_preserves_human_content(workspace, app, kind, field):
    service, p = app.state.service, principal(workspace)
    assert {'ctx_key', 'ctx_index_version'} <= {r['name'] for r in inspect(service.db.engine).get_columns(kind)}
    with service.db.transaction() as s:
        row = put(s, kind, p['tenant_id'], p['seller_id'], {field: 'original-id', 'text': 'Original human correction'})
        before = doc(row)
        s.flush()
        s.execute(text(f'UPDATE {kind} SET ctx_key=NULL,ctx_index_version=NULL WHERE id=:id'), {'id': row.id})
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        current = get(s, kind, before['id'])
        assert doc(current) == before
        assert current.ctx_key == 'original-id'
    assert f'ix_{kind}_context' in {i['name'] for i in inspect(service.db.engine).get_indexes(kind)}


def test_review_relationship_upgrade_is_additive_repeatable(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from app.migrate import migrate_database
    from sqlalchemy import create_engine

    root = Path(__file__).resolve().parents[1]
    url = f'sqlite:///{tmp_path}/review-upgrade.db'
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'migrations'))
    config.set_main_option('sqlalchemy.url', url)
    command.upgrade(config, '0014_current_knowledge')
    engine = create_engine(url)
    with engine.begin() as c:
        for table in ('comments', 'reviews'):
            c.execute(text(f"INSERT INTO {table} (id,tenant_id,seller_id,data,version,created_at) VALUES ('retained','t','s',:data,9,'2026-10-15 00:00:00')"), {'data': '{"protected":"preserved"}'})
    migrate_database(url)
    migrate_database(url)
    for table in ('comments', 'reviews'):
        assert 'ctx_key' in {r['name'] for r in inspect(engine).get_columns(table)}
        with engine.connect() as c:
            assert c.execute(text(f'SELECT id,version,data FROM {table}')).one() == ('retained', 9, '{"protected":"preserved"}')
    engine.dispose()
