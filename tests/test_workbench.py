"""HTTP boundary tests and actual end-to-end local replay against a loopback FaultLab."""
import asyncio
import os
import re
import socket
import threading
import time
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select
from firstproof.config import Settings
from firstproof.db import Database, Experiment, Workload
from firstproof.faultlab import create_app as fault_app
from firstproof.web import create_app, session_csrf
from firstproof.worker import Worker


@pytest.fixture
def setup(tmp_path):
    cfg=Settings(mode='local',database_url='sqlite:///'+str(tmp_path/'web.db'),operator_token='a'*40,public_origin='http://testserver',faultlab_token='fault-secret',workload_concurrency=4)
    db=Database(cfg.database_url)
    with TestClient(create_app(cfg,db)) as client:yield cfg,db,client


def login(cfg,client):
    assert client.post('/login',data={'token':cfg.operator_token.get_secret_value()},headers={'origin':cfg.public_origin},follow_redirects=False).status_code==303
    return session_csrf(client.cookies['firstproof_session'],cfg.operator_token.get_secret_value())


def create_draft(cfg,client,csrf):
    r=client.post('/experiments',data={'csrf_token':csrf,'name':'Actual retry experiment','purpose':'pilot','source':'synthetic','workloads':'14','seed':'1729'},headers={'origin':cfg.public_origin},follow_redirects=False)
    assert r.status_code==303,r.text
    return r.headers['location'].rsplit('/',1)[1]


def test_auth_csrf_and_private_reports(setup):
    cfg,db,client=setup
    assert client.get('/').status_code==200
    assert client.post('/login',data={'token':'a'*40}).status_code==403
    csrf=login(cfg,client);eid=create_draft(cfg,client,csrf)
    assert client.get('/experiments/'+eid).status_code==200
    r=client.post('/experiments/'+eid+'/action',data={'csrf_token':'wrong','action':'run'},headers={'origin':cfg.public_origin})
    assert r.status_code==403
    r=client.post('/experiments/'+eid+'/action',data={'csrf_token':csrf,'action':'run','approval_hash':'wrong'},headers={'origin':cfg.public_origin})
    assert r.status_code==409
    client.cookies.clear()
    assert client.get('/experiments/'+eid).status_code==404
    assert client.get('/experiments/'+eid+'/export').status_code==401


def test_e2e_actual_http_replay(setup):
    cfg,db,client=setup
    # Use a real bound loopback socket, not a mocked policy outcome.
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.listen(128)
    server=uvicorn.Server(uvicorn.Config(fault_app(cfg.faultlab_token.get_secret_value()),log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.03)
        assert server.started
        cfg.faultlab_url=f'http://127.0.0.1:{port}'
        csrf=login(cfg,client);eid=create_draft(cfg,client,csrf)
        with db.tx() as s:ph=s.get(Experiment,eid).protocol_hash
        response=client.post('/experiments/'+eid+'/action',data={'csrf_token':csrf,'action':'run','approval_hash':ph},headers={'origin':cfg.public_origin},follow_redirects=False)
        assert response.status_code==303,response.text
        # Duplicate approval is rejected, not queued twice.
        assert client.post('/experiments/'+eid+'/action',data={'csrf_token':csrf,'action':'run','approval_hash':ph},headers={'origin':cfg.public_origin}).status_code==409
        w=Worker(cfg,db);job=db.claim(w.owner);assert job
        asyncio.run(w.process(job))
        with db.tx() as s:
            e=s.get(Experiment,eid)
            assert e.status=='replay_complete',e.error
            assert e.summary['observed']==14 and e.summary['complete']
            assert e.summary['native']['status']=='local_replay_not_native'
            assert not e.summary['decision']['eligible']
            assert len(list(s.scalars(select(Workload).where(Workload.experiment_id==eid))))==14
        page=client.get('/experiments/'+eid);assert page.status_code==200
        assert 'Measured outcomes' in page.text and 'Retain incumbent' in page.text
        assert client.get('/experiments/'+eid+'/export').json()['summary']['observed']==14
        assert client.post('/experiments/'+eid+'/action',data={'csrf_token':csrf,'action':'publish'},headers={'origin':cfg.public_origin},follow_redirects=False).status_code==303
        client.cookies.clear();assert client.get('/experiments/'+eid).status_code==200
        assert client.get('/experiments/'+eid+'/export').status_code==401
    finally:
        server.should_exit=True;thread.join(5);sock.close()
