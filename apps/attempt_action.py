from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat

REVIEW_BINDING = "review_decision_binding"
REFERENCE_KIND = 'preaudit_attempt_reference'
IDENTITY_FIELDS = ('workflow_key', 'case_key', 'attempt', 'request_id', 'received_at', 'action_file', 'state_file')
_STATE_FIELDS = frozenset({
    'request_id', 'received_at', 'created_at', 'discovery_seconds', 'failed_at',
    'material_file', 'material_fingerprint', 'evidence_fingerprint',
    'authorization_grant', 'submission_token', 'decision_fingerprint',
    'remark_fingerprint', 'envelope_fingerprint', 'receipt_fingerprint',
    'submission_token_sha256', 'company_comparison_contract_version',
    'action_reference_sha256', 'recovery_receipt_fingerprint', REVIEW_BINDING,
})


@contextmanager
def _input_stream(path: Path, *, private: bool = False):
    path = Path(path).expanduser().absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("diagnostic_input_unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("diagnostic_input_unsafe")
            if (metadata.st_mode & 0o022) or (private and stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError("diagnostic_input_permissions_invalid")
            yield stream
    except FileNotFoundError as error:
        raise ValueError("diagnostic_input_missing") from error
    except OSError as error:
        raise ValueError("diagnostic_input_unreadable") from error


def read_input(path: Path, *, private: bool = False) -> bytes:
    with _input_stream(path, private=private) as stream:
        if os.fstat(stream.fileno()).st_size > 16 * 1024 * 1024:
            raise ValueError("diagnostic_input_too_large")
        return stream.read()


def input_sha256(path: Path) -> str:
    with _input_stream(path) as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def legacy_contract(state: dict) -> bool:
    if 'action_reference_sha256' not in state:
        return True
    reference = json.loads(read_input(Path(state['action_file'])))
    return isinstance(reference, dict) and reference.get('kind') == 'preaudit_action'


def view(path: Path, payload: dict, *, raw: bytes | None = None, state: dict | None = None) -> dict:
    path = Path(path)
    state_path = path.with_name('state.json')
    if path.name != 'action.json':
        return payload
    if state is None:
        if not state_path.exists() and payload.get('kind') != REFERENCE_KIND:
            return payload
        state = json.loads(read_input(state_path))
    if payload.get('kind') != REFERENCE_KIND and (not isinstance(state, dict) or 'action_reference_sha256' not in state):
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
    fields = IDENTITY_FIELDS if payload.get('kind') == REFERENCE_KIND else tuple(key for key in IDENTITY_FIELDS if key in payload)
    if (not isinstance(state, dict)
            or payload.get('kind') == REFERENCE_KIND and set(payload) != {'schema_version', 'kind', *IDENTITY_FIELDS}
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
        excluded = (excluded | {'rule_version'}) - {'recovery_receipt_fingerprint'}
    return {key: value for key, value in state.items() if key not in excluded}


def read_action(path: Path, *, state: dict | None = None) -> dict:
    path = Path(path)
    raw = read_input(path)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('persistent_state_invalid')
    return view(path, payload, raw=raw, state=state)
