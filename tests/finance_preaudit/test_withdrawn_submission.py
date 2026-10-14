from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine, recovery_receipt
from tests.finance_preaudit.test_flow_engine_submission import _engine, _decision, _grant, _verify_grant, _success, _material


def submitted(root):
    engine = _engine(root, 'recorded-withdrawal', authorization_source=_grant,
        authorization_verifier=_verify_grant, submit_frozen=_success)
    action = engine.next_action('travel_expense', mode='controlled_write')
    decision = root / 'input.json'
    json_artifact.write_json_atomic(decision, _decision(action))
    closed = engine.complete_review(Path(action['action_file']), decision)
    source = Path(closed['action_file'])
    state = json.loads(source.with_name('state.json').read_text())
    assert flow_engine.FlowEngine.confirmed_submission(root, state)
    observation = {'kind': 'preaudit_withdrawal_observation', 'target_root':str(root.parents[1]),
        'action_file':str(source), 'source_fingerprints':recovery_receipt._fingerprints(source.parent),
        'request_id':state['request_id'], 'received_at':state['received_at'],
        'workflow_key':state['workflow_key'], 'workflow_id':'recorded-flow', 'workflow_name':'recorded-travel', 'actor_id':'recorded-actor', 'node_id':'recorded-node',
        'last_operate_time':'2026-08-22 10:00:00', 'observed_at':datetime.now(timezone.utc).isoformat()}
    reader = flow_engine.FlowEngine(root=root, discover_candidates=engine._discover_candidates,
        discover_handled=lambda: [], collect_material=lambda request, _: _material(request))
    return reader, source, observation


def test_confirmed_withdrawal_creates_fresh_preview_and_preserves_original_success(tmp_path):
    root = tmp_path / 'runtime/finance_preaudit_flows'
    engine, source, observation = submitted(root)
    old = {p.name:p.read_bytes() for p in source.parent.iterdir() if p.is_file()}
    result = engine.recover_withdrawn_submission(source, confirmed=True, observation=observation)
    assert result['action'] == 'review_required' and result['attempt'] == 2
    assert result['write_state'] == 'not_requested' and result['mode'] == 'preview'
    state = json.loads(Path(result['state_file']).read_text())
    proof = recovery_receipt.projection(Path(result['action_file']).parent, state)
    assert proof['receipt_status'] == 'verified'
    assert proof['source_outcome'] == 'submitted_then_withdrawn'
    assert 'no_write_evidence' not in proof
    assert flow_engine.FlowEngine.resumable_review(root, state)
    assert {p.name:p.read_bytes() for p in source.parent.iterdir() if p.is_file()} == old
    with pytest.raises(ValueError, match='withdrawal_recovery_not_safe'):
        engine.recover_withdrawn_submission(source, confirmed=True, observation=observation)


@pytest.mark.parametrize('mutation', ['unconfirmed', 'wrong_identity', 'wrong_source', 'handled', 'duplicate', 'receipt_changed', 'unknown'])
def test_withdrawal_recovery_rejects_unsafe_source_or_live_state(tmp_path, mutation):
    root = tmp_path / 'runtime/finance_preaudit_flows'
    engine, source, observation = submitted(root)
    confirmed = mutation != 'unconfirmed'
    if mutation == 'wrong_identity': observation['request_id'] = 'other'
    if mutation == 'wrong_source': observation['action_file'] = str(source.parent.parent/'other/action.json')
    if mutation == 'handled': engine._discover_handled = lambda: [{'request_id':observation['request_id']}]
    if mutation == 'duplicate':
        original = engine._discover_candidates
        engine._discover_candidates = lambda workflow: original(workflow) * 2
    if mutation == 'receipt_changed': source.with_name('execution-receipt.json').write_text('{}')
    if mutation == 'unknown':
        state=json.loads(source.with_name('state.json').read_text());state['write_state']='unknown'
        source.with_name('state.json').write_text(json.dumps(state))
    with pytest.raises(ValueError, match='withdrawal_recovery_not_safe'):
        engine.recover_withdrawn_submission(source, confirmed=confirmed, observation=observation)
    assert not source.parent.with_name('attempt-2').exists()


