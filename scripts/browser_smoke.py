"""Optional real-browser smoke test against an isolated local deployment.

Install Playwright (`pip install playwright`; `playwright install chromium`) or set
CHROMIUM_PATH to a system Chromium. This script never sends native experiment events.
The generated report is visibly local replay evidence, not official Flagsmith data.
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def wait(url, processes):
    for _ in range(100):
        if any(p.poll() is not None for p in processes): raise RuntimeError('Child service exited')
        try:
            urllib.request.urlopen(url,timeout=1).close();return
        except OSError:time.sleep(.2)
    raise RuntimeError('Startup timeout')


def main():
    root=Path(__file__).resolve().parents[1];os.chdir(root)
    out=Path(os.getenv('FP_SMOKE_OUTPUT',str(root/'artifacts')));out.mkdir(parents=True,exist_ok=True)
    web,fault=free_port(),free_port();base=f'http://127.0.0.1:{web}';token=secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix='firstproof-browser-') as tmp:
        env={**os.environ,'MODE':'local','DATABASE_URL':'sqlite:///'+str(Path(tmp)/'smoke.db'),'OPERATOR_TOKEN':token,'PUBLIC_ORIGIN':base,'SECURE_COOKIES':'false','FAULTLAB_TOKEN':secrets.token_urlsafe(32),'FAULTLAB_URL':f'http://127.0.0.1:{fault}'}
        subprocess.run([sys.executable,'-c','from firstproof.config import settings; from firstproof.db import Database; Database(settings().database_url).initialize()'],check=True,env=env)
        processes=[]
        try:
            for mod,port in [('firstproof.faultlab:app',fault),('firstproof.web:app',web)]:
                processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',mod,'--host','127.0.0.1','--port',str(port),'--no-access-log','--log-level','warning'],env=env))
            wait(f'http://127.0.0.1:{fault}/health',processes);wait(base+'/health/ready',processes)
            processes.append(subprocess.Popen([sys.executable,'-m','firstproof.worker'],env=env))
            with sync_playwright() as p:
                options={'headless':True}
                if os.getenv('CHROMIUM_PATH'):options['executable_path']=os.environ['CHROMIUM_PATH']
                browser=p.chromium.launch(**options)
                page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base);page.get_by_role('link',name='Operator sign-in').click();page.locator('#token').fill(token);page.get_by_role('button',name='Sign in').click()
                page.locator('#workloads').fill('30');page.get_by_role('button',name='Prepare protocol').click()
                page.get_by_role('button',name='Approve protocol & execute').click()
                page.get_by_role('heading',name='Measured outcomes').wait_for(timeout=90000)
                assert 'Local replay' in page.locator('header').inner_text()
                assert 'do not promote' in page.locator('main').inner_text()
                eid=page.url.rsplit('/',1)[1]
                export=page.request.get(base+'/experiments/'+eid+'/export');assert export.ok
                report=export.json();assert report['summary']['observed']==30 and report['summary']['complete'] and not report['summary']['decision']['eligible']
                (out/'local-replay-evidence.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                page.screenshot(path=str(out/'local-workbench.png'),full_page=True)
                page.get_by_role('button',name='Publish read-only report').click();page.get_by_role('button',name='Sign out').click()
                page.goto(base+'/experiments/'+eid);assert page.get_by_role('heading',name='Measured outcomes').is_visible()
                page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'local-mobile.png'),full_page=True)
                assert not errors,errors
                browser.close()
            print('PASS: Chromium operator workflow, 30 actual HTTP replay workloads, export, publication and mobile rendering.')
        finally:
            for process in processes:
                if process.poll() is None:process.terminate()
            for process in processes:
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill()


if __name__=='__main__':main()
