"""Two-workspace HTTP episodes. Cooldown state is private to one episode."""
import asyncio
import hashlib
import random
from time import perf_counter
from typing import TypedDict

import httpx

from .contracts import COOLDOWN_VARIANTS, Family, Protocol
from .integrations import IntegrationError, request_json

A_DEADLINE_MS = 3000
HARD_TIMEOUT_SECONDS = 3.0


class WorkspaceProfile(TypedDict):
    family: str
    outage_ms: int
    delay_ms: int
    retry_after: int
    expected: str


class EpisodeProfile(TypedDict):
    family: str
    arrival_offset_ms: int
    workspace_a: WorkspaceProfile
    workspace_b: WorkspaceProfile


def episode_profile(identity: str, family: Family, rng: random.Random, deadline_ms: int) -> EpisodeProfile:
    hint = rng.choice((1, 2)) if family != Family.HEALTHY else 0
    offsets = {
        Family.HEALTHY: 0,
        Family.COOLDOWN_OVERLAP: rng.randint(30, 350),
        Family.COOLDOWN_BOUNDARY: max(0, hint * 1000 - deadline_ms + rng.randint(-160, 160)),
        Family.HEALTHY_FIRST: 0,
        Family.AFTER_COOLDOWN: hint * 1000 + rng.randint(80, 180),
    }
    return {
        'family': family.value,
        'arrival_offset_ms': offsets[family],
        'workspace_a': {
            'family': 'rate_limit' if hint else 'healthy',
            'outage_ms': rng.randint(300, 700) if hint else 0,
            'delay_ms': rng.randint(10, 35),
            'retry_after': hint,
            'expected': hashlib.sha256(f'{identity}:a'.encode()).hexdigest()[:24],
        },
        'workspace_b': {
            'family': 'healthy',
            'outage_ms': 0,
            'delay_ms': rng.randint(10, 35),
            'retry_after': 0,
            'expected': hashlib.sha256(f'{identity}:b'.encode()).hexdigest()[:24],
        },
    }