def test_withdrawal_material_failure_is_isolated_without_reusing_old_opinion(tmp_path):
    root=tmp_path/'runtime/finance_preaudit_flows'
    engine, source, observation=submitted(root)
    def failed(*_): raise RuntimeError('invoice_buyer_read_failed')
    engine._collect_material=failed
    result=engine.recover_withdrawn_submission(source,confirmed=True,observation=observation)
    assert result['action']=='needs_admin' and result['reason']=='material_collection_failed'
    assert result['write_state']=='not_requested'
    attempt=Path(result['action_file']).parent
    assert not (attempt/'review-decision.json').exists() and not (attempt/'execution-receipt.json').exists()
    state=json.loads((attempt/'state.json').read_text())
    assert recovery_receipt.projection(attempt,state)['receipt_status']=='verified'
    from apps.oa_finance_preaudit import item_failure
    assert item_failure.isolated(attempt,state)


@pytest.mark.parametrize('mutation', ['valid', 'no_confirmation', 'extra_unresolved', 'wrong_action', 'unknown', 'proof_missing'])
def test_maintenance_explains_only_the_explicit_confirmed_withdrawal(tmp_path, mutation):
    from apps.oa_finance_preaudit import maintenance_runtime, withdrawn_submission
    from tests.finance_preaudit.test_maintenance_runtime import live_snapshot
    root=tmp_path/'runtime/finance_preaudit_flows'
    _, source, observation=submitted(root)
    proof=withdrawn_submission.source_proof(observation)
    snapshot=live_snapshot()
    snapshot['reconciliation']['continuation']={'status':'blocked','scope':'controller','isolated_pending':0,
        'blocking_reasons':{'pending_write_state_unresolved':1}}
    snapshot['reconciliation']['quarantine']['pending_quarantined']=0
    state=json.loads(source.with_name('state.json').read_text())
    row={'category':'pending_not_handled','workflow_key':state['workflow_key'],'item_key':state['case_key'],
        'attempt':1,'local_execution_state':'closed','local_write_state':'submitted'}
    snapshot['reconciliation']['findings']=[row]
    snapshot['maintenance_current_inputs'].update(verified_withdrawn_actions=[str(source)],
        pending_frozen_count=1,validated_frozen_count=1)
    if mutation=='no_confirmation':proof['withdrawal_confirmed']=False
    if mutation=='extra_unresolved':snapshot['reconciliation']['continuation']['blocking_reasons']['identity_conflict']=1
    if mutation=='wrong_action':proof['withdrawn_action']=str(source.parent/'other.json')
    if mutation=='unknown':row['local_write_state']='unknown'
    if mutation=='proof_missing':snapshot['maintenance_current_inputs'].pop('verified_withdrawn_actions')
    if mutation=='valid':
        with pytest.raises(RuntimeError):maintenance_runtime.assert_live_safe(snapshot)
        maintenance_runtime.assert_live_safe(snapshot,source_only_proof=proof)
        assert snapshot['reconciliation']['continuation']['status']=='blocked'
    else:
        with pytest.raises(RuntimeError):maintenance_runtime.assert_live_safe(snapshot,source_only_proof=proof)


