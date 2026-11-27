"""FastAPI workbench. Public reports are opt-in; privileged work is operator-only.

A single sandbox operator is intentional for this competition release. This is not
an enterprise multi-tenant authentication system. No mutation is performed on GET.
"""
import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import select, func
from .config import settings
from .contracts import Protocol, cooldown_protocol, default_protocol, digest
from .db import Database, Experiment, Workload, Audit, Job, Budget
from .verification import release_decision

ROOT = Path(__file__).parent
SESSION_COOKIE = 'firstproof_session'


def make_session(secret):
    """Signed, short-lived random operator session with a session-bound CSRF token."""
    body=f'{int(time.time())+8*3600}.{secrets.token_urlsafe(24)}'
    sig=hmac.new(secret.encode(),body.encode(),hashlib.sha256).hexdigest()
    return body+'.'+sig


def session_csrf(token, secret):
    """Validate signature and expiration before deriving the anti-CSRF value."""
    if not token or not secret or len(token)>200: return None
    try:
        expiry, nonce, signature=token.split('.')
        if int(expiry)<=time.time(): return None
        body=f'{expiry}.{nonce}'
        expected=hmac.new(secret.encode(),body.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature): return None
        return hmac.new(secret.encode(),('csrf:'+body).encode(),hashlib.sha256).hexdigest()
    except (ValueError,TypeError): return None


