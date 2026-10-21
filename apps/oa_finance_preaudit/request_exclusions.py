from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Sequence

from . import attempt_state, flow_engine, reconciliation


_MAIN_FILES = ('action.json', 'state.json', 'material.json', 'review-packet.json',
               'review-decision.json', 'remark.txt', 'submission-envelope.json', 'execution-receipt.json')
_CLAIM_FORBIDDEN = ('review-decision.json', 'remark.txt', 'submission-envelope.json',
                    'execution-receipt.json', 'writer-boundary.json')
_ERROR = 'request_exclusion_untrusted'


def _pin(path: Path, root: Path) -> dict:
    if not flow_engine._managed_existing_path(path, root):
        raise ValueError(_ERROR)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(_ERROR)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat(follow_symlinks=False)
    fields = ('st_dev', 'st_ino', 'st_mtime_ns', 'st_size')
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError(_ERROR)
    return {'path': str(path), 'sha256': digest, 'device': before.st_dev, 'inode': before.st_ino,
            'mtime_ns': before.st_mtime_ns, 'size': before.st_size}


@dataclass(frozen=True)
class _Request:
    workflow_key: str
    request_id: str
    item_key: str
    attempt: int
    pins: tuple[dict, ...]
    execution_state: str
    write_state: str
    write_authorized: bool