@pytest.mark.parametrize('mutation', ['valid','wrong_actor','wrong_node','wrong_workflow','handled','duplicate','incomplete','not_editable','handled_error'])
def test_official_withdrawal_observation_requires_current_unique_ownership(tmp_path, monkeypatch, capsys, mutation):
    import sys
    from apps.oa_approval import api,auth,flow
    from apps.oa_finance_preaudit import withdrawn_submission
    root=tmp_path/'runtime/finance_preaudit_flows'
    _, source, observation=submitted(root)
    base={'workflowId':'recorded-flow','workflowName':'recorded-travel'}
    row={'requestId':observation['request_id'],'receiveTime':observation['received_at'],
        'userid':'recorded-actor','nodeid':'recorded-node','currentNodeId':'recorded-node','workflowBaseInfo':base}
    data={'requestId':observation['request_id'],'currentNodeId':'recorded-node','workflowBaseInfo':base,
        'canEdit':True,'canView':True,'lastOperateTime':'2026-08-22 10:00:00'}
    if mutation=='wrong_actor':row['userid']='other'
    if mutation=='wrong_node':data['currentNodeId']='other'
    if mutation=='wrong_workflow':data['workflowBaseInfo']={**base,'workflowId':'other'}
    if mutation=='not_editable':data['canEdit']=False
    class Client:
        def _post_readonly(self,endpoint,*_):
            if endpoint in api.HANDLED_ENDPOINTS:
                if mutation=='handled_error':return {'code':'FAILED','message':'recorded-read-error'}
                return {'data':[row] if mutation=='handled' else []}
            if mutation=='incomplete':return {'data':[row]*200}
            return {'data':[row,row] if mutation=='duplicate' else [row]}
        def get(self,*_):return {'code':'SUCCESS','data':data}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys,'argv',['observe',str(source)])
    monkeypatch.setattr(auth,'load_local_env',lambda:None)
    monkeypatch.setattr(auth,'env',lambda _: 'recorded-actor')
    monkeypatch.setattr(api,'build_api',lambda _:Client())
    monkeypatch.setattr(flow,'load_all_configs',lambda:[{'flow_key':'travel_expense',
        'workflow_id':base['workflowId'],'workflow_name':base['workflowName']}])
    if mutation=='valid':
        exec(withdrawn_submission._OBSERVE,{})
        seen=json.loads(capsys.readouterr().out)
        assert withdrawn_submission.valid_observation(source,json.loads(source.with_name('state.json').read_text()),seen)
    else:
        with pytest.raises(SystemExit):exec(withdrawn_submission._OBSERVE,{})
        assert json.loads(capsys.readouterr().out)['status']=='blocked'


def test_bootstrap_current_inputs_validate_old_success_without_importing_new_module(tmp_path, monkeypatch):
    import hashlib, shutil, sys
    from apps.oa_finance_preaudit import maintenance_runtime, withdrawn_submission, production_config
    for key in ('OA_FROZEN_WRITER_LOCK_FILE','OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
            'OA_FINANCE_PREAUDIT_STOP_FILE','OA_FINANCE_PREAUDIT_RUNTIME_ROOT'):
        monkeypatch.delenv(key,raising=False)
    root=tmp_path/'runtime/finance_preaudit_flows'
    _, source, observation=submitted(root)
    source_root=Path(maintenance_runtime.__file__).resolve().parents[2]
    shutil.copytree(source_root/'apps',tmp_path/'apps',ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'apps/oa_finance_preaudit/withdrawn_submission.py').unlink()
    interpreter=tmp_path/'.venv/bin/python';interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "'+sys.executable+'" "$@"\n');interpreter.chmod(0o700)
    isolated_auth=tmp_path.parent/(tmp_path.name+'-auth.env');isolated_auth.write_text('');isolated_auth.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE',str(isolated_auth))
    state=json.loads(source.with_name('state.json').read_text())
    policy=tmp_path/'runtime/control/write-policy.json';policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({'schema_version':1,'kind':'preaudit_write_policy',
        'authorized_workflows':[*production_config.FINANCE_WORKFLOWS,'packaging_layout_approval']}));policy.chmod(0o600)
    status=tmp_path/'status.json';status.write_text(json.dumps({'reconciliation':{'findings':[
        {'category':'pending_not_handled','workflow_key':state['workflow_key'],'item_key':state['case_key'],'attempt':1}]}}))
    receipt={'path':str(status),'sha256':hashlib.sha256(status.read_bytes()).hexdigest()}
    with pytest.raises(RuntimeError,match='maintenance_frozen_inputs_untrusted'):
        maintenance_runtime.read_current_inputs(tmp_path,receipt)
    proof=withdrawn_submission.source_proof(observation)
    result=maintenance_runtime.read_current_inputs(tmp_path,receipt,source_only_proof=proof,require_reaped=True)
    assert result['verified_withdrawn_actions']==[str(source)]
    assert result['validated_frozen_count']==result['pending_frozen_count']==1
    source.with_name('execution-receipt.json').write_text('{}')
    with pytest.raises(RuntimeError,match='maintenance_frozen_inputs_untrusted'):
        maintenance_runtime.read_current_inputs(tmp_path,receipt,source_only_proof=proof,require_reaped=True)


