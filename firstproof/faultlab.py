"""Private, bounded HTTP upstream emulator with an independent request-observation endpoint.

The server responds according to elapsed monotonic time, not the assigned treatment.
It deliberately does not know the Flagsmith variant. Only the trusted runner registers
profiles. Never expose this service publicly or allow arbitrary URLs in profiles.
"""
import asyncio
import hmac
import time
import os
from dataclasses import dataclass, field
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    """Finite allowlisted fault parameters generated independently of assignment."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    family: Literal['healthy','transient','rate_limit','excessive_retry_after','persistent','slow_response','corrupt_response']
    outage_ms: int = Field(ge=0, le=5000)
    delay_ms: int = Field(ge=0, le=2000)
    retry_after: int = Field(ge=0, le=3600)
    expected: str = Field(pattern=r'^[0-9a-f]{24}$')


@dataclass
class Entry:
    profile: Profile
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    requests: list[dict] = field(default_factory=list)


def create_app(token: str | None = None) -> FastAPI:
    """Construct one single-process FaultLab. All state is run-scoped and bounded."""
    app = FastAPI(title='FirstProof FaultLab', docs_url=None, redoc_url=None, openapi_url=None)
    records: dict[str, Entry] = {}
    secret = token or os.getenv('FAULTLAB_TOKEN', 'local-faultlab-only')
    if not secret:
        raise ValueError('FaultLab requires a non-empty token')

    def auth(x_faultlab_token: str = Header(default='')):
        if not hmac.compare_digest(x_faultlab_token.encode(), secret.encode()):
            raise HTTPException(403, 'FaultLab access denied')

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.put('/profiles/{identity}', dependencies=[Depends(auth)])
    async def register(identity: str, profile: Profile):
        """Idempotent before execution; never reset a clock after exposure."""
        if len(identity) > 100:
            raise HTTPException(400, 'Invalid identity')
        now = time.monotonic()
        for key in [k for k, v in records.items() if now-v.created > 600]:
            records.pop(key, None)
        if identity in records:
            if records[identity].profile != profile or records[identity].started is not None:
                raise HTTPException(409, 'Profile already used')
        elif len(records) >= 20000:
            raise HTTPException(503, 'FaultLab capacity exceeded')
        else:
            records[identity] = Entry(profile)
        return {'registered': True}

    @app.get('/search/{identity}', dependencies=[Depends(auth)])
    async def search(identity: str):
        """Real HTTP response with controlled delay and status; no provider traffic."""
        item = records.get(identity)
        if item is None:
            raise HTTPException(404, 'Unknown workload')
        if len(item.requests) >= 10:
            raise HTTPException(429, 'Request recording capacity exceeded')
        now = time.monotonic()
        if item.started is None:
            item.started = now
        elapsed = (now-item.started)*1000
        p = item.profile
        status, hint, delay = 200, None, p.delay_ms
        if p.family == 'persistent':
            status = 503
        elif p.family == 'transient' and elapsed < p.outage_ms:
            status = 503
        elif p.family == 'rate_limit' and elapsed < p.outage_ms:
            status, hint = 429, p.retry_after
        elif p.family == 'excessive_retry_after':
            status, hint = 429, 3600
        elif p.family == 'slow_response' and elapsed < p.outage_ms:
            delay = max(p.delay_ms, 750)
        row = {'ordinal': len(item.requests)+1, 'arrival_ms': elapsed, 'status': status,
               'retry_after': hint, 'response_delay_ms': delay}
        item.requests.append(row)
        await asyncio.sleep(delay/1000)
        body = {'search_metadata': {'status': 'Success'},
                'organic_results': [{'title': 'Controlled fixture', 'link': 'https://example.org/fixture', 'snippet': p.expected}]}
        if p.family == 'corrupt_response':
            body = {'search_metadata': {'status': 'Error'}, 'organic_results': 'invalid'}
        if status != 200:
            body = {'error': 'Controlled upstream failure'}
        return JSONResponse(body, status_code=status, headers={'Retry-After': str(hint)} if hint is not None else {})

    @app.get('/observations/{identity}', dependencies=[Depends(auth)])
    async def observe(identity: str):
        """Verifier retrieves the server-side trace, independent of client claims."""
        item = records.get(identity)
        if item is None:
            raise HTTPException(404, 'Unknown workload')
        return {'requests': item.requests}

    @app.delete('/profiles/{identity}', dependencies=[Depends(auth)])
    async def cleanup(identity: str):
        records.pop(identity, None)
        return {'deleted': True}

    return app


app = create_app()
