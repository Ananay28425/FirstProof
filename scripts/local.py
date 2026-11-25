"""One-command local replay on Windows/macOS/Linux; no provider credentials required.

This launches three explicit loopback processes and stops them on Ctrl+C. It does not
simulate native Flagsmith results. It intentionally overrides MODE to local.
"""
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[1]
    os.chdir(root)
    token=secrets.token_urlsafe(32)
    env={**os.environ,'MODE':'local','DATABASE_URL':'sqlite:///./local-replay.db',
         'OPERATOR_TOKEN':token,'PUBLIC_ORIGIN':'http://127.0.0.1:8000','SECURE_COOKIES':'false',
         'FAULTLAB_URL':'http://127.0.0.1:8001','FAULTLAB_TOKEN':secrets.token_urlsafe(32)}
    # Initialize before parallel process startup to avoid schema creation races.
    subprocess.run([sys.executable,'-c','from firstproof.config import settings; from firstproof.db import Database; Database(settings().database_url).initialize()'],env=env,check=True)
    processes=[]
    try:
        for module,port in [('firstproof.faultlab:app','8001'),('firstproof.web:app','8000')]:
            processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',module,'--host','127.0.0.1','--port',port,'--no-access-log'],env=env))
        for port,path in [(8001,'health'),(8000,'health/ready')]:
            for _ in range(60):
                if any(p.poll() is not None for p in processes): raise RuntimeError('Service exited; inspect console')
                try:
                    urllib.request.urlopen(f'http://127.0.0.1:{port}/{path}',timeout=1).close();break
                except OSError: time.sleep(.25)
            else: raise RuntimeError('Local startup health check failed')
        processes.append(subprocess.Popen([sys.executable,'-m','firstproof.worker'],env=env))
        print('\nFirstProof local replay: http://127.0.0.1:8000\nOperator token (local only): '+token+'\nCtrl+C stops all services.',flush=True)
        while all(p.poll() is None for p in processes): time.sleep(1)
        raise RuntimeError('A service exited; inspect console')
    except KeyboardInterrupt: pass
    finally:
        for p in processes:
            if p.poll() is None: p.terminate()
        for p in processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill()


if __name__=='__main__': main()
