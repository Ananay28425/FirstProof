"""Two credible read-only HTTP retry policies. Only scheduling differs.

Both respect valid Retry-After, use the same deadline/attempt limits, retryable errors,
per-attempt timeout and capped full jitter. Neither knows the scenario or expected result.
"""
import asyncio
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, asdict
import httpx

CONTROL = 'control'
CANDIDATE = 'context_aware'
POLICIES = {CONTROL, CANDIDATE}
RETRYABLE = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Outcome:
    """Client observation, not an assertion that the operation is correct."""
    elapsed_ms: float
    attempts: list[dict]
    status: int | None
    body: dict | None
    reason: str

    def json(self):
        return asdict(self)


def retry_after(value: str | None, now: datetime | None = None) -> float:
    """Parse nonnegative integer seconds or an HTTP date per HTTP Retry-After."""
    if not value:
        return 0.0
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(min(int(value[:12]), 10**12)) if len(value) <= 12 else 10**12
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, (date-(now or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0.0


def retry_delay(policy: str, index: int, uniform: float, hint: float, remaining: float) -> float:
    """Full jitter with identical base/cap. Candidate preserves 120 ms for another try.

    Never truncate a server-imposed minimum wait. Returning a delay beyond the budget
    causes both policies to stop safely. No policy is guaranteed to perform better.
    """
    if policy not in POLICIES:
        raise ValueError('Unknown retry policy')
    cap = min(0.8, 0.20 * 2**(index-1))
    if policy == CANDIDATE:
        cap = min(cap, max(0.0, remaining-0.120))
    return max(hint, cap * uniform)


async def execute(client: httpx.AsyncClient, url: str, policy: str, deadline_ms: int,
                  max_attempts: int, seed: int, *, params=None, headers=None, attempt_timeout_ms=600) -> Outcome:
    """Execute actual read-only GETs; monotonic deadline includes network and sleep.

    The same implementation is used by normal SerpAPI research and controlled trials.
    No HTTPX automatic retry is enabled. Returned bodies are bounded before decoding.
    """
    if policy not in POLICIES:
        raise ValueError('Unknown retry policy')
    start = time.monotonic()
    end = start + deadline_ms/1000
    rng = random.Random(seed)
    attempts = []
    status, body, reason = None, None, 'attempt_budget_exhausted'
    for index in range(1, max_attempts+1):
        remaining = end-time.monotonic()
        if remaining <= 0:
            reason = 'deadline'; break
        trace = {'ordinal': index, 'started_ms': (time.monotonic()-start)*1000, 'status': None, 'retry_after': 0.0}
        attempts.append(trace)
        hint = 0.0
        status, body = None, None
        try:
            async with asyncio.timeout(min(remaining, attempt_timeout_ms/1000)):
                async with client.stream('GET', url, params=params, headers=headers, follow_redirects=False) as response:
                    status = response.status_code
                    trace['status'] = status
                    hint = retry_after(response.headers.get('Retry-After'))
                    trace['retry_after'] = hint
                    if status == 200:
                        chunks, total = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > 1_000_000:
                                reason = 'response_too_large'; break
                            chunks.append(chunk)
                        else:
                            try:
                                import json
                                parsed = json.loads(b''.join(chunks))
                                body = parsed if isinstance(parsed, dict) else None
                                metadata = body.get('search_metadata', {}) if body else {}
                                valid = isinstance(metadata, dict) and metadata.get('status') == 'Success' and isinstance(body.get('organic_results', []), list)
                                reason = 'response_received' if valid else 'invalid_response'
                            except (ValueError, UnicodeError):
                                reason = 'invalid_json'
                        break
                    if status not in RETRYABLE:
                        reason = 'non_retryable_status'; break
                    reason = 'retryable_status'
        except (TimeoutError, httpx.TimeoutException):
            reason = 'attempt_timeout'
        except httpx.TransportError:
            reason = 'transport_error'
        finally:
            trace['finished_ms'] = (time.monotonic()-start)*1000
        if index == max_attempts:
            break
        remaining = end-time.monotonic()
        delay = retry_delay(policy, index, rng.random(), hint, remaining)
        trace['scheduled_delay_ms'] = delay*1000
        if delay >= remaining or remaining <= 0:
            reason = 'retry_delay_exceeds_budget'; break
        await asyncio.sleep(delay)
    return Outcome((time.monotonic()-start)*1000, attempts, status, body, reason)