def test_withdrawn_recovery_is_reauthorized_and_submits_only_new_decision(tmp_path):
    root=tmp_path/'runtime/finance_preaudit_flows'
    engine, source, observation=submitted(root)
    collected=[]
    def collect(request, attempt):
        collected.append(attempt)
        material=_material(request)
        material['declared_fields'][0]['value']='2026-10'
        return material
    writes=[]
    controlled=flow_engine.FlowEngine(root=root,discover_candidates=engine._discover_candidates,
        discover_handled=lambda:[],collect_material=collect,
        authorization_source=_grant,authorization_verifier=_verify_grant,
        submit_frozen=lambda job:writes.append(job) or _success(job))
    action=controlled.recover_withdrawn_submission(source,confirmed=True,observation=observation,mode='controlled_write')
    assert collected==[Path(action['action_file']).parent]
    material=json.loads(Path(action['action_file']).with_name('material.json').read_text())
    assert material['declared_fields'][0]['value']=='2026-10'
    assert action['attempt']==2 and action['mode']=='controlled_write' and writes==[]
    decision=root/'new-decision.json';json_artifact.write_json_atomic(decision,_decision(action))
    result=controlled.complete_review(Path(action['action_file']),decision)
    assert result['action']=='closed' and result['write_state']=='submitted' and len(writes)==1
    assert flow_engine.FlowEngine.confirmed_submission(root,json.loads(Path(result['state_file']).read_text()))


@pytest.mark.parametrize('confirmation', [(False,False),(True,False),(False,True),(True,True)])
def test_production_prepare_requires_both_confirmations_and_never_submits(tmp_path, monkeypatch, confirmation):
    from apps.oa_finance_preaudit import withdrawn_submission,maintenance_runtime,production_release,cli
    from tests.finance_preaudit.test_maintenance_runtime import live_snapshot
    for key in ('OA_FROZEN_WRITER_LOCK_FILE','OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
            'OA_FINANCE_PREAUDIT_STOP_FILE','OA_FINANCE_PREAUDIT_RUNTIME_ROOT'):
        monkeypatch.delenv(key,raising=False)
    root=tmp_path/'runtime/finance_preaudit_flows'
    engine, source, observation=submitted(root)
    state=json.loads(source.with_name('state.json').read_text())
    snapshot=live_snapshot()
    snapshot['reconciliation']['continuation']={'status':'blocked','scope':'controller','isolated_pending':0,
        'blocking_reasons':{'pending_write_state_unresolved':1}}
    snapshot['reconciliation']['quarantine']['pending_quarantined']=0
    snapshot['reconciliation']['findings']=[{'category':'pending_not_handled','workflow_key':state['workflow_key'],
        'item_key':state['case_key'],'attempt':1,'local_execution_state':'closed','local_write_state':'submitted'}]
    snapshot['maintenance_current_inputs'].update(verified_withdrawn_actions=[str(source)],
        pending_frozen_count=1,validated_frozen_count=1)
    calls=[]
    def factory(*, controlled):
        assert controlled is True
        calls.append(controlled)
        return flow_engine.FlowEngine(root=root,discover_candidates=engine._discover_candidates,
            discover_handled=lambda:[],collect_material=lambda request,_:_material(request),
            authorization_source=_grant,authorization_verifier=_verify_grant,
            submit_frozen=lambda _:pytest.fail('prepare cannot write OA'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(production_release,'_source',lambda *_a,**_k:'recorded-revision')
    monkeypatch.setattr(production_release,'_observed_revision',lambda _:'recorded-revision')
    monkeypatch.setattr(withdrawn_submission,'observe',lambda *_:observation)
    monkeypatch.setattr(maintenance_runtime,'read_live_status',lambda *_a,**_k:snapshot)
    monkeypatch.setattr(cli,'_live_flow_engine',factory)
    result=withdrawn_submission.prepare(source,confirmed=confirmation[0],confirmed_write=confirmation[1])
    assert result['write_state']=='not_requested'
    if all(confirmation):
        assert result['status']=='prepared' and result['result']['mode']=='controlled_write' and calls==[True]
        assert not source.parent.with_name('attempt-2').joinpath('execution-receipt.json').exists()
    else:
        assert result['status']=='confirmation_required' and calls==[]
        assert not source.parent.with_name('attempt-2').exists()