async def run_episode(
    http: httpx.AsyncClient, root: str, headers: dict[str, str], identity: str,
    profile: EpisodeProfile, variant: str, protocol: Protocol,
) -> dict:
    """Replay one episode and verify both workspaces against separate upstream traces."""
    if variant not in COOLDOWN_VARIANTS:
        raise IntegrationError('Unknown cooldown assignment')
    profiles = {'A': profile['workspace_a'], 'B': profile['workspace_b']}
    upstream_ids = {workspace: f'{identity}:{workspace}' for workspace in profiles}
    registered = []
    cooldown_until: dict[tuple[str, str], float] = {}
    client_trace: dict[str, list[dict]] = {'A': [], 'B': []}
    enqueued: dict[str, float] = {}
    origin = perf_counter()

    def ms(timestamp: float) -> float:
        return (timestamp - origin) * 1000

    async def read(workspace: str) -> tuple[int, float, bool]:
        # The workload calls one read method. Both policies share the same retry limit.
        key = (workspace if variant != 'control' else '*', 'conversations.info')
        while (wait := cooldown_until.get(key, 0) - perf_counter()) > 0:
            await asyncio.sleep(wait)
        sent = perf_counter()
        try:
            response = await http.get(
                f'{root}/search/{upstream_ids[workspace]}', headers=headers, timeout=2.5,
            )
        except httpx.HTTPError as exc:
            raise IntegrationError(f'FaultLab read failed ({type(exc).__name__})') from None
        received = perf_counter()
        hint = 0
        valid = False
        if response.status_code == 429:
            raw_hint = response.headers.get('Retry-After', '')
            if raw_hint not in {'1', '2'}:
                raise IntegrationError('FaultLab returned an unexpected Retry-After')
            hint = int(raw_hint)
            cooldown_until[key] = max(cooldown_until.get(key, 0), received + hint)
        elif response.status_code == 200:
            try:
                body = response.json()
                items = body['organic_results']
                valid = (body['search_metadata']['status'] == 'Success' and isinstance(items, list)
                         and len(items) == 1 and items[0]['snippet'] == profiles[workspace]['expected'])
            except (ValueError, TypeError, KeyError):
                pass
        else:
            raise IntegrationError(f'FaultLab returned unexpected HTTP {response.status_code}')
        client_trace[workspace].append({
            'sent_ms': ms(sent), 'received_ms': ms(received),
            'status': response.status_code, 'retry_after': hint,
        })
        return response.status_code, received, valid

    async def read_b() -> tuple[int, float, bool]:
        enqueued['B'] = perf_counter()
        return await read('B')

    async def retry_throttled_workspace() -> tuple[int, float, bool]:
        remaining = HARD_TIMEOUT_SECONDS - (perf_counter() - enqueued['A'])
        async with asyncio.timeout(remaining):
            return await read('A')

    try:
        for workspace, upstream_id in upstream_ids.items():
            await request_json(http, 'PUT', f'{root}/profiles/{upstream_id}',
                               headers=headers, payload=profiles[workspace])
            registered.append(upstream_id)
        origin = perf_counter()
        if profile['family'] == Family.HEALTHY_FIRST:
            async with asyncio.timeout(HARD_TIMEOUT_SECONDS):
                b = await read_b()
        enqueued['A'] = perf_counter()
        async with asyncio.timeout(HARD_TIMEOUT_SECONDS):
            a = await read('A')
            if (a[0] == 429) != bool(profiles['A']['retry_after']):
                raise IntegrationError('FaultLab did not deliver the declared first response')
        retry_a = (
            asyncio.create_task(retry_throttled_workspace())
            if a[0] == 429 and protocol.max_attempts > 1 else None
        )
        try:
            if profile['family'] != Family.HEALTHY_FIRST:
                await asyncio.sleep(profile['arrival_offset_ms'] / 1000)
                async with asyncio.timeout(HARD_TIMEOUT_SECONDS):
                    b = await read_b()
            if retry_a is not None:
                a = await retry_a
        finally:
            if retry_a is not None and not retry_a.done():
                retry_a.cancel()
                await asyncio.gather(retry_a, return_exceptions=True)
            elif retry_a is not None and not retry_a.cancelled():
                retry_a.exception()
        server_trace = {}
        for workspace, upstream_id in upstream_ids.items():
            observed = await request_json(http, 'GET', f'{root}/observations/{upstream_id}', headers=headers)
            server_trace[workspace] = observed['requests']
    except TimeoutError:
        raise IntegrationError('FaultLab episode exceeded its transport timeout') from None
    finally:
        for upstream_id in registered:
            try:
                await request_json(http, 'DELETE', f'{root}/profiles/{upstream_id}', headers=headers)
            except IntegrationError:
                # FaultLab expires abandoned profiles.
                pass

    trace_complete = all(
        [(row['status'], row['retry_after'] or 0) for row in server_trace[workspace]]
        == [(row['status'], row['retry_after']) for row in client_trace[workspace]]
        for workspace in profiles
    )
    a_elapsed = (a[1] - enqueued['A']) * 1000
    b_elapsed = (b[1] - enqueued['B']) * 1000
    a_attempts = client_trace['A']
    hint_violation = False
    if len(a_attempts) > 1 and trace_complete:
        hint_ms = a_attempts[0]['retry_after'] * 1000
        client_wait = a_attempts[1]['sent_ms'] - a_attempts[0]['received_ms']
        a_server = server_trace['A']
        upstream_wait = a_server[1]['arrival_ms'] - a_server[0]['arrival_ms']
        hint_violation = min(client_wait, upstream_wait) + 0.01 < hint_ms
    invalid = (a[0] == 200 and not a[2]) or (b[0] == 200 and not b[2])
    attempt_violation = len(server_trace['A']) > protocol.max_attempts or len(server_trace['B']) != 1
    deadline_violation = max(a_elapsed, b_elapsed) > HARD_TIMEOUT_SECONDS * 1000
    verified_b = b[2] and trace_complete and len(server_trace['B']) == 1
    success = verified_b and b_elapsed <= protocol.deadline_ms
    reason = 'unverified'
    if verified_b:
        reason = 'verified_on_time' if success else 'verified_late'
    return {
        'success': success, 'a_recovered': a[2] and a_elapsed <= A_DEADLINE_MS and trace_complete,
        'a_elapsed_ms': a_elapsed, 'elapsed_ms': b_elapsed,
        'reason': reason,
        'attempt_count': sum(len(rows) for rows in server_trace.values()),
        'attempt_violation': attempt_violation,
        'hint_violation': hint_violation,
        'deadline_violation': deadline_violation,
        'invalid_accepted': invalid, 'trace_complete': trace_complete,
        'enqueued_ms': {workspace: ms(start) for workspace, start in enqueued.items()},
        'client_trace': client_trace, 'server_trace': server_trace,
    }
