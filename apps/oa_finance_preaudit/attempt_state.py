from __future__ import annotations

import hashlib
import json
from pathlib import Path

from apps import json_artifact
from apps.attempt_action import (
    IDENTITY_FIELDS as _IDENTITY,
    REFERENCE_KIND as _KIND,
    legacy_contract as legacy_contract,
    read_action as read_action,
    read_input,
    view,
)


def binding(state: dict) -> dict:
    return ({'action_reference_sha256': state['action_reference_sha256']}
            if 'action_reference_sha256' in state else {})


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
