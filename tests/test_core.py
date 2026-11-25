"""Focused tests for safety invariants, native schema and durable job state."""
import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
import httpx
import pytest
from sqlalchemy import select
from firstproof.contracts import default_protocol, Protocol, digest, Family
from firstproof.db import Database, Experiment, Job
from firstproof.faultlab import create_app
from firstproof.integrations import github_issue_endpoint, reconcile_native
from firstproof.policies import retry_after, retry_delay, execute, CONTROL, CANDIDATE
from firstproof.verification import summarize, verify, release_decision
from firstproof.worker import manifest


def test_retry_after_parsing():
    assert retry_after('3600')==3600
    assert retry_after('Wed, 01 Jan 2025 00:00:05 GMT',datetime(2025,1,1,tzinfo=timezone.utc))==5
    assert retry_after('invalid')==0


@pytest.mark.parametrize('policy',[CONTROL,CANDIDATE])
def test_server_hint_never_truncated(policy):
    assert retry_delay(policy,4,.8,3600,.1)==3600
    assert retry_delay(policy,1,.5,1,2)>=1


def test_candidate_preserves_budget():
    assert retry_delay(CANDIDATE,3,1,0,.2)<retry_delay(CONTROL,3,1,0,.2)


def test_manifest_repeatable_and_separate():
    p=default_protocol()
    rows=list(manifest(p,'e')); assert rows==list(manifest(p,'e'))
    assert {i[0] for i in rows}.isdisjoint({i[0] for i in manifest(p,'e',holdout=True)})
    assert len(rows)==p.workloads


@pytest.mark.parametrize('url',['http://github.com/x/y/issues/1','https://github.com.evil/x/y/issues/1','https://github.com/x/y/issues/1?x=1','http://127.0.0.1/admin','https://github.com/../y/issues/1','https://github.com@127.0.0.1/x/y/issues/1'])
def test_source_adapter_rejects_arbitrary_destinations(url):
    assert github_issue_endpoint(url) is None


def test_source_adapter_accepts_exact_public_issue():
    assert github_issue_endpoint('https://github.com/urllib3/urllib3/issues/1338')=='https://api.github.com/repos/urllib3/urllib3/issues/1338'


@pytest.mark.asyncio
@pytest.mark.parametrize('policy',[CONTROL,CANDIDATE])
@pytest.mark.parametrize('family,expected_success',[('healthy',True),('corrupt_response',False),('excessive_retry_after',False)])
async def test_faultlab_contract(policy,family,expected_success):
    app=create_app('test-token');headers={'X-Faultlab-Token':'test-token'}
    p=default_protocol().model_copy(update={'deadline_ms':500})
    profile={'family':family,'outage_ms':200,'delay_ms':5,'retry_after':1,'expected':'a'*24}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://faultlab') as client:
        assert (await client.put('/profiles/id',json=profile,headers=headers)).status_code==200
        result=await execute(client,'http://faultlab/search/id',policy,p.deadline_ms,p.max_attempts,1,headers=headers)
        trace=(await client.get('/observations/id',headers=headers)).json()['requests']
        verified=verify(result,trace,profile['expected'],p)
        assert verified['success']==expected_success
        assert not verified['invalid_accepted'] and not verified['hint_violation']
        assert verified['trace_complete']
        if family=='excessive_retry_after': assert len(trace)==1
        assert (await client.put('/profiles/id',json=profile,headers=headers)).status_code==409


@pytest.mark.asyncio
async def test_faultlab_denies_anonymous_access():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app('secret')),base_url='http://faultlab') as c:
        assert (await c.get('/observations/id')).status_code==403


def fake_summary():
    """Synthetic unit-test inputs only; never appear as application results."""
    p=default_protocol(workloads=2000,purpose='main')
    rows=[]
    for variant,success in [(CONTROL,500),(CANDIDATE,900)]:
        for i in range(1000):
            rows.append({'assignment':variant,'family':'healthy','success':i<success,'elapsed_ms':10,'attempt_count':1,'reason':'unit_test','invalid_accepted':False,'attempt_violation':False,'hint_violation':False,'deadline_violation':False,'trace_complete':True})
    return p,summarize(rows,p)


def test_native_reconciliation_and_gate():
    p,s=fake_summary()
    raw={'results':{'as_of':'2026-11-26T00:00:00Z','last_error_at':None,'payload':{'srm_p_value':1.0,'metrics':[{'metric_id':1,'variants':{'control':{'n':1000,'sum':500},'context_aware':{'n':1000,'sum':900}},'inference':{'context_aware':{'chance_to_win':.999,'ci_low':.3,'ci_high':1.1}}}]}}}
    exposure={'exposures':{'last_error_at':None,'payload':{'excluded_identities':0}}}
    native=reconcile_native(raw,s,1,exposure)
    assert native['reconciled']
    assert not release_decision(s,p,native,False)['eligible']
    assert release_decision(s,p,native,True)['eligible']
    raw['results']['payload']['metrics'][0]['variants']['control']['sum']=499
    assert not reconcile_native(raw,s,1,exposure)['reconciled']
    assert not release_decision(s,p,{},True)['eligible']


def test_corrupt_guard_blocks_candidate():
    p,s=fake_summary();s['groups'][CANDIDATE]['violations']['invalid_accepted']=1
    assert not release_decision(s,p,{'reconciled':True,'win_probability':.99,'srm_warning':False,'credible_interval':[.1,.3]},True)['eligible']


@pytest.fixture
def db(tmp_path):
    url=os.getenv('TEST_POSTGRES_URL') or 'sqlite:///'+str(tmp_path/'test.db')
    database=Database(url);database.initialize()
    yield database
    if os.getenv('TEST_POSTGRES_URL'):
        from firstproof.db import Base
        Base.metadata.drop_all(database.engine)
    database.engine.dispose()


def test_lease_recovery_and_no_duplicate_claim(db):
    p=default_protocol();eid=str(uuid.uuid4())
    with db.tx() as s:
        s.add(Experiment(id=eid,name='Test job',mode='local',status='queued',protocol=p.model_dump(mode='json'),protocol_hash=digest(p)))
        s.flush();jid=db.enqueue(s,eid,'run',5)
    assert db.claim('a',lease=-1)==(jid,eid,'run')
    assert db.claim('b') is None
    db.recover()
    with db.tx() as s:
        assert s.get(Experiment,eid).status=='interrupted'
        assert s.get(Job,jid).state=='failed'
        with pytest.raises(RuntimeError):db.fence(s,jid,'a')
