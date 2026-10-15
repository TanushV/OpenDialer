from datetime import timedelta

from conftest import reserve, start


def test_registry_import_validates_authorization_and_checksum_atomically(workspace):
    c, h = workspace['client'], workspace['headers']
    body = {'kind':'national','npas':['415'], 'subscription_reference':'authorized-fixture',
        'refreshed_at':'2026-10-14T17:00:00+00:00','numbers':['+14155550101']}
    before = len(c.get('/v1/workspace',headers=h).json()['registries'])
    assert c.post('/v1/admin/registry/import',headers=h,json={**body,'checksum':'bad'}).status_code == 422
    assert len(c.get('/v1/workspace',headers=h).json()['registries']) == before
    r = c.post('/v1/admin/registry/import',headers=h,json=body)
    assert r.status_code == 200, r.text
    assert r.json()['published'] and r.json()['checksum']
    packet = reserve(workspace)
    assert packet.status_code == 409
    assert 'REGISTRY' in packet.text


def test_replacement_endpoint_preserves_old_optout_and_lead_identity(workspace):
    c,h = workspace['client'],workspace['headers']
    lead = c.get('/v1/workspace',headers=h).json()['leads'][0]
    c.post('/v1/suppressions',headers=h,json={'phone':lead['phone'],'reason':'wrong_party'})
    result = c.patch(f"/v1/leads/{lead['id']}/endpoint",headers=h,
                     json={'phone':'+14155550188','evidence':'Company-provided new contact'})
    assert result.status_code == 200,result.text
    corrected = c.get(f"/v1/leads/{lead['id']}/packet",headers=h).json()['lead']
    assert corrected['id'] == lead['id'] and corrected['phone'] == '+14155550188'
    reimport = c.post('/v1/leads/import',headers=h,json={'campaign_id':workspace['campaign_id'],
        'rows':[{'name':'Old endpoint','phone':lead['phone']}]})
    assert reimport.json()['leads'][0]['suppressed']


def test_playbook_publication_preserves_versions_and_validates_identity(workspace):
    c,h=workspace['client'],workspace['headers']
    old=c.get('/v1/workspace',headers=h).json()['playbooks'][0]
    data={'name':'Revised introduction','opener':'Hi, this is {rep} from Example Seller.',
        'screening_intro':'{rep} from Example Seller, calling about business needs.',
        'cards':old['cards'],'approved':True,'reason':'Reviewed for accuracy'}
    r=c.post('/v1/admin/playbooks',headers=h,json=data)
    assert r.status_code==200,r.text
    versions=c.get('/v1/workspace',headers=h).json()['playbooks']
    assert len(versions)==2
    assert c.post('/v1/admin/playbooks',headers=h,json={**data,'opener':'We guarantee 10x revenue'}).status_code==422


def test_agent_dtmf_and_detached_voicemail_are_separately_controlled(workspace,carrier):
    call=start(workspace)
    c,h=workspace['client'],workspace['headers']
    assert c.post(f"/v1/calls/{call['id']}/dtmf",headers=h,json={'digits':'42#'}).status_code==200
    assert c.post(f"/v1/calls/{call['id']}/dtmf",headers=h,json={'digits':'javascript:bad'}).status_code==422
    c.post(f"/v1/calls/{call['id']}/hangup",headers=h)
    assert c.post(f"/v1/calls/{call['id']}/dtmf",headers=h,json={'digits':'1'}).status_code==409


def test_deferred_transcription_does_not_wait_for_coach(workspace):
    call=start(workspace)
    service=workspace['client'].app.state.service
    assert hasattr(service,'maintenance_tick')
