"""Typed evidence, workload and outcome contracts. No executable LLM-generated code."""
import hashlib
import json
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Reject unknown fields instead of accepting accidental or malicious extensions."""
    model_config = ConfigDict(extra='forbid', frozen=True)


class Family(StrEnum):
    HEALTHY = 'healthy'
    TRANSIENT = 'transient'
    RATE_LIMIT = 'rate_limit'
    EXCESSIVE_HINT = 'excessive_retry_after'
    PERSISTENT = 'persistent'
    SLOW = 'slow_response'
    CORRUPT = 'corrupt_response'


class Evidence(StrictModel):
    source: str = Field(max_length=500)
    title: str = Field(max_length=250)
    excerpt: str = Field(max_length=2000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    stance: Literal['support', 'counter', 'context']


class Scenario(StrictModel):
    family: Family
    weight: int = Field(ge=1, le=100)
    rationale: str = Field(min_length=1, max_length=500)
    evidence_sha256: list[str] = Field(default_factory=list, max_length=10)


class Proposal(StrictModel):
    hypothesis: str = Field(min_length=10, max_length=1000)
    scenarios: list[Scenario] = Field(min_length=2, max_length=7)
    assumptions: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode='after')
    def unique_families(self):
        if len({s.family for s in self.scenarios}) != len(self.scenarios):
            raise ValueError('Scenario families must be unique')
        return self


class Protocol(StrictModel):
    """Immutable specification approved before treatment assignment."""
    version: Literal[1] = 1
    purpose: Literal['pilot', 'main', 'validation'] = 'pilot'
    hypothesis: str = Field(min_length=10, max_length=1000)
    seed: int = Field(ge=0, le=2**63-1)
    workloads: int = Field(ge=2, le=10000)
    deadline_ms: int = Field(default=2000, ge=250, le=5000)
    max_attempts: int = Field(default=4, ge=1, le=5)
    min_effect_pp: float = Field(default=5, gt=0, le=100)
    max_amplification_pct: float = Field(default=10, ge=0, le=100)
    scenarios: list[Scenario] = Field(min_length=2, max_length=7)
    assumptions: list[str] = Field(min_length=1, max_length=10)
    evidence: list[Evidence] = Field(default_factory=list, max_length=30)
    policy_version: Literal['retry-v1'] = 'retry-v1'

    @model_validator(mode='after')
    def integrity(self):
        if len({s.family for s in self.scenarios}) != len(self.scenarios):
            raise ValueError('Duplicate scenario family')
        known = {e.sha256 for e in self.evidence}
        if any(h not in known for s in self.scenarios for h in s.evidence_sha256):
            raise ValueError('Scenario refers to evidence outside this protocol')
        required = {Family.HEALTHY, Family.EXCESSIVE_HINT, Family.CORRUPT}
        if self.purpose != 'pilot' and (self.workloads < 100 or not required.issubset({s.family for s in self.scenarios})):
            raise ValueError('Main/validation requires >=100 workloads and healthy, excessive hint, corrupt guard cases')
        return self


def digest(value: BaseModel | dict) -> str:
    """Canonical hash binds approvals to exact protocol and operation data."""
    data = value.model_dump(mode='json') if isinstance(value, BaseModel) else value
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def default_protocol(seed: int = 1729, workloads: int = 30, purpose: str = 'pilot') -> Protocol:
    """Local synthetic design. Weights are assumptions, not measured production frequencies."""
    return Protocol(seed=seed, workloads=workloads, purpose=purpose,
        hypothesis='Remaining-budget-aware retry scheduling improves verified completion without unacceptable request amplification.',
        scenarios=[Scenario(family=f, weight=w, rationale='Explicit synthetic failure model; not a production prevalence estimate.')
                   for f, w in [(Family.HEALTHY, 20), (Family.TRANSIENT, 25), (Family.RATE_LIMIT, 15),
                                (Family.EXCESSIVE_HINT, 5), (Family.PERSISTENT, 10), (Family.SLOW, 20), (Family.CORRUPT, 5)]],
        assumptions=['Independent workloads; read-only search requests; scenario weights are design assumptions.',
                     'Controlled transport is not a representative sample of actual SerpAPI service incidents.'])
