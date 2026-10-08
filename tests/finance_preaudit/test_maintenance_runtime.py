from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import maintenance_runtime


@pytest.fixture(autouse=True)
def clean_control_environment(monkeypatch):
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
                'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT'):
        monkeypatch.delenv(key, raising=False)


def live_snapshot():
    return {
        'maintenance_status_file': {'sha256': 'a' * 64},
        'maintenance_current_inputs': {'kind': 'preaudit_maintenance_current_inputs', 'status': 'verified',
            'read_only': True, 'write_state': 'not_requested', 'status_sha256': 'a' * 64,
            'policy_sha256': 'b' * 64, 'authorized_workflows': ['travel_expense', 'business_entertainment',
            'business_support_fee', 'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense', 'packaging_layout_approval'],
            'pending_frozen_count': 0, 'validated_frozen_count': 0},
        'live_check': {'credentials': {'status': 'ok'},
                       'oa': {'authentication': 'ok', 'execution_identity': 'verified'},
                       'web_session': {'status': 'ok', 'authentication': 'ok', 'source': 'rpa_page'},
                       'write_state': 'not_requested'},
        'controller_handoff': {'manifest_state': 'failed', 'children_reaped': True,
                               'restart_mode': 'fresh_process', 'restart_verified': True,
                               'controller_active': True, 'writer_active': False,
                               'active_request_writers': 1},
        'reconciliation': {'kind': 'preaudit_reconciliation', 'read_only': True,
                           'continuation': {'status': 'ready', 'scope': 'item',
                                            'isolated_pending': 1, 'blocking_reasons': {}},
                           'quarantine': {'pending_quarantined': 1}, 'findings': []},
    }


def test_maintenance_owns_target_leases_until_context_exits(tmp_path):
    with maintenance_runtime.target_runtime_leases(tmp_path):
        writer = tmp_path / 'runtime/control/frozen-writer.lock'
        controller = tmp_path / 'runtime/control/rolling-controller.lock'
        assert runtime_control.controller_active(controller)
        assert runtime_control.active_request_writers(writer) == 1
        with pytest.raises(BlockingIOError):
            with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY, writer, blocking=False):
                pass
    assert not runtime_control.controller_active(controller)
    assert runtime_control.active_request_writers(writer) == 0


def test_safe_live_snapshot_allows_trusted_item_quarantine():
    maintenance_runtime.assert_live_safe(live_snapshot())


def test_current_gate_requires_real_readonly_authorization_and_frozen_proof():
    snapshot = live_snapshot()
    snapshot.pop('maintenance_current_inputs')
    with pytest.raises(RuntimeError, match='maintenance_current_inputs_untrusted'):
        maintenance_runtime.assert_live_safe(snapshot)


@pytest.mark.parametrize('scenario,code', [
    ('valid_preview', None),
    ('missing_policy', 'maintenance_authorization_untrusted'),
    ('missing_packaging_authorization', 'maintenance_authorization_untrusted'),
    ('unsafe_policy', 'maintenance_authorization_untrusted'),
    ('crossroot_policy', 'maintenance_authorization_untrusted'),
    ('damaged_preview_material', 'maintenance_frozen_inputs_untrusted'),
])
def test_target_owned_current_validators_check_policy_and_pending_frozen_preview(tmp_path, monkeypatch, scenario, code):
    from apps.oa_finance_preaudit import production_config, reconciliation
    from tests.finance_preaudit.test_flow_engine_submission import _engine
    source_root = Path(maintenance_runtime.__file__).resolve().parents[2]
    isolated_auth = tmp_path.parent / (tmp_path.name + '-auth.env')
    isolated_auth.write_text('')
    isolated_auth.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(isolated_auth))
    shutil.copytree(source_root / 'apps', tmp_path / 'apps', ignore=shutil.ignore_patterns('__pycache__'))
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    import sys
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    flows = tmp_path / 'runtime/finance_preaudit_flows'
    action = _engine(flows, 'preview-current-inputs').next_action('travel_expense')
    path = Path(action['action_file'])
    state = json.loads(path.with_name('state.json').read_text())
    if scenario == 'damaged_preview_material':
        path.with_name('material.json').write_text('{}')
    if scenario != 'missing_policy':
        policy = tmp_path / 'runtime/control/write-policy.json'
        policy.parent.mkdir(parents=True)
        authorized = [*production_config.FINANCE_WORKFLOWS]
        if scenario != 'missing_packaging_authorization':
            authorized.append('packaging_layout_approval')
        policy.write_text(json.dumps({'schema_version': 1, 'kind': 'preaudit_write_policy', 'authorized_workflows': authorized}))
        if scenario == 'unsafe_policy':
            policy.chmod(0o666)
    if scenario == 'crossroot_policy':
        configured = tmp_path / '.env'
        configured.write_text('OA_FINANCE_PREAUDIT_WRITE_POLICY=' + str(tmp_path.parent / 'other-policy.json') + '\n')
        configured.chmod(0o600)
    snapshot = {'reconciliation': reconciliation.reconcile(flows,
        pending_rows=[{'request_id': state['request_id'], 'received_at': state['received_at'], 'workflow_key': 'travel_expense'}],
        handled_rows=[])}
    status_file = tmp_path / 'status.json'
    raw = json.dumps(snapshot).encode()
    status_file.write_bytes(raw)
    status_file.chmod(0o600)
    receipt = {'path': str(status_file), 'sha256': hashlib.sha256(raw).hexdigest()}
    if code:
        with pytest.raises(RuntimeError, match=code):
            maintenance_runtime.read_current_inputs(tmp_path, receipt)
    else:
        result = maintenance_runtime.read_current_inputs(tmp_path, receipt)
        assert result['status'] == 'verified'
        assert result['pending_frozen_count'] == result['validated_frozen_count'] == 1
        assert result['status_sha256'] == receipt['sha256']