@dataclass(frozen=True)
class Scope:
    project_root: Path
    issue: int
    _requests: tuple[_Request, ...]

    @classmethod
    def bind(cls, project_root: Path, action_files: Sequence[Path], *, issue: int) -> Scope:
        if not action_files or type(issue) is not int or issue <= 0:
            raise ValueError(_ERROR)
        project_root = Path(project_root)
        if (not project_root.is_absolute() or project_root != project_root.resolve()
                or any(path.is_symlink() for path in project_root.parents)):
            raise ValueError(_ERROR)
        root = project_root / 'runtime/finance_preaudit_flows'
        reader = flow_engine.FlowEngine(root=root, discover_candidates=lambda _: (), collect_material=lambda *_: {})
        requests = []
        seen = set()
        try:
            for raw in action_files:
                path = Path(raw)
                if (not path.is_absolute() or path != path.resolve() or not flow_engine._is_attempt_action_path(path, root)
                        or path.name != 'action.json'):
                    raise ValueError(_ERROR)
                claim_pins = tuple(_pin(path.with_name(name), root) for name in ('action.json', 'state.json'))
                state = flow_engine._read_object(path.with_name('state.json'), code=_ERROR)
                action = attempt_state.read_action(path)
                collecting = state.get('execution_state') == 'collecting'
                if (not reader._attempt_identity_matches(path.parent, state, require_material=not collecting)
                        or not action or any(state.get(key) != value for key, value in action.items())
                        or state['request_id'] in seen):
                    raise ValueError(_ERROR)
                if collecting:
                    if (state.get('action') != 'collecting' or state.get('write_state') != 'not_requested'
                            or state.get('write_authorized') is not False
                            or any(os.path.lexists(path.with_name(name)) for name in _CLAIM_FORBIDDEN)):
                        raise ValueError(_ERROR)
                    recovery = path.with_name('recovery-receipt.json')
                    pins = claim_pins + ((_pin(recovery, root),) if os.path.lexists(recovery) else ())
                else:
                    pins = claim_pins + tuple(_pin(path.with_name(name), root) for name in _MAIN_FILES[2:])
                    if any(state.get(key) != value for key, value in {
                            'action': 'closed', 'execution_state': 'closed', 'write_state': 'submitted',
                            'mode': 'controlled_write', 'write_authorized': True}.items()):
                        raise ValueError(_ERROR)
                    reader._completion_receipt(path.parent, state)
                    material = flow_engine._read_object(path.with_name('material.json'), code=_ERROR)
                    packet = flow_engine._read_object(path.with_name('review-packet.json'), code=_ERROR)
                    decision = flow_engine._read_object(path.with_name('review-decision.json'), code=_ERROR)
                    if (state['material_fingerprint'] != 'sha256:' + pins[2]['sha256']
                            or material.get('request_id') != state['request_id']
                            or material.get('workflow_key') != state['workflow_key']
                            or packet.get('workflow_key') != state['workflow_key']
                            or packet.get('material_fingerprint') != state['material_fingerprint']
                            or packet.get('rule_version') != decision.get('rule_version')):
                        raise ValueError(_ERROR)
                seen.add(state['request_id'])
                requests.append(_Request(state['workflow_key'], state['request_id'], state['case_key'],
                                         state['attempt'], pins, state['execution_state'], state['write_state'],
                                         state['write_authorized']))
            scope = cls(project_root, issue, tuple(requests))
            scope.verify()
            return scope
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(_ERROR) from error

    def _states(self) -> list[dict]:
        states = reconciliation.latest_states(self.project_root / 'runtime/finance_preaudit_flows')
        selected = {item.request_id: item for item in self._requests}
        for state in states:
            item = selected.get(state.get('request_id'))
            if item is not None and (state.get('workflow_key') != item.workflow_key
                                     or state.get('write_state') in {'unknown', 'submitting'}):
                raise ValueError(_ERROR)
        for item in self._requests:
            if item.execution_state == 'collecting':
                matched = [state for state in states if state['case_key'] == item.item_key
                           and state['workflow_key'] == item.workflow_key]
                if len(matched) != 1 or matched[0]['attempt'] != item.attempt:
                    raise ValueError(_ERROR)
        return states

    def _verify_pins(self) -> None:
        root = self.project_root / 'runtime/finance_preaudit_flows'
        try:
            for item in self._requests:
                if item.execution_state == 'collecting':
                    action = Path(item.pins[0]['path'])
                    recovery_pinned = any(Path(pin['path']).name == 'recovery-receipt.json' for pin in item.pins)
                    if (any(os.path.lexists(action.with_name(name)) for name in _CLAIM_FORBIDDEN)
                            or os.path.lexists(action.with_name('recovery-receipt.json')) != recovery_pinned
                            or max(number for number, _ in reconciliation._attempt_roots(action.parent.parent)) != item.attempt):
                        raise ValueError(_ERROR)
                for pin in item.pins:
                    if _pin(Path(pin['path']), root) != pin:
                        raise ValueError(_ERROR)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise ValueError(_ERROR) from error

    def verify(self) -> None:
        self._verify_pins()
        self._states()

    def filter_candidates(self, rows: list[dict]) -> list[dict]:
        self.verify()
        selected = {item.request_id: item.workflow_key for item in self._requests}
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(_ERROR)
            request_id = str(row.get('request_id') or row.get('requestId') or '').strip()
            if row.get('request_id') and row.get('requestId') and row['request_id'] != row['requestId']:
                raise ValueError(_ERROR)
            if request_id in selected:
                workflow = row.get('workflow_key') or row.get('flow_key')
                if (workflow != selected[request_id] or row.get('workflow_key') and row.get('flow_key')
                        and row['workflow_key'] != row['flow_key']):
                    raise ValueError(_ERROR)
            else:
                kept.append(row)
        return kept

    def receipt(self) -> dict:
        return {'schema_version': 1, 'kind': 'preaudit_request_exclusions', 'issue': self.issue,
                'historical_submission_verified': False, 'items': [
                    {'workflow_key': item.workflow_key, 'request_sha256': hashlib.sha256(item.request_id.encode()).hexdigest(),
                     'source_item_key': item.item_key, 'source_attempt': item.attempt,
                     'source_execution_state': item.execution_state, 'source_write_state': item.write_state,
                     'source_write_authorized': item.write_authorized,
                     'action_file': item.pins[0]['path'], 'pins': deepcopy(list(item.pins))}
                    for item in self._requests]}

    def project(self, raw_reconciliation: dict) -> dict:
        self._verify_pins()
        if (raw_reconciliation.get('schema_version') != 1
                or raw_reconciliation.get('kind') != 'preaudit_reconciliation'
                or raw_reconciliation.get('read_only') is not True
                or not isinstance(raw_reconciliation.get('findings'), list)
                or 'execution_exclusions' in raw_reconciliation):
            raise ValueError(_ERROR)
        result = deepcopy(raw_reconciliation)
        try:
            continuation = result['continuation']
            reasons = continuation['blocking_reasons']
            if not isinstance(reasons, dict) or any(type(value) is not int or value < 0 for value in reasons.values()):
                raise ValueError(_ERROR)
            if (continuation.get('status') != ('blocked' if reasons else 'ready')
                    or type(continuation.get('isolated_pending')) is not int
                    or continuation['isolated_pending'] < 0
                    or continuation['isolated_pending'] != result['quarantine']['pending_quarantined']):
                raise ValueError(_ERROR)
            states = {(state['workflow_key'], state['case_key']): state for state in self._states()}
            selected = {(item.workflow_key, item.item_key, item.attempt): item for item in self._requests}
            request_ids = {item.request_id for item in self._requests}
            removed = []
            active = set()
            conflicts = 0
            for row in result['findings']:
                state = states.get((row.get('workflow_key'), row.get('item_key')))
                if state is not None and state.get('request_id') in request_ids:
                    if row.get('local_write_state') in {'unknown', 'submitting'}:
                        raise ValueError(_ERROR)
                identity = (row.get('workflow_key'), row.get('item_key'), row.get('attempt'))
                item = selected.get(identity)
                if item is not None and item.execution_state == 'collecting' and row.get('category') == 'pending_not_handled':
                    if (row.get('local_execution_state') != 'collecting'
                            or row.get('local_write_state') != 'not_requested' or identity in removed):
                        raise ValueError(_ERROR)
                    removed.append(identity)
                    active.add(item.request_id)
                if item is not None and item.execution_state == 'closed' and row.get('category') == 'identity_conflict':
                    if (row.get('local_execution_state') != 'closed' or row.get('local_write_state') != 'submitted'
                            or row.get('recommendation') == 'deferred_historical_cleanup'
                            or identity in removed):
                        raise ValueError(_ERROR)
                    removed.append(identity)
                    active.add(state['request_id'])
                    conflicts += 1
            count = len(removed)
            if reasons.get('identity_conflict', 0) < conflicts:
                raise ValueError(_ERROR)
            if conflicts:
                reasons['identity_conflict'] -= conflicts
                if not reasons['identity_conflict']:
                    del reasons['identity_conflict']
            if count:
                retained = []
                for row in result['findings']:
                    if (row.get('workflow_key'), row.get('item_key'), row.get('attempt')) in removed:
                        for field, key in (('counts', row['category']), ('recommendations', row['recommendation'])):
                            if result[field].get(key, 0) < 1:
                                raise ValueError(_ERROR)
                            result[field][key] -= 1
                            if not result[field][key]:
                                del result[field][key]
                    else:
                        retained.append(row)
                result['findings'] = retained
                result['local_cases'] -= count
            dispositions = result['pending_dispositions']
            pending_items = dispositions['items']
            if (dispositions.get('read_only') is not True or not isinstance(pending_items, list)
                    or type(dispositions.get('total')) is not int or dispositions['total'] != len(pending_items)):
                raise ValueError(_ERROR)
            pending_identities = set()
            matched_requests = set()
            for row in pending_items:
                if (not isinstance(row, dict) or not isinstance(row.get('received_at'), str)
                        or not row['received_at'] or not isinstance(row.get('item_key'), str)
                        or not isinstance(row.get('workflow_key'), str)):
                    raise ValueError(_ERROR)
                for item in self._requests:
                    if row['item_key'] != reconciliation._item_key(item.request_id, row['received_at']):
                        continue
                    if row['workflow_key'] != item.workflow_key or row.get('write_state') in {'unknown', 'submitting'}:
                        raise ValueError(_ERROR)
                    if item.request_id in active:
                        pending_identities.add((item.workflow_key, row['item_key']))
                        matched_requests.add(item.request_id)
            if matched_requests != active:
                raise ValueError(_ERROR)
            excluded = len(pending_identities)
            quarantine = result['quarantine']
            if type(quarantine['actionable_pending']) is not int or quarantine['actionable_pending'] < excluded:
                raise ValueError(_ERROR)
            quarantine['actionable_pending'] -= excluded
            continuation.update(status='blocked' if reasons else 'ready', scope='controller' if reasons else (
                'item' if continuation['isolated_pending'] else 'none'))
            result.update(scope_excluded_pending=excluded, execution_exclusions={**self.receipt(),
                'raw_continuation': deepcopy(raw_reconciliation['continuation'])})
            return result
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(_ERROR) from error
