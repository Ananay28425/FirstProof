"""Deterministic outcome oracle and raw metric aggregation; never uses an LLM."""
import math
from collections import Counter
from .policies import Outcome, CONTROL, CANDIDATE
from .contracts import COOLDOWN_VARIANTS, Protocol


def verify(outcome: Outcome, server: list[dict], expected: str, protocol: Protocol) -> dict:
    """Separate success from safety. A safe failure is a nonconversion, not a violation.

    Tiny timer overshoot is reported separately as a guardrail (10 ms scheduling tolerance).
    The deadline for the primary metric remains exact. Validate Retry-After using server
    arrivals and client receipt time where the response was actually observed.
    """
    integrity = False
    if outcome.status == 200 and outcome.body:
        entries = outcome.body.get('organic_results')
        integrity = isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict) and entries[0].get('snippet') == expected
    invalid_accepted = outcome.reason == 'response_received' and not integrity
    attempt_violation = max(len(server), len(outcome.attempts)) > protocol.max_attempts
    hint_violation = False
    for prior, following in zip(outcome.attempts, outcome.attempts[1:]):
        hint = prior.get('retry_after', 0)
        if hint and following['started_ms'] + 1 < prior['finished_ms'] + hint*1000:
            hint_violation = True
    deadline_violation = outcome.elapsed_ms > protocol.deadline_ms + 10
    # If an attempt was never seen by the controlled server, infrastructure validity is
    # uncertain: never silently claim that a transport outage was an intended test fault.
    trace_complete = len(server) == len(outcome.attempts) and bool(server)
    success = (integrity and outcome.reason == 'response_received' and outcome.elapsed_ms <= protocol.deadline_ms
               and not attempt_violation and not hint_violation and trace_complete)
    return {'success': success, 'invalid_accepted': invalid_accepted,
            'attempt_violation': attempt_violation, 'hint_violation': hint_violation,
            'deadline_violation': deadline_violation, 'trace_complete': trace_complete,
            'elapsed_ms': outcome.elapsed_ms, 'attempt_count': len(outcome.attempts),
            'reason': outcome.reason, 'client_trace': outcome.attempts, 'server_trace': server}


def percentile(values, p):
    """Nearest-rank percentile, explicit and reproducible."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values)*p)-1)]


def summarize(rows: list[dict], protocol: Protocol) -> dict:
    """Aggregate only recorded observations, counting failures in the denominator."""
    result = {'groups': {}, 'planned': protocol.workloads, 'observed': len(rows), 'by_scenario': {}}
    variants = tuple(COOLDOWN_VARIANTS) if protocol.cooldown else (CONTROL, CANDIDATE)
    for variant in variants:
        group = [r for r in rows if r['assignment'] == variant]
        n = len(group)
        successes = sum(r['success'] for r in group)
        result['groups'][variant] = {'n': n, 'successes': successes, 'rate': successes/n if n else None,
            'mean_attempts': sum(r['attempt_count'] for r in group)/n if n else None,
            'p50_ms': percentile([r['elapsed_ms'] for r in group], .50),
            'p95_ms': percentile([r['elapsed_ms'] for r in group], .95),
            'p99_ms': percentile([r['elapsed_ms'] for r in group], .99),
            'violations': {k: sum(bool(r[k]) for r in group) for k in ('invalid_accepted','attempt_violation','hint_violation','deadline_violation')},
            'incomplete_traces': sum(not r['trace_complete'] for r in group)}
        if protocol.cooldown:
            result['groups'][variant]['a_recoveries'] = sum(r['a_recovered'] for r in group)
            result['groups'][variant]['a_recovery_rate'] = (
                result['groups'][variant]['a_recoveries'] / n if n else None
            )
        for family in {r['family'] for r in group}:
            subset = [r for r in group if r['family'] == family]
            result['by_scenario'].setdefault(family, {})[variant] = {'n': len(subset), 'successes': sum(r['success'] for r in subset)}
    a,b = (result['groups'][k] for k in variants)
    result['lift_pp'] = 100*(b['rate']-a['rate']) if a['n'] and b['n'] else None
    result['amplification_pct'] = 100*(b['mean_attempts']/a['mean_attempts']-1) if a['mean_attempts'] and b['mean_attempts'] is not None else None
    result['complete'] = len(rows) == protocol.workloads and all(r['trace_complete'] for r in rows) and a['n']+b['n']==len(rows)
    result['srm_warning'] = bool(len(rows) and (a['n']-b['n'])**2/len(rows) > 10.8276) # chi-square 1 df, p<.001
    result['reasons'] = dict(Counter(r['reason'] for r in rows))
    return result


def wilson(k, n, z=1.96):
    """Wilson interval for a proportion; conservative joint bound below is diagnostic."""
    if not n: return (0.0, 1.0)
    p = k/n; d = 1+z*z/n
    c = (p+z*z/(2*n))/d
    h = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return max(0,c-h), min(1,c+h)


def release_decision(summary: dict, protocol: Protocol, native: dict, validation_passed: bool = False) -> dict:
    """Fail closed. Native evidence is required, local results cannot impersonate it.

    Uses conservative Bonferroni Wilson difference bound (each interval 97.5%) to
    require evidence of practical benefit. The native win probability remains native.
    """
    reasons = []
    variants = tuple(COOLDOWN_VARIANTS) if protocol.cooldown else (CONTROL, CANDIDATE)
    a,b = (summary['groups'][k] for k in variants)
    lower = 100*(wilson(b['successes'],b['n'],2.2414)[0]-wilson(a['successes'],a['n'],2.2414)[1])
    if not summary['complete'] or summary['srm_warning']: reasons.append('Incomplete or imbalanced local observations')
    if any(b['violations'].values()): reasons.append('Candidate safety guardrail failed')
    if protocol.cooldown and not workspace_recovery_preserved(a, b):
        reasons.append('Workspace A recovery regressed or is unavailable')
    if summary['amplification_pct'] is None or summary['amplification_pct'] > protocol.max_amplification_pct: reasons.append('Request amplification exceeds threshold or is unavailable')
    if min(a['n'],b['n']) < 50 or min(a['successes'],b['successes']) < 5: reasons.append('Insufficient native reporting minimum')
    if lower < protocol.min_effect_pp: reasons.append('Practical improvement not established by conservative lower bound')
    if native.get('reconciled') and native.get('srm_warning') is not False:
        if protocol.cooldown and native.get('srm_p_value') is None and native.get('independent_srm_p_value') is None:
            reasons.append('Sample ratio check unavailable. Verify the active 50/50 rollout and reconcile at least 100 exposures.')
        else:
            reasons.append('Native sample ratio check failed or is unavailable')
    if not native.get('reconciled') or native.get('win_probability',0) < .95 or not native.get('credible_interval') or native['credible_interval'][0] <= 0:
        reasons.append('Native statistical evidence missing, inconsistent or inconclusive')
    if protocol.purpose != 'main': reasons.append('Only a main protocol can authorize promotion')
    if not validation_passed: reasons.append('Independent reserved validation not approved')
    return {'eligible': not reasons, 'decision': 'eligible_for_human_approval' if not reasons else 'retain_incumbent', 'reasons': reasons, 'effect_lower_bound_pp': lower}


def workspace_recovery_preserved(control: dict, candidate: dict) -> bool:
    """Require no observed decrease in A's recovery rate."""
    baseline = control.get('a_recovery_rate')
    proposed = candidate.get('a_recovery_rate')
    return baseline is not None and proposed is not None and proposed >= baseline
