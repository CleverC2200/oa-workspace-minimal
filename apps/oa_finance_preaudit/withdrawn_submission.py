"""Explicit recovery of a confirmed submission withdrawn by its human operator."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from . import maintenance_runtime, recovery_receipt

KIND = 'preaudit_confirmed_withdrawal_source_only'


def valid_observation(action: Path, state: dict, observation: dict, *, fresh: bool = True) -> bool:
    """Bind the recorded official observation to the untouched successful attempt."""
    try:
        if (not isinstance(observation, dict) or set(observation) != {
                'kind', 'target_root', 'action_file', 'source_fingerprints', 'request_id', 'received_at',
                'workflow_key', 'workflow_id', 'workflow_name', 'actor_id', 'node_id', 'last_operate_time', 'observed_at'}
                or observation['kind'] != 'preaudit_withdrawal_observation'
                or observation['target_root'] != str(action.parents[6])
                or observation['action_file'] != str(action)
                or observation['source_fingerprints'] != recovery_receipt._fingerprints(action.parent)
                or any(observation[key] != state[key] for key in ('request_id', 'received_at', 'workflow_key'))
                or any(not isinstance(observation[key], str) or not observation[key].strip()
                    for key in ('workflow_id', 'workflow_name', 'actor_id', 'node_id', 'last_operate_time'))):
            return False
        seen = datetime.fromisoformat(observation['observed_at'])
        age = (datetime.now(timezone.utc) - seen).total_seconds()
        return seen.tzinfo is not None and age >= 0 and (not fresh or age <= 300)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


# Both bootstrap maintenance and installed recovery use this exact bridge. Only
# existing target modules are imported; candidate modules/credentials are never injected.
_OBSERVE = r'''
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from apps.oa_approval import api, auth, flow
from apps.oa_finance_preaudit import flow_engine, recovery_receipt
root = Path.cwd().resolve()
action = Path(sys.argv[1])
def all_rows(client, endpoint):
    rows, previous = [], None
    for page_no in range(1, 101):
        response = client._post_readonly(endpoint, {'pageNo':str(page_no), 'pageSize':'200', 'isNeedOs':True})
        api._raise_on_readonly_error(response, code='withdrawal_queue_fetch_failed')
        page = api._extract_rows(response)
        if not page:
            return rows
        if page == previous:
            raise ValueError()
        rows.extend(page)
        if len(page) < 200:
            return rows
        previous = page
    raise ValueError()
try:
    flows = root / 'runtime/finance_preaudit_flows'
    if not action.is_absolute() or not action.is_relative_to(flows) or not flow_engine._is_attempt_action_path(action, flows):
        raise ValueError()
    state = flow_engine._read_object(action.with_name('state.json'), code='withdrawal_recovery_not_safe')
    if (state['action_file'] != str(action) or state.get('workflow_key') not in flow_engine._FINANCE_WORKFLOW_KEYS
            or not flow_engine.FlowEngine.confirmed_submission(flows, state)):
        raise ValueError()
    auth.load_local_env()
    configs = flow.load_all_configs()
    config = next(c for c in configs if c['flow_key'] == state['workflow_key'])
    client = api.build_api(config)
    rows = [row for row in all_rows(client, api.TODO_ENDPOINT)
        if str(row.get('requestId') or row.get('requestid') or '') == state['request_id']]
    if len(rows) != 1:
        raise ValueError()
    raw = rows[0]
    for upper, lower in (('requestId','requestid'), ('userid','userId'), ('receiveTime','receivetime')):
        if raw.get(upper) and raw.get(lower) and str(raw[upper]) != str(raw[lower]):
            raise ValueError()
    pending = api._pending_case(raw)
    actor = auth.env('OA_UID')
    if (pending['request_id'] != state['request_id'] or pending['received_at'] != state['received_at']
            or not actor or pending['operator_id'] != actor or not flow.workflow_matches(raw, config)
            or any(api._handled_case(row)['request_id'] == state['request_id']
                for endpoint in api.HANDLED_ENDPOINTS for row in all_rows(client, endpoint))):
        raise ValueError()
    context = client.get('/api/workflow/paService/getWorkflowRequest', {'requestId':state['request_id']})
    data = context.get('data')
    if (context.get('code') != 'SUCCESS' or not isinstance(data, dict)
            or data.get('canView') is not True or data.get('canEdit') is not True
            or str(data.get('requestId') or '') != state['request_id']
            or not flow.workflow_matches(data, config)
            or flow.workflow_identity(data) != {key:pending[key] for key in ('workflow_id','workflow_name')}):
        raise ValueError()
    node = str(raw.get('nodeid') or '')
    if not node or node != str(raw.get('currentNodeId') or '') or node != str(data.get('currentNodeId') or ''):
        raise ValueError()
    print(json.dumps({'kind':'preaudit_withdrawal_observation', 'target_root':str(root), 'action_file':str(action),
        'source_fingerprints':recovery_receipt._fingerprints(action.parent), 'request_id':state['request_id'],
        'received_at':state['received_at'], 'workflow_key':state['workflow_key'],
        'workflow_id':pending['workflow_id'], 'workflow_name':pending['workflow_name'], 'actor_id':actor, 'node_id':node,
        'last_operate_time':str(data.get('lastOperateTime') or ''), 'observed_at':datetime.now(timezone.utc).isoformat()}))
except (OSError, ValueError, RuntimeError, KeyError, TypeError, StopIteration):
    print(json.dumps({'status':'blocked', 'error_code':'withdrawal_recovery_not_safe'}))
    raise SystemExit(1)
'''


def observe(target_root: Path, action: Path) -> dict:
    root = Path(target_root).absolute()
    maintenance_runtime.assert_target_environment(root)
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    result = maintenance_runtime._run_readonly([str(root / '.venv/bin/python'), '-B', '-E', '-c', _OBSERVE,
        str(action)], target_root=root, environment=environment, timeout=300, require_reaped=True)
    if result.returncode or len(result.stdout.encode()) > 32_000:
        raise RuntimeError('withdrawal_recovery_not_safe')
    value = json.loads(result.stdout)
    state = json.loads(action.with_name('state.json').read_bytes())
    if not valid_observation(action, state, value):
        raise RuntimeError('withdrawal_recovery_not_safe')
    return value


def source_proof(observation: dict) -> dict:
    return {'kind':KIND, 'target_root':observation['target_root'], 'withdrawn_action':observation['action_file'],
        'source_fingerprints':observation['source_fingerprints'], 'withdrawal_confirmed':True}


def same_observation(first: dict, current: dict) -> bool:
    return {k:v for k,v in first.items() if k != 'observed_at'} == {k:v for k,v in current.items() if k != 'observed_at'}


def prepare(action: Path, *, confirmed: bool = False, confirmed_write: bool = False) -> dict:
    """Default to readonly preflight; two confirmations prepare fresh material without OA submission."""
    from . import cli, production_release
    root = Path.cwd().resolve()
    production_release._assert_no_stop(root)
    with maintenance_runtime.target_runtime_leases(root):
        production_release._source(root, production_release._observed_revision(root), production_release.ORIGIN, synchronized=True)
        observation = observe(root, action)
        if not confirmed or not confirmed_write:
            return {'status':'confirmation_required', 'write_state':'not_requested',
                'withdrawal_confirmation_required':not confirmed, 'write_confirmation_required':not confirmed_write,
                'approval_started':False, 'observation':observation}
        proof = source_proof(observation)
        snapshot = maintenance_runtime.read_live_status(root, require_reaped=True, source_only_proof=proof)
        maintenance_runtime.assert_live_safe(snapshot, source_only_proof=proof)
        engine = cli._live_flow_engine(controlled=True)
        result = engine.recover_withdrawn_submission(action, confirmed=True, observation=observation, mode='controlled_write')
        return {'status':'prepared' if result.get('action') == 'review_required' else 'needs_admin',
            'write_state':'not_requested', 'result':result, 'original_evidence_preserved':True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action-file', required=True, type=Path)
    parser.add_argument('--confirm-withdrawal', choices=['YES'])
    parser.add_argument('--confirm-write', choices=['YES'])
    args = parser.parse_args(argv)
    try:
        result = prepare(args.action_file, confirmed=args.confirm_withdrawal == 'YES', confirmed_write=args.confirm_write == 'YES')
    except (OSError, ValueError, RuntimeError) as error:
        result = {'status':'blocked', 'error_code':str(error), 'write_state':'not_requested'}
    print(json.dumps(result, ensure_ascii=False))
    return int(result['status'] == 'blocked')


if __name__ == '__main__':
    raise SystemExit(main())
