from __future__ import annotations

import hashlib
import json
from pathlib import Path

from apps import json_artifact
from . import submission


_KIND = 'preaudit_attempt_reference'
_IDENTITY = ('workflow_key', 'case_key', 'attempt', 'request_id', 'received_at', 'action_file', 'state_file')
_STATE_FIELDS = frozenset({
    'request_id', 'received_at', 'created_at', 'discovery_seconds', 'failed_at',
    'material_file', 'material_fingerprint', 'evidence_fingerprint',
    'authorization_grant', 'submission_token', 'decision_fingerprint',
    'remark_fingerprint', 'envelope_fingerprint', 'receipt_fingerprint',
    'submission_token_sha256', 'company_comparison_contract_version',
    'action_reference_sha256', 'recovery_receipt_fingerprint', submission.REVIEW_BINDING,
})


def binding(state: dict) -> dict:
    return ({'action_reference_sha256': state['action_reference_sha256']}
            if 'action_reference_sha256' in state else {})


def legacy_contract(state: dict) -> bool:
    if 'action_reference_sha256' not in state:
        return True
    from .review_context import read_input
    reference = json.loads(read_input(Path(state['action_file'])))
    return isinstance(reference, dict) and reference.get('kind') == 'preaudit_action'


def create(action_file: Path, state: dict) -> dict:
    path = Path(action_file)
    reference = {'schema_version': 1, 'kind': _KIND,
                 **{key: state[key] for key in _IDENTITY}}
    if path.exists() or path.is_symlink():
        raise ValueError('persistent_state_invalid')
    json_artifact.write_json_atomic(path, reference, indent=2)
    return {**state, 'action_reference_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def write(path: Path, state: dict, *, indent: int | None = None) -> Path:
    path = Path(path)
    action_file = path.with_name('action.json')
    from .review_context import read_input
    raw = read_input(action_file)
    reference = json.loads(raw)
    if not isinstance(reference, dict) or reference.get('kind') not in {_KIND, 'preaudit_action'}:
        raise ValueError('persistent_state_invalid')
    digest = hashlib.sha256(raw).hexdigest()
    previous = json.loads(read_input(path)) if path.exists() else {}
    if (not isinstance(previous, dict)
            or any(value is not None and value != digest for value in (
                state.get('action_reference_sha256'), previous.get('action_reference_sha256')))
            or reference.get('kind') == _KIND and path.exists() and 'action_reference_sha256' not in previous):
        raise ValueError('persistent_state_invalid')
    if (action_file.is_symlink() or path.is_symlink()
            or state.get('action_reference_sha256', digest) != digest):
        raise ValueError('persistent_state_invalid')
    if previous:
        view(action_file, reference, raw=raw, state=previous)
    fields = _IDENTITY if reference.get('kind') == _KIND else tuple(key for key in _IDENTITY if key in reference)
    if any(reference.get(key) != state.get(key) for key in fields):
        raise ValueError('persistent_state_invalid')
    return json_artifact.write_json_atomic(path, {**state, 'action_reference_sha256': digest}, indent=indent)


def view(path: Path, payload: dict, *, raw: bytes | None = None, state: dict | None = None) -> dict:
    path = Path(path)
    state_path = path.with_name('state.json')
    if path.name != 'action.json':
        return payload
    if state is None:
        if not state_path.exists() and payload.get('kind') != _KIND:
            return payload
        from .review_context import read_input
        state = json.loads(read_input(state_path))
    if payload.get('kind') != _KIND and (not isinstance(state, dict) or 'action_reference_sha256' not in state):
        if (not isinstance(state, dict) or payload.get('kind') != 'preaudit_action'
                or payload.get('schema_version') != 1):
            raise ValueError('persistent_state_invalid')
        shared = ('workflow_key', 'case_key', 'attempt', 'action_file', 'state_file')
        if any(payload.get(key) != state.get(key) for key in shared):
            raise ValueError('persistent_state_invalid')
        phase = (state.get('execution_state'), payload.get('execution_state'))
        if phase not in {('collecting', 'review_required'), ('review_required', 'closed')}:
            required = ('action', 'execution_state', 'business_state', 'mode', 'write_authorized')
            if state.get('execution_state') == 'review_required':
                required += ('action_token', 'authorization_fingerprint', 'review_packet_file')
            if any(payload.get(key) != state.get(key) for key in required):
                raise ValueError('persistent_state_invalid')
            shared = ('authorization_fingerprint', 'action_token', 'review_packet_file', 'reason',
                      'failure_detail_code')
            if any(payload.get(key) != state.get(key) for key in shared if key in payload):
                raise ValueError('persistent_state_invalid')
            if any(payload.get(key) != state.get(key) for key in (
                    'review_failure_fingerprint', 'review_interruption_fingerprint', 'preparation_timeout_proof')):
                raise ValueError('persistent_state_invalid')
            if (state.get('write_state') != 'submitting'
                    and payload.get('write_state') != state.get('write_state')):
                raise ValueError('persistent_state_invalid')
        return payload
    if any(part.is_symlink() for part in (path, state_path, *path.parents)):
        raise ValueError('persistent_state_invalid')
    fields = _IDENTITY if payload.get('kind') == _KIND else tuple(key for key in _IDENTITY if key in payload)
    if (not isinstance(state, dict)
            or payload.get('kind') == _KIND and set(payload) != {'schema_version', 'kind', *_IDENTITY}
            or payload.get('schema_version') != 1
            or state.get('schema_version') != 1 or state.get('kind') != 'preaudit_action'
            or payload.get('action_file') != str(path.absolute())
            or payload.get('state_file') != str(state_path.absolute())
            or any(state.get(key) != payload.get(key) for key in fields)
            or state.get('action_reference_sha256') != hashlib.sha256(raw if raw is not None else path.read_bytes()).hexdigest()):
        raise ValueError('persistent_state_invalid')
    if state.get('execution_state') == 'needs_admin':
        fields = {'schema_version', 'kind', 'action', 'execution_state', 'business_state', 'write_state',
                  'coordination_seconds', 'coordination_tokens', 'workflow_key', 'mode', 'write_authorized',
                  'reason', 'action_file', 'state_file', 'case_key', 'attempt', 'failure_detail_code',
                  'failure_counts', 'failure_diagnostic', 'review_failure_fingerprint',
                  'review_interruption_fingerprint', 'preparation_timeout_proof'}
        return {key: value for key, value in state.items() if key in fields}
    excluded = {'action_reference_sha256'} if state.get('execution_state') == 'collecting' else _STATE_FIELDS
    if state.get('execution_state') == 'closed':
        excluded = excluded | {'rule_version'}
    return {key: value for key, value in state.items() if key not in excluded}


def read_action(path: Path, *, state: dict | None = None) -> dict:
    path = Path(path)
    from .review_context import read_input
    raw = read_input(path)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('persistent_state_invalid')
    return view(path, payload, raw=raw, state=state)
