"""Durable workflow worker: research, provision, execute, reconcile and release.

Only a trusted operator can enqueue a mutation. Remote operations are journaled
before dispatch. Expired leases are interrupted, never silently retried after a
possibly successful external mutation. Keep the API and worker separate.
"""
import asyncio
import hashlib
import logging
import random
import time
import uuid
from datetime import datetime, timezone
import httpx
from sqlalchemy import select, update
from .config import settings
from .contracts import Protocol, Family, digest
from .db import Database, Experiment, Workload, Job, Outbox
from .integrations import FlagsmithAdmin, NativeRuntime, LocalReplay, IntegrationError, research, propose, reconcile_native, request_json
from .policies import execute, CONTROL, CANDIDATE
from .verification import verify, summarize, release_decision

log = logging.getLogger('firstproof.worker')


def manifest(protocol: Protocol, eid: str, *, holdout=False):
    """Independent seeds and time-based faults; treatment never enters generation."""
    rng = random.Random(protocol.seed + (0x5F3759DF if holdout else 0))
    families = [s.family for s in protocol.scenarios]
    weights = [s.weight for s in protocol.scenarios]
    n = min(80, max(20, protocol.workloads//10)) if holdout else protocol.workloads
    for i in range(n):
        f = rng.choices(families, weights)[0]
        # First few holdout cases deterministically cover the declared guard families.
        if holdout and i < len(families): f = families[i]
        key = f'{eid}:{"h" if holdout else "m"}:{i}'
        profile = {'family':f.value,'outage_ms':rng.randint(60,900),'delay_ms':rng.randint(5,35),
                   'retry_after':1,'expected':hashlib.sha256((key+':fixture').encode()).hexdigest()[:24]}
        yield key, i, profile, rng.randrange(2**63)


class Worker:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.owner = str(uuid.uuid4())
        self.job = None

    def binding(self):
        """Pin the exact native target without storing credentials in the experiment."""
        c=self.cfg
        return digest({'api':c.flagsmith_api_url,'environment':c.flagsmith_environment_key.get_secret_value(),
                       'feature':c.flagsmith_feature_id,'candidate':c.flagsmith_candidate_id,'flag':c.flagsmith_flag})

    def assert_binding(self, remote):
        if remote.get('binding') != self.binding():
            raise IntegrationError('Native target differs from the frozen target')

    def mutate(self, eid, action, **changes):
        """Fence all state changes and write a durable audit event in the same transaction."""
        with self.db.tx() as s:
            self.db.fence(s, self.job, self.owner)
            e = s.get(Experiment,eid)
            for k,v in changes.items(): setattr(e,k,v)
            self.db.audit(s,eid,action,status=e.status)

    def load(self,eid):
        with self.db.tx() as s: return s.get(Experiment,eid)

    async def remote_journal(self,eid,remote,action,operation):
        """If a crash follows dispatch, the pending journal remains inspectable."""
        remote = {**remote, 'pending_action':action}
        self.mutate(eid,action+'_pending',remote=remote)
        result = await operation()
        remote = {**remote, 'pending_action':None}
        return remote,result

    async def research_job(self,e):
        cfg = self.cfg
        policy = CONTROL
        if cfg.mode == 'live' and cfg.flagsmith_release_environment_key.get_secret_value():
            runtime = NativeRuntime(cfg,release=True)
            try: policy = await runtime.release_policy('firstproof-research')
            finally: await runtime.close()
        # Explicitly selected live research: no search fixture is substituted.
        async with httpx.AsyncClient(timeout=12,trust_env=False,limits=httpx.Limits(max_connections=8)) as http:
            found = await research(cfg,self.db,http,e.name,policy)
            proposal = await propose(cfg,http,e.name,found)
        p = Protocol.model_validate({**e.protocol,**proposal.model_dump(mode='json'),'evidence':[x.model_dump(mode='json') for x in found]})
        self.mutate(e.id,'research_completed',protocol=p.model_dump(mode='json'),protocol_hash=digest(p),status='draft',error='')

    async def fault_operation(self,http,identity,profile,seed,policy,p):
        """Execute the identical client against a private HTTP transport; observe server trace."""
        root = self.cfg.faultlab_url.rstrip('/')
        hdr = {'X-Faultlab-Token':self.cfg.faultlab_token.get_secret_value()}
        await request_json(http,'PUT',f'{root}/profiles/{identity}',headers=hdr,payload=profile)
        try:
            outcome = await execute(http,f'{root}/search/{identity}',policy,p.deadline_ms,p.max_attempts,seed,headers=hdr)
            trace = await request_json(http,'GET',f'{root}/observations/{identity}',headers=hdr)
            return verify(outcome,trace['requests'],profile['expected'],p)
        finally:
            # Cleanup does not change the outcome; the server also expires old profiles.
            try: await request_json(http,'DELETE',f'{root}/profiles/{identity}',headers=hdr)
            except IntegrationError: pass

    async def run(self,e):
        p = Protocol.model_validate(e.protocol)
        if digest(p) != e.protocol_hash: raise RuntimeError('Approved protocol hash mismatch')
        if p.workloads > self.cfg.max_workloads: raise RuntimeError('Workload budget exceeded')
        live = e.mode == 'live'
        runtime = NativeRuntime(self.cfg) if live else LocalReplay()
        remote = dict(e.remote)
        event = 'operation_verified_'+e.id.replace('-','')
        try:
            async with httpx.AsyncClient(timeout=12,trust_env=False,limits=httpx.Limits(max_connections=self.cfg.workload_concurrency+4,max_keepalive_connections=self.cfg.workload_concurrency+2)) as http:
                if live:
                    if remote.get('binding') != self.binding(): raise IntegrationError('Approved native target changed')
                    admin = FlagsmithAdmin(self.cfg,http)
                    warehouses = await admin.warehouses()
                    if not isinstance(warehouses,list) or not any(w.get('status')=='connected' for w in warehouses):
                        raise IntegrationError('No connected warehouse in dedicated experiment environment')
                    remote,metric = await self.remote_journal(e.id,remote,'create_metric',lambda:admin.create_metric(event))
                    remote.update(metric_id=int(metric['id']),event=event)
                    self.mutate(e.id,'metric_created',remote=remote)
                    remote,native = await self.remote_journal(e.id,remote,'create_experiment',lambda:admin.create_experiment('FirstProof '+e.id,p.hypothesis,remote['metric_id']))
                    remote.update(experiment_id=int(native['id']),dashboard=admin.dashboard(native['id']))
                    self.mutate(e.id,'native_created',remote=remote)
                    remote,current = await self.remote_journal(e.id,remote,'start_experiment',lambda:admin.start(remote['experiment_id']))
                    remote['rollout_snapshot'] = current.get('experiment_rollout')
                    if not remote['rollout_snapshot']: raise IntegrationError('Native rollout readback unavailable')
                    self.mutate(e.id,'native_started',remote=remote)
                rows = list(manifest(p,e.id))
                # Entire manifest and seeds are recorded before assignment; holdout remains
                # separate and never emits native events or contributes to the main metric.
                holdout = list(manifest(p,e.id,holdout=True)) if p.purpose=='main' and live else []
                remote['manifest_hash'] = digest({'rows':rows,'holdout':holdout})
                self.mutate(e.id,'manifest_frozen',remote=remote,status='running')
                with self.db.tx() as s:
                    self.db.fence(s,self.job,self.owner)
                    s.add_all(Workload(id=i,experiment_id=e.id,ordinal=n,family=prof['family'],profile={**prof,'seed':seed}) for i,n,prof,seed in rows)
                semaphore = asyncio.Semaphore(self.cfg.workload_concurrency)
                async def one(item):
                    identity, ordinal, profile, seed = item
                    async with semaphore:
                        variant = await runtime.evaluate(identity,remote.get('experiment_id'))
                        # Persist assignment before exposure; if worker dies, the run is invalid.
                        with self.db.tx() as s:
                            self.db.fence(s,self.job,self.owner)
                            w = s.get(Workload,identity); w.assignment=variant; w.phase='exposing'
                        await runtime.expose(identity,variant,remote.get('experiment_id'))
                        obs = await self.fault_operation(http,identity,profile,seed,variant,p)
                        with self.db.tx() as s:
                            self.db.fence(s,self.job,self.owner)
                            w = s.get(Workload,identity); w.observation=obs; w.phase='observed'
                            if live and obs['success']: s.add(Outbox(id=identity,experiment_id=e.id))
                        return {'assignment':variant,'family':profile['family'],**obs}
                # Schedule bounded chunks rather than thousands of simultaneous tasks.
                observations=[]
                for i in range(0,len(rows),self.cfg.workload_concurrency):
                    async with asyncio.TaskGroup() as tg:
                        tasks=[tg.create_task(one(row)) for row in rows[i:i+self.cfg.workload_concurrency]]
                    observations.extend(t.result() for t in tasks)
                    if live:
                        with self.db.tx() as s:
                            ids = list(s.scalars(select(Outbox.id).where(Outbox.experiment_id==e.id,Outbox.state=='pending').limit(self.cfg.workload_concurrency)))
                        for identity in ids:
                            await runtime.conversion(identity,event)
                            with self.db.tx() as s:
                                self.db.fence(s,self.job,self.owner)
                                o=s.get(Outbox,identity);o.state='sdk_submitted';o.attempts+=1
                await runtime.flush()
                summary=summarize(observations,p)
                # Persist main observations before reserved validation. This validation
                # uses both implementations on matched held-out profiles, not Flagsmith
                # exposure data; it checks guardrails and non-regression, not significance.
                validation={'passed':False,'status':'not_run'}
                if holdout:
                    checks=[]
                    for identity,ordinal,profile,seed in holdout:
                        # Alternate paired order to reduce systematic warm-up/order bias.
                        order=(CONTROL,CANDIDATE) if ordinal%2==0 else (CANDIDATE,CONTROL)
                        for variant in order:
                            row=await self.fault_operation(http,identity+':'+variant,profile,seed,variant,p)
                            checks.append({'assignment':variant,'family':profile['family'],**row})
                    vp=p.model_copy(update={'workloads':len(checks)})
                    vs=summarize(checks,vp); b=vs['groups'][CANDIDATE]
                    passed=vs['complete'] and not any(b['violations'].values()) and vs['lift_pp']>=0 and vs['amplification_pct']<=p.max_amplification_pct
                    validation={'passed':bool(passed),'status':'reserved_paired_check','summary':vs,'observations':checks}
                summary['validation']=validation
                summary['native']={'reconciled':False,'status':'awaiting_flagsmith' if live else 'local_replay_not_native'}
                summary['decision']=release_decision(summary,p,summary['native'],validation['passed'])
                if live:
                    current=await admin.inspect(remote['experiment_id'])
                    if current.get('status')!='running' or current.get('experiment_rollout')!=remote['rollout_snapshot']:
                        raise IntegrationError('Native rollout changed during the experiment')
                self.mutate(e.id,'execution_completed',summary=summary,remote=remote,status='awaiting_native' if live else 'replay_complete')
        finally:
            await runtime.close()

    async def reconcile(self,e,refresh=False):
        """Native results are read from the API; pending or mismatching counts block release."""
        if e.mode!='live': raise IntegrationError('Local replay cannot retrieve native results')
        remote=dict(e.remote); p=Protocol.model_validate(e.protocol)
        self.assert_binding(remote)
        async with httpx.AsyncClient(timeout=15,trust_env=False) as http:
            admin=FlagsmithAdmin(self.cfg,http); rid=remote['experiment_id']
            current=await admin.inspect(rid)
            if current.get('status')!='running' or current.get('experiment_rollout')!=remote['rollout_snapshot']:
                raise IntegrationError('Native configuration drift detected')
            if refresh:
                await admin.call('POST',f'experiments/{rid}/results/refresh/')
                await admin.call('POST',f'experiments/{rid}/exposures/refresh/')
            raw=await admin.results(rid); exp=await admin.call('GET',f'experiments/{rid}/exposures/')
        summary=dict(e.summary)
        native=reconcile_native(raw,summary,remote['metric_id'],exp)
        summary['native']=native
        summary['native_raw']=raw
        summary['native_exposures_raw']=exp
        summary['decision']=release_decision(summary,p,native,summary['validation']['passed'])
        self.mutate(e.id,'native_results_read',summary=summary,status='analyzed' if native['reconciled'] else 'awaiting_native')
        if native['reconciled']:
            with self.db.tx() as s:
                self.db.fence(s,self.job,self.owner)
                s.execute(update(Outbox).where(Outbox.experiment_id==e.id,Outbox.state=='sdk_submitted').values(state='aggregate_reconciled'))

    async def finish(self,e,variant):
        """Complete a native experiment using official API; candidate is gate-bound.

        Promotion is confined to the dedicated experiment sandbox. A separate normal
        research release environment is never mutated automatically by this application.
        """
        p=Protocol.model_validate(e.protocol); remote=dict(e.remote)
        self.assert_binding(remote)
        if variant==CANDIDATE and not release_decision(e.summary,p,e.summary['native'],e.summary['validation']['passed'])['eligible']:
            raise IntegrationError('Candidate not eligible for promotion')
        async with httpx.AsyncClient(timeout=15,trust_env=False) as http:
            admin=FlagsmithAdmin(self.cfg,http); rid=remote['experiment_id']; current=await admin.inspect(rid)
            if current.get('status')!='running' or current.get('experiment_rollout')!=remote['rollout_snapshot']:
                raise IntegrationError('Native configuration drift detected')
            # Refresh read-only evidence before mutating. If native results changed,
            # force a new approval against the exact updated summary.
            raw=await admin.results(rid); exp=await admin.call('GET',f'experiments/{rid}/exposures/')
            if raw!=e.summary['native_raw'] or exp!=e.summary['native_exposures_raw']:
                raise IntegrationError('Native evidence changed; reconcile and approve again')
            body={'rollout_decision':'variant','rollout_variant':self.cfg.flagsmith_candidate_id} if variant==CANDIDATE else {'rollout_decision':'control'}
            remote,result=await self.remote_journal(e.id,remote,'complete_experiment',lambda:admin.call('POST',f'experiments/{rid}/complete/',body))
            readback=await admin.inspect(rid)
            if readback.get('status')!='completed' or readback.get('rollout_decision')!=body['rollout_decision']:
                raise IntegrationError('Native completion readback mismatch')
            remote['release_readback']=readback
            # Verify selected runtime value without using an exposure-producing call.
            runtime=NativeRuntime(self.cfg)
            try:
                flags=await runtime._call(runtime.sdk.get_identity_flags,'fp-release-check-'+e.id)
                f=flags.get_flag(self.cfg.flagsmith_flag)
                if not f.enabled or f.value!=variant: raise IntegrationError('Selected behavior not confirmed by SDK')
                remote['runtime_readback']=f.value
                # A controlled smoke request shows the selected runtime behavior, not a new A/B conversion.
                _,_,profile,seed=next(manifest(p,e.id,holdout=True))
                profile={**profile,'family':'healthy'}
                smoke=await self.fault_operation(http,e.id+':release-smoke',profile,seed,f.value,p)
                if not smoke['success']: raise IntegrationError('Native selection confirmed but release smoke test failed')
                remote['release_smoke']=smoke
            finally: await runtime.close()
        self.mutate(e.id,'release_completed',remote=remote,status='promoted' if variant==CANDIDATE else 'retained')

    async def process(self,job):
        jid,eid,kind=job; self.job=jid
        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                self.db.heartbeat(jid,self.owner)
        pulse=asyncio.create_task(heartbeat())
        try:
            e=self.load(eid)
            if kind=='research': await self.research_job(e)
            elif kind=='run': await self.run(e)
            elif kind in {'reconcile','refresh'}: await self.reconcile(e,kind=='refresh')
            elif kind in {'retain','promote'}: await self.finish(e,CONTROL if kind=='retain' else CANDIDATE)
            else: raise RuntimeError('Unknown job type')
            with self.db.tx() as s: self.db.fence(s,jid,self.owner).state='done'
        except Exception as exc:
            # Never include arbitrary provider exceptions (URLs may contain keys).
            message=str(exc)[:250] if isinstance(exc,IntegrationError) else type(exc).__name__
            log.warning('job_failed kind=%s id=%s type=%s',kind,jid,type(exc).__name__)
            try:
                with self.db.tx() as s:
                    j=self.db.fence(s,jid,self.owner);j.state='failed';j.error=message
                    e=s.get(Experiment,eid);e.error=message
                    # Read-only refresh failures can be retried. Mutations remain fail-closed.
                    e.status='awaiting_native' if kind in {'reconcile','refresh'} else ('draft' if kind=='research' else 'interrupted')
                    self.db.audit(s,eid,'job_failed',kind=kind,error=message)
            except Exception: log.warning('lease_lost id=%s',jid)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse,return_exceptions=True)
            self.job=None

    async def forever(self):
        """Simple service loop, supervised by the process manager."""
        while True:
            try:
                self.db.recover()
                job=self.db.claim(self.owner)
                if job: await self.process(job)
                else: await asyncio.sleep(.5)
            except Exception as exc:
                log.warning('worker_loop_error type=%s',type(exc).__name__)
                await asyncio.sleep(2)


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    # HTTP request logs can contain query credentials (SerpAPI). Do not emit them.
    for logger_name in ('httpx','httpcore','urllib3','flagsmith'):
        logging.getLogger(logger_name).disabled=True
    cfg=settings(); db=Database(cfg.database_url)
    if cfg.mode=='local': db.initialize()
    asyncio.run(Worker(cfg,db).forever())


if __name__=='__main__': main()