@pytest.mark.parametrize('section,key,value,code', [
    ('credentials', 'status', 'missing', 'maintenance_live_identity_untrusted'),
    ('oa', 'execution_identity', 'mismatch', 'maintenance_live_identity_untrusted'),
    ('oa', 'authentication', 'failed', 'maintenance_live_identity_untrusted'),
])
def test_live_safety_rejects_missing_identity_or_auth(section, key, value, code):
    snapshot = live_snapshot()
    snapshot['live_check'][section][key] = value
    with pytest.raises(RuntimeError, match=code):
        maintenance_runtime.assert_live_safe(snapshot)


@pytest.mark.parametrize('state', ['unknown', 'submitting', 'submitted'])
def test_live_safety_rejects_current_unresolved_writes_even_with_claimed_ready(state):
    snapshot = live_snapshot()
    snapshot['reconciliation']['findings'] = [{'category': 'pending_not_handled', 'local_write_state': state}]
    with pytest.raises(RuntimeError, match='maintenance_current_write_unresolved'):
        maintenance_runtime.assert_live_safe(snapshot)


def test_request_writer_conflict_prevents_maintenance(tmp_path):
    writer = tmp_path / 'runtime/control/frozen-writer.lock'
    with runtime_control.request_writer_lease('request-in-flight', writer):
        with pytest.raises(RuntimeError, match='maintenance_writer_active'):
            with maintenance_runtime.target_runtime_leases(tmp_path):
                pytest.fail('must not enter maintenance')


def test_target_override_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FROZEN_WRITER_LOCK_FILE', '/wrong/root/writer.lock')
    with pytest.raises(RuntimeError, match='maintenance_runtime_override'):
        with maintenance_runtime.target_runtime_leases(tmp_path):
            pass


@pytest.mark.parametrize('mode,code', [('hash', 'maintenance_live_receipt_invalid'),
                                     ('exit', 'maintenance_live_status_failed'), ('valid', 'maintenance_current_inputs_untrusted')])
def test_read_live_status_requires_terminal_success_and_exact_receipt(tmp_path, mode, code):
    import sys
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!' + sys.executable + '\n'
        'import sys,json,hashlib\n'
        'from pathlib import Path\n'
        'target=Path(sys.argv[sys.argv.index("--output-file")+1])\n'
        'raw=b"{\\\"schema_version\\\":1}\\n"\n'
        'target.write_bytes(raw)\n'
        'target.chmod(0o600)\n'
        'print(json.dumps({"status_file":{"path":str(target),"bytes":len(raw),'
        '"sha256":' + ('"wrong"' if mode == 'hash' else 'hashlib.sha256(raw).hexdigest()') + '}}))\n'
        'raise SystemExit(' + ('2' if mode == 'exit' else '0') + ')\n')
    interpreter.chmod(0o700)
    if code is None:
        payload = maintenance_runtime.read_live_status(tmp_path)
        assert payload['schema_version'] == 1
        assert payload['maintenance_status_file']['bytes'] == len(b'{"schema_version":1}\n')
    else:
        with pytest.raises(RuntimeError, match=code):
            maintenance_runtime.read_live_status(tmp_path)


@pytest.mark.parametrize('section', ['live_check', 'controller_handoff', 'reconciliation'])
def test_live_safety_rejects_incomplete_sections_with_stable_code(section):
    snapshot = live_snapshot()
    snapshot[section] = None
    with pytest.raises(RuntimeError, match='maintenance_live_evidence_invalid'):
        maintenance_runtime.assert_live_safe(snapshot)
