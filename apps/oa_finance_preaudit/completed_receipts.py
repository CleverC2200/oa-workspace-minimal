from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from uuid import UUID

from . import app_review_shutdown, attempt_state, flow_engine, host_chat_review as host, review_decision


def summarize(*, root: Path, owner: str, run_ids: list[str]) -> dict:
    try:
        if str(UUID(owner)) != owner or not run_ids or len(run_ids) > 100:
            raise ValueError
        if any(UUID(hex=run).hex != run for run in run_ids):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('completed_receipts_selection_invalid') from error
    root = root.expanduser().absolute()
    if '..' in root.parts or any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
        raise ValueError('completed_receipts_root_invalid')
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda workflow: (),
                                    collect_material=lambda request, attempt: {})
    counts = Counter(completed=0, excluded=0, invalid=0, duplicate=0)
    workflows = Counter()
    details, rounds, seen = [], [], set()
    for run in dict.fromkeys(run_ids):
        try:
            value = app_review_shutdown.sealed_round(owner=owner, run_id=run)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            counts['invalid'] += 1
            rounds.append({'run_id': run, 'status': 'invalid', 'reason': 'sealed_round_unverified'})
            continue
        rounds.append({'run_id': run, 'status': 'verified', 'reason': value['close_reason']})
        requests = value.get('requests', {})
        if not isinstance(requests, dict):
            counts['invalid'] += 1
            continue
        for request_id, published in requests.items():
            item = {'run_id': run, 'request_id': request_id}
            try:
                path = Path(published['request_file'])
                if '..' in path.parts or not path.is_relative_to(root):
                    raise ValueError('request_untrusted')
                raw, request = host._read(path)
                kind = request.get('review_kind')
                if (hashlib.sha256(raw).hexdigest() != published['request_sha256']
                        or request.get('kind') != 'preaudit_host_review_request' or request.get('schema_version') != 1
                        or request.get('owner_task_id') != owner or request.get('run_id') != run
                        or request.get('controller_instance_id') != value['controller_instance_id']
                        or request.get('request_id') != request_id
                        or kind not in {'finance', 'packaging_draft', 'packaging_confirmation'}):
                    raise ValueError('request_untrusted')
                action_file = Path(request['action_file'])
                expected = host._request_file(action_file, kind, owner=owner, run_id=run if value['schema_version'] == 2 else None)
                if '..' in action_file.parts or path != expected or not action_file.is_relative_to(root):
                    raise ValueError('request_untrusted')
                if kind == 'packaging_confirmation':
                    item.update(status='excluded', reason='independent_recheck')
                else:
                    action = attempt_state.read_action(action_file)
                    state = json.loads(attempt_state.read_input(action_file.with_name('state.json')))
                    frozen = request['input_fingerprints']
                    if any(frozen[name] != hashlib.sha256(attempt_state.read_input(action_file.with_name(name))).hexdigest()
                           for name in ('material.json', 'review-packet.json')):
                        raise ValueError('request_untrusted')
                    if ('action_reference_sha256' in state and not attempt_state.legacy_contract(state)
                            and frozen['action.json'] != state['action_reference_sha256']):
                        raise ValueError('request_untrusted')
                    item.update(action_file=str(action_file), workflow_key=state['workflow_key'])
                    assigned = host.sealed_assignment(path, request, value)
                    if assigned['state'] in {'assigned', 'failed_fenced', 'failed_sealed'}:
                        item.update(status='excluded', reason='not_submitted_in_round')
                    elif (action.get('action') != 'closed' or action.get('execution_state') != 'closed'
                            or state.get('action') != 'closed' or state.get('execution_state') != 'closed'):
                        item.update(status='excluded', reason='action_not_closed')
                    else:
                        for name in ('execution-receipt.json', 'review-decision.json', 'remark.txt', 'submission-envelope.json'):
                            attempt_state.read_input(action_file.with_name(name))
                        receipt = engine._completion_receipt(action_file.parent, state)
                        decision = json.loads(attempt_state.read_input(action_file.with_name('review-decision.json')))
                        if hashlib.sha256(decision['action_token'].encode()).hexdigest() != frozen['action_token_sha256']:
                            raise ValueError('request_untrusted')
                        flow_engine.validate_frozen_review_inputs(attempt_root=action_file.parent, state=state,
                                                                 workflow_key=state['workflow_key'])
                        if state['mode'] != 'controlled_write':
                            item.update(status='excluded', reason='preview')
                        elif action.get('write_state') != 'submitted' or state.get('write_state') != 'submitted':
                            item.update(status='excluded', reason='not_submitted')
                        else:
                            submitted = host.sealed_submission(path, request, value)
                            if kind == 'finance':
                                _, model = host._read(Path(submitted['source']['decision_file']))
                                packet = json.loads(attempt_state.read_input(action_file.with_name('review-packet.json')))
                                frozen_action = {**action, 'action_token': decision['action_token']}
                                normalized = review_decision.bind_fields(frozen_action, packet, model)
                                if state['workflow_key'] == 'small_marketing_fee' and packet['rule_version'] == 'small_marketing_fee:2':
                                    normalized = review_decision.normalize_keyed_items(
                                        normalized, tuple(item['item_key'] for item in packet['items']))
                                material = json.loads(attempt_state.read_input(action_file.with_name('material.json')))
                                if (normalized != submitted['decision'] or flow_engine._validate_review_decision(
                                        frozen_action, packet, material, normalized).decision != decision):
                                    raise ValueError('host_review_submission_untrusted')
                            if kind == 'packaging_draft':
                                host._package_completion(request, value['assignments'][request_id])
                            if str(action_file) in seen:
                                item.update(status='duplicate', reason='same_action')
                            else:
                                item.update(status='completed', business_state=receipt['business_state'])
                                workflows[state['workflow_key']] += 1
                                seen.add(str(action_file))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                item.update(status='invalid', reason='completion_unverified')
            counts[item['status']] += 1
            details.append(item)
    return {'schema_version': 1, 'kind': 'preaudit_completed_receipts', 'read_only': True,
            'owner_task_id': owner, 'business_acceptance': 'not_verified', 'counts': dict(counts),
            'workflows': dict(workflows), 'rounds': rounds, 'details': details}


def summary(report: dict) -> dict:
    gaps = Counter(item['reason'] for item in [*report['rounds'], *report['details']]
                   if item['status'] in {'invalid', 'excluded'})
    return {key: report[key] for key in ('schema_version', 'kind', 'read_only', 'business_acceptance', 'counts', 'workflows')} | {
        'selected_rounds': len(report['rounds']), 'gaps': dict(gaps)}
