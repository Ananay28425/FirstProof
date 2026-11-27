"""Provider contract unit tests with explicit test doubles, never live evidence."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import httpx
import pytest
from firstproof.config import Settings
from firstproof.integrations import FlagsmithAdmin, NativeRuntime, IntegrationError, reconcile_native, strict_proposal_schema


@pytest.mark.asyncio
async def test_admin_native_payloads_and_endpoints():
    cfg=Settings(flagsmith_api_url='https://flagsmith.invalid/api/v1/',flagsmith_environment_key='sandbox',flagsmith_admin_key='test-only',flagsmith_feature_id=42,flagsmith_candidate_id=11,flagsmith_project_id=5)
    calls=[]
    def handler(request):
        assert request.headers['Authorization']=='Api-Key test-only'
        calls.append((request.method,request.url.path,json.loads(request.content) if request.content else None))
        path=request.url.path
        if path.endswith('/warehouse-connections/'):return httpx.Response(200,json=[{'status':'connected'}])
        if path.endswith('/experiment-metrics/'):return httpx.Response(201,json={'id':9})
        if path.endswith('/experiments/'):return httpx.Response(201,json={'id':8})
        if path.endswith('/start/'):return httpx.Response(200,json={})
        if path.endswith('/experiments/8/'):return httpx.Response(200,json={'status':'running'})
        return httpx.Response(200,json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        admin=FlagsmithAdmin(cfg,c);assert (await admin.warehouses())[0]['status']=='connected'
        metric=await admin.create_metric('operation_verified_test')
        exp=await admin.create_experiment('test','Hypothesis with a real baseline',metric['id'])
        assert (await admin.start(exp['id']))['status']=='running'
        await admin.create_metric('workspace_b_elapsed_ms', numeric=True)
        await admin.create_experiment('Cooldown', 'Workspace-specific cooldown', 9, cooldown=True, latency_metric_id=10)
    body=calls[2][2]
    assert body['feature']==42 and body['metrics'][0]['metric']==9
    assert body['experiment_rollout']['multivariate_feature_state_values']==[{'multivariate_feature_option':11,'percentage_allocation':50}]
    assert calls[1][2]['definition']['event']=='operation_verified_test'
    assert calls[-2][2]['aggregation'] == 'mean' and calls[-2][2]['direction'] == 'down'
    assert calls[-1][2]['experiment_rollout']['feature_state_value']['value'] == 'shared_cooldown'
    assert calls[-1][2]['metrics'] == [
        {'metric': 9, 'expected_direction': 'increase'},
        {'metric': 10, 'expected_direction': 'decrease'},
    ]


@pytest.mark.asyncio
async def test_runtime_enrollment_and_exposure_metadata():
    # Bypass optional dependency installation; emulate exact documented SDK objects.
    runtime=NativeRuntime.__new__(NativeRuntime)
    runtime.lock=asyncio.Lock();runtime.flag='firstproof_retry_policy';runtime.sdk=Mock()
    flag=SimpleNamespace(enabled=True,value='context_aware',variant='context_aware',experiment=SimpleNamespace(id=9,in_experiment=True))
    runtime.sdk.get_identity_flags.return_value.get_flag.return_value=flag
    assert await runtime.evaluate('identity',9)=='context_aware'
    await runtime.expose('identity','context_aware',9)
    runtime.sdk.track_exposure_event.assert_called_once_with('firstproof_retry_policy',identifier='identity',value='context_aware',metadata={'experiment_id':9})
    await runtime.conversion('identity','operation_verified_test')
    runtime.sdk.track_event.assert_called_once_with('operation_verified_test',identifier='identity')
    with pytest.raises(IntegrationError): await runtime.evaluate('identity',10)
    flag.enabled=False
    with pytest.raises(IntegrationError): await runtime.evaluate('identity',9)
    flag.enabled = True
    flag.value, flag.variant = 'shared_cooldown', 'control'
    assert await runtime.evaluate('episode', 9, cooldown=True) == 'control'
    await runtime.expose('episode', 'control', 9)
    runtime.sdk.track_exposure_event.assert_called_with(
        'firstproof_retry_policy', identifier='episode', value='control', metadata={'experiment_id': 9},
    )
    await runtime.numeric_event('episode', 'elapsed', 21.5)
    runtime.sdk.track_event.assert_called_with('elapsed', identifier='episode', value=21.5)


def test_cooldown_reconciliation_requires_both_native_count_views():
    candidate = 'workspace_method_cooldown'
    local = {'groups': {'control': {'n': 52, 'successes': 30}, candidate: {'n': 51, 'successes': 50}}}
    raw = {'results': {'last_error_at': None, 'as_of': 'now', 'payload': {
        'srm_p_value': 0.9, 'metrics': [{
            'metric_id': 9, 'variants': {'control': {'n': 52, 'sum': 30}, candidate: {'n': 51, 'sum': 50}},
            'inference': {candidate: {'chance_to_win': 0.99, 'ci_low': 0.1, 'ci_high': 0.9}},
        }],
    }}}
    counts = {'control': 52, candidate: 51}
    exposures = {'exposures': {'last_error_at': None, 'as_of': 'now', 'payload': {
        'excluded_identities': 0, 'timeseries': {'points': [{'new_identities': counts}]},
    }}}
    assert reconcile_native(raw, local, 9, exposures, cooldown=True)['reconciled']
    counts[candidate] -= 1
    assert not reconcile_native(raw, local, 9, exposures, cooldown=True)['reconciled']
    counts[candidate] += 1
    raw['results']['payload']['srm_p_value'] = None
    assert reconcile_native(raw, local, 9, exposures, cooldown=True)['srm_warning']
    check = reconcile_native(raw, local, 9, exposures, cooldown=True, verified_equal_split=True)
    assert not check['srm_warning']
    assert check['srm_p_value'] is None and 0.9 < check['independent_srm_p_value'] < 1
    raw['results']['payload']['srm_p_value'] = 0.0001
    check = reconcile_native(raw, local, 9, exposures, cooldown=True, verified_equal_split=True)
    assert check['srm_warning'] and check['independent_srm_p_value'] is None
    raw['results']['payload']['srm_p_value'] = float('nan')
    assert not reconcile_native(raw, local, 9, exposures, cooldown=True)['reconciled']


def test_strict_model_schema_requires_every_object_property():
    schema=strict_proposal_schema()
    def check(node):
        if isinstance(node,dict):
            assert 'default' not in node
            if node.get('type')=='object' and 'properties' in node:
                assert set(node['required'])==set(node['properties']) and node['additionalProperties'] is False
            for val in node.values():check(val)
        elif isinstance(node,list):
            for val in node:check(val)
    check(schema)