def create_app(cfg=None, db=None):
    cfg=cfg or settings(); db=db or Database(cfg.database_url)
    secret=cfg.operator_token.get_secret_value()
    templates=Jinja2Templates(directory=str(ROOT/'templates'))

    @asynccontextmanager
    async def lifespan(app):
        if cfg.mode=='local': db.initialize()
        yield
        db.engine.dispose()

    app=FastAPI(title='FirstProof',version='0.1.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(cfg.public_origin).hostname or 'localhost', '127.0.0.1', 'localhost'])
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

    @app.middleware('http')
    async def protect(request: Request, call_next):
        """Security headers and bounded form bodies; hosting proxy also enforces limits."""
        if request.method in {'POST','PUT','PATCH','DELETE'}:
            try: length=int(request.headers.get('content-length','-1'))
            except ValueError: return Response(status_code=400)
            # Require Content-Length on our small forms. Reject streaming uploads.
            if length<0 or length>65536: return Response('Invalid request size',status_code=413)
            if request.headers.get('origin') != cfg.public_origin.rstrip('/'):
                return Response('Origin check failed',status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        if cfg.secure_cookies: response.headers['Strict-Transport-Security']='max-age=31536000'
        if not request.url.path.startswith('/static/'): response.headers['Cache-Control']='no-store'
        return response

    def csrf(request): return session_csrf(request.cookies.get(SESSION_COOKIE),secret)

    def operator(request:Request):
        if not csrf(request): raise HTTPException(401,'Operator sign-in required')

    def authorize(request, submitted):
        token=csrf(request)
        if not token or not hmac.compare_digest(token,submitted): raise HTTPException(403,'Invalid approval session')

    def render(request,name,**ctx):
        return templates.TemplateResponse(request=request,name=name,context={'mode':cfg.mode,'operator':bool(csrf(request)),'csrf':csrf(request),'max_workloads':cfg.max_workloads,**ctx})

    def get_visible(s,eid,request):
        e=s.get(Experiment,eid)
        if not e or not (csrf(request) or e.remote.get('published')): raise HTTPException(404,'Experiment not found')
        return e

    @app.get('/health/live')
    def live(): return {'status':'ok'}

    @app.get('/health/ready')
    def ready():
        try:
            with db.tx() as s: s.execute(select(Budget.key).limit(1)).first()
            return {'status':'ready','mode':cfg.mode}
        except Exception: return JSONResponse({'status':'not_ready'},status_code=503)

    @app.get('/',response_class=HTMLResponse)
    def index(request:Request):
        with db.tx() as s:
            rows=list(s.scalars(select(Experiment).order_by(Experiment.created_at.desc()).limit(100)))
            if not csrf(request): rows=[e for e in rows if e.remote.get('published')]
            return render(request,'index.html',experiments=rows,configured=bool(secret))

    @app.get('/login',response_class=HTMLResponse)
    def login_page(request:Request): return render(request,'login.html',error='')

    @app.post('/login')
    def login(request:Request,token:str=Form(max_length=256)):
        # The token is a 256-bit randomly generated secret, not a human password.
        if not secret or len(secret)<32 or not hmac.compare_digest(token.encode(),secret.encode()):
            return render(request,'login.html',error='Invalid operator token or token not configured.')
        response=RedirectResponse('/',status_code=303)
        response.set_cookie(SESSION_COOKIE,make_session(secret),max_age=8*3600,httponly=True,secure=cfg.secure_cookies,samesite='strict',path='/')
        return response

    @app.post('/logout',dependencies=[Depends(operator)])
    def logout(request:Request,csrf_token:str=Form()):
        authorize(request,csrf_token)
        response=RedirectResponse('/',status_code=303);response.delete_cookie(SESSION_COOKIE,path='/');return response

    @app.post('/experiments',dependencies=[Depends(operator)])
    def create(request:Request, csrf_token:str=Form(), name:str=Form(min_length=5,max_length=150), purpose:str=Form(), workloads:int=Form(), source:str=Form(), seed:int=Form(default=1729)):
        authorize(request,csrf_token)
        if purpose not in {'pilot','main'} or source not in {'pinned','research','synthetic'} or not 2<=workloads<=cfg.max_workloads:
            raise HTTPException(400,'Invalid protocol selection')
        if cfg.mode=='live' and purpose=='main' and source=='synthetic':
            raise HTTPException(400,'Main live experiment requires source-backed evidence')
        if cfg.mode=='local' and purpose=='main': raise HTTPException(400,'Local mode is replay only')
        try: p=(cooldown_protocol if source=='pinned' else default_protocol)(seed,workloads,purpose)
        except ValueError: raise HTTPException(400,'Invalid workload count or seed') from None
        eid=str(uuid.uuid4())
        with db.tx() as s:
            e=Experiment(id=eid,name=name.strip(),mode=cfg.mode,status='researching' if source=='research' else 'draft',protocol=p.model_dump(mode='json'),protocol_hash=digest(p),remote={},summary={})
            s.add(e);s.flush()
            db.audit(s,eid,'draft_created',purpose=purpose)
            if source=='research':
                try: db.enqueue(s,eid,'research',cfg.max_jobs)
                except ValueError as exc: raise HTTPException(429,str(exc)) from None
        return RedirectResponse('/experiments/'+eid,status_code=303)

    @app.get('/experiments/{eid}',response_class=HTMLResponse)
    def detail(eid:str,request:Request):
        with db.tx() as s:
            e=get_visible(s,eid,request)
            audit=list(s.scalars(select(Audit).where(Audit.experiment_id==eid).order_by(Audit.id.desc()).limit(30))) if csrf(request) else []
            observed=s.scalar(select(func.count()).select_from(Workload).where(Workload.experiment_id==eid,Workload.phase=='observed'))
            return render(request,'detail.html',e=e,observed=observed,audit=audit,decision_hash=digest({'summary':e.summary,'protocol':e.protocol_hash}),
                          expected_flag=e.remote.get('flag_name', cfg.flagsmith_flag))

    @app.post('/experiments/{eid}/action',dependencies=[Depends(operator)])
    def action(eid:str,request:Request,csrf_token:str=Form(),action:str=Form(),approval_hash:str=Form(default='')):
        """Server-side state machine; forms are not authority. Locks serialize transitions."""
        authorize(request,csrf_token)
        with db.tx() as s:
            # Serialize all state-changing requests on the same admission row first.
            s.execute(select(Budget).where(Budget.key=='admission').with_for_update()).scalar_one()
            e=s.scalars(select(Experiment).where(Experiment.id==eid).with_for_update()).first()
            if not e: raise HTTPException(404,'Experiment not found')
            if e.mode!=cfg.mode: raise HTTPException(409,'Deployment mode differs from stored experiment')
            if action=='publish':
                if e.status not in {'replay_complete','analyzed','retained','promoted'}: raise HTTPException(409,'Only reviewed results can be published')
                e.remote={**e.remote,'published':True};db.audit(s,eid,'published');return RedirectResponse('/experiments/'+eid,status_code=303)
            if action=='unpublish':
                e.remote={**e.remote,'published':False};db.audit(s,eid,'unpublished');return RedirectResponse('/experiments/'+eid,status_code=303)
            allowed={'draft':{'run'},'awaiting_native':{'reconcile','refresh'},'analyzed':{'reconcile','refresh','retain','promote'}}
            if action not in allowed.get(e.status,set()): raise HTTPException(409,'Action not allowed in current state')
            if action=='run':
                p=Protocol.model_validate(e.protocol)
                if approval_hash!=e.protocol_hash or digest(p)!=e.protocol_hash: raise HTTPException(409,'Protocol changed; reload and approve again')
                if e.mode=='live':
                    if p.purpose=='main' and (not p.evidence or not any(sc.evidence_sha256 for sc in p.scenarios)):
                        raise HTTPException(409,'Live main run needs a verified source-backed proposal')
                    # One live experiment per configured flag at a time, even across replicas.
                    active=s.scalar(select(func.count()).select_from(Experiment).where(Experiment.id!=eid,Experiment.mode=='live',Experiment.status.in_(['queued','running','awaiting_native','analyzed','reconciling','releasing','interrupted'])))
                    if active: raise HTTPException(409,'Resolve previous live experiment before starting another')
                if e.mode=='live':
                    from .worker import Worker
                    target=Worker(cfg,db).binding()
                    env_target=digest({'api':cfg.flagsmith_api_url,'environment':cfg.flagsmith_environment_key.get_secret_value()})
                    if p.purpose=='main':
                        prior=s.scalars(select(Experiment).where(Experiment.mode=='live',Experiment.id!=eid))
                        if any(x.protocol.get('purpose')=='pilot' and x.remote.get('environment_binding')==env_target for x in prior):
                            raise HTTPException(409,'Use a separate Flagsmith environment for the main run')
                    e.remote={**e.remote,'binding':target,'environment_binding':env_target}
                e.status='queued'
            elif action in {'reconcile','refresh'}:
                if e.mode!='live': raise HTTPException(409,'No native results for local replay')
                e.status='reconciling'
            elif action in {'retain','promote'}:
                if e.mode!='live' or approval_hash!=digest({'summary':e.summary,'protocol':e.protocol_hash}): raise HTTPException(409,'Approval stale or invalid')
                p=Protocol.model_validate(e.protocol)
                if action=='promote' and not release_decision(e.summary,p,e.summary['native'],e.summary['validation']['passed'])['eligible']:
                    raise HTTPException(409,'Candidate has not passed release gates')
                e.status='releasing'
            try: jid=db.enqueue(s,eid,action,cfg.max_jobs)
            except ValueError as exc: raise HTTPException(429,str(exc)) from None
            db.audit(s,eid,'operator_approved_'+action,job=jid,approval_hash=approval_hash)
        return RedirectResponse('/experiments/'+eid,status_code=303)

    @app.get('/experiments/{eid}/export',dependencies=[Depends(operator)])
    def export(eid:str,request:Request):
        """Operator-only reproducibility export. No keys or provider request URLs included."""
        with db.tx() as s:
            e=get_visible(s,eid,request)
            rows=list(s.scalars(select(Workload).where(Workload.experiment_id==eid).order_by(Workload.ordinal)))
            data={'id':eid,'mode':e.mode,'protocol':e.protocol,'protocol_hash':e.protocol_hash,'summary':e.summary,
                  'workloads':[{'id':w.id,'assignment':w.assignment,'profile':w.profile,'observation':w.observation} for w in rows]}
            return Response(json.dumps(data,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="firstproof-{eid}.json"'})

    return app


app=create_app()
