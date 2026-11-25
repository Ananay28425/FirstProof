"""Persistent state, audit records and transactional leased jobs.

PostgreSQL is the live backend. SQLite is a single-worker local replay convenience.
The same transaction changes experiment state and enqueues its associated work.
"""
import time
import uuid
from contextlib import contextmanager
from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, create_engine, select, update, func, or_, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Experiment(Base):
    """A single immutable protocol with independently tracked remote resources."""
    __tablename__ = 'experiments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    protocol: Mapped[dict] = mapped_column(JSON)
    protocol_hash: Mapped[str] = mapped_column(String(64))
    remote: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Workload(Base):
    """Unique identity and durable raw observation; a retry never overwrites an observed result."""
    __tablename__ = 'workloads'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey('experiments.id'), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    family: Mapped[str] = mapped_column(String(40))
    profile: Mapped[dict] = mapped_column(JSON)
    assignment: Mapped[str | None] = mapped_column(String(30), nullable=True)
    phase: Mapped[str] = mapped_column(String(20), default='pending')
    observation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint('experiment_id', 'ordinal'),)


class Outbox(Base):
    """Logical conversion intent. SDK submission is not remote confirmation."""
    __tablename__ = 'outbox'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey('experiments.id'), index=True)
    state: Mapped[str] = mapped_column(String(24), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    """Leases fence worker writes; ambiguous experiments fail closed rather than silently replay."""
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey('experiments.id'), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(16), default='queued', index=True)
    owner: Mapped[str] = mapped_column(String(36), default='')
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    error: Mapped[str] = mapped_column(Text, default='')


class Audit(Base):
    """Append-only at the application layer; not a cryptographically immutable database."""
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[dict] = mapped_column(JSON)
    at: Mapped[float] = mapped_column(Float, default=time.time)


class Budget(Base):
    """Database-wide admission and provider budget rows, not process-local counters."""
    __tablename__ = 'budgets'
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True, **({'connect_args': {'check_same_thread': False, 'timeout': 15}} if url.startswith('sqlite') else {'pool_size': 5, 'max_overflow': 2, 'pool_timeout': 5}))
        if url.startswith('sqlite'):
            @event.listens_for(self.engine, 'connect')
            def configure(conn, _):
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('PRAGMA journal_mode=WAL')
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        """Local bootstrap. Live deployment runs Alembic before starting replicas."""
        Base.metadata.create_all(self.engine)
        with self.tx() as s:
            for key in ('admission', 'search'):
                if s.get(Budget, key) is None:
                    s.add(Budget(key=key, count=0))

    @contextmanager
    def tx(self):
        """Short transaction; never hold one open during network calls."""
        with self.sessions.begin() as s:
            yield s

    def audit(self, s, eid, action, **detail):
        s.add(Audit(experiment_id=eid, action=action, detail=detail))

    def enqueue(self, s, eid, kind, cap):
        """Serialize admission in PostgreSQL before counting outstanding jobs."""
        s.execute(select(Budget).where(Budget.key == 'admission').with_for_update()).scalar_one()
        count = s.scalar(select(func.count()).select_from(Job).where(Job.state.in_(['queued', 'running'])))
        if count >= cap:
            raise ValueError('Job queue full; retry later')
        job = Job(id=str(uuid.uuid4()), experiment_id=eid, kind=kind, state='queued')
        s.add(job)
        return job.id

    def claim(self, owner: str, lease: float = 120):
        """SKIP LOCKED makes distinct PostgreSQL workers claim distinct jobs."""
        with self.tx() as s:
            job = s.scalars(select(Job).where(Job.state == 'queued').order_by(Job.created_at).limit(1).with_for_update(skip_locked=True)).first()
            if job is None:
                return None
            # Conditional update also prevents duplicate claim in the local SQLite profile.
            result = s.execute(update(Job).where(Job.id == job.id, Job.state == 'queued').values(state='running', owner=owner, lease_until=time.time()+lease))
            if result.rowcount != 1:
                return None
            return job.id, job.experiment_id, job.kind

    def fence(self, s, jid, owner):
        """Require a still-live lease before changing state or committing evidence."""
        job = s.scalars(select(Job).where(Job.id == jid).with_for_update()).one()
        if job.owner != owner or job.state != 'running' or job.lease_until <= time.time():
            raise RuntimeError('Worker lease lost')
        return job

    def heartbeat(self, jid, owner):
        with self.tx() as s:
            j = self.fence(s, jid, owner)
            j.lease_until = time.time()+120

    def recover(self):
        """Preserve accepted job records and mark uncertain external effects for review.
        No automatic retry of a remote mutation or a partly exposed workload.
        """
        with self.tx() as s:
            for j in s.scalars(select(Job).where(Job.state == 'running', Job.lease_until < time.time()).with_for_update(skip_locked=True)):
                j.state, j.error = 'failed', 'Lease expired; inspect external effects before starting a fresh experiment.'
                e = s.get(Experiment, j.experiment_id)
                e.status, e.error = 'interrupted', j.error
                self.audit(s, e.id, 'worker_interrupted', job=j.id)

    def reserve_search(self, limit):
        """Global lifetime cap; increment before dispatch (failed requests count)."""
        with self.tx() as s:
            row = s.scalars(select(Budget).where(Budget.key == 'search').with_for_update()).one()
            if row.count >= limit:
                raise ValueError('Global SerpAPI budget exhausted')
            row.count += 1
