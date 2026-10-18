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


def admission_blocked_snapshot(root):
    snapshot = live_snapshot()
    action = root / 'runtime/finance_preaudit_flows/general_expense/cases/case/attempt-1/action.json'
    snapshot['maintenance_current_inputs'].update(target_root=str(root), pending_frozen_count=1,
        validated_frozen_count=1, admission_blocked_actions=[str(action)])
    snapshot['reconciliation'].update(
        continuation={'status': 'blocked', 'scope': 'controller', 'isolated_pending': 0,
            'blocking_reasons': {'pending_write_state_unresolved': 1}},
        quarantine={'pending_quarantined': 0},
        findings=[{'category': 'pending_not_handled', 'workflow_key': 'general_expense',
            'item_key': 'case', 'attempt': 1, 'local_execution_state': 'review_required',
            'local_write_state': 'authorized'}])
    return snapshot


def test_source_admission_bridge_is_opt_in_and_preserves_raw_block(tmp_path):
    snapshot = admission_blocked_snapshot(tmp_path)
    original = json.dumps(snapshot, sort_keys=True)
    with pytest.raises(RuntimeError, match='maintenance_reconciliation_untrusted'):
        maintenance_runtime.assert_live_safe(snapshot)
    maintenance_runtime.assert_live_safe(snapshot, allow_admission_blocked=True)
    assert json.dumps(snapshot, sort_keys=True) == original


@pytest.mark.parametrize('invalid', ('extra_block', 'count', 'missing_proof', 'wrong_action',
    'duplicate_proof', 'unknown_write', 'writer', 'status_sha', 'unvalidated', 'stop_mode'))
def test_source_admission_bridge_keeps_other_safety_gates(tmp_path, invalid):
    snapshot = admission_blocked_snapshot(tmp_path)
    inputs = snapshot['maintenance_current_inputs']
    extra = {}
    if invalid == 'extra_block':
        snapshot['reconciliation']['continuation']['blocking_reasons']['identity_conflict'] = 1
    elif invalid == 'count':
        snapshot['reconciliation']['continuation']['blocking_reasons']['pending_write_state_unresolved'] = 2
    elif invalid == 'missing_proof':
        inputs.pop('admission_blocked_actions')
    elif invalid == 'wrong_action':
        inputs['admission_blocked_actions'] = [str(tmp_path / 'another/action.json')]
    elif invalid == 'duplicate_proof':
        inputs['admission_blocked_actions'] *= 2
    elif invalid == 'unknown_write':
        snapshot['reconciliation']['findings'][0]['local_write_state'] = 'unknown'
    elif invalid == 'writer':
        snapshot['controller_handoff']['writer_active'] = True
    elif invalid == 'status_sha':
        inputs['status_sha256'] = 'c' * 64
    elif invalid == 'unvalidated':
        inputs['validated_frozen_count'] = 0
    else:
        extra['source_only_proof'] = {'historical_stopped_actions': []}
    with pytest.raises(RuntimeError, match='maintenance_'):
        maintenance_runtime.assert_live_safe(snapshot, allow_admission_blocked=True, **extra)


@pytest.mark.parametrize('variant', ('old_gate', 'new_gate', 'frozen_drift', 'writer-boundary.json',
    'execution-receipt.json', 'review-decision.json', 'remark.txt', 'submission-envelope.json'))
def test_source_bridge_uses_installed_frozen_validator_without_writing(tmp_path, monkeypatch, variant):
    import sys
    from apps.oa_finance_preaudit import flow_engine, production_config, reconciliation
    from tests.finance_preaudit.test_flow_engine_material_gaps import _engine, _unresolved_evidence_material
    auth_env = tmp_path.parent / (tmp_path.name + '-auth.env')
    auth_env.write_text('')
    auth_env.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(auth_env))
    monkeypatch.delenv('OA_FINANCE_PREAUDIT_WRITE_POLICY', raising=False)
    source = Path(maintenance_runtime.__file__).resolve().parents[2]
    shutil.copytree(source / 'apps', tmp_path / 'apps', ignore=shutil.ignore_patterns('__pycache__'))
    installed = tmp_path / 'apps/oa_finance_preaudit/flow_engine.py'
    if variant != 'new_gate':
        new = '_action, persisted, _packet = reader._open_attempt(action_path, require_reviewable=False)'
        text = installed.read_text()
        assert new in text
        installed.write_text(text.replace(new, '_action, persisted, _packet = reader._open_attempt(action_path)', 1))
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    flows = tmp_path / 'runtime/finance_preaudit_flows'
    submissions = []
    engine = _engine(flows, 'general_expense', RuntimeError('unused'), controlled=True, submissions=submissions)
    engine._collect_material = lambda request, _: _unresolved_evidence_material(request)
    with monkeypatch.context() as old:
        old.setattr(flow_engine, '_require_reviewable_material', lambda *_: None)
        action = engine.next_action('general_expense', mode='controlled_write')
    attempt = Path(action['action_file']).parent
    state = json.loads((attempt / 'state.json').read_text())
    if variant == 'frozen_drift':
        (attempt / 'material.json').write_text('{}')
    elif variant.endswith('.json') or variant == 'remark.txt':
        (attempt / variant).write_text('{}')
    frozen = {p: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
    policy = tmp_path / 'runtime/control/write-policy.json'
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({'schema_version': 1, 'kind': 'preaudit_write_policy',
        'authorized_workflows': [*production_config.FINANCE_WORKFLOWS, 'packaging_layout_approval']}))
    snapshot = {'reconciliation': reconciliation.reconcile(flows, pending_rows=[
        {'request_id': state['request_id'], 'received_at': state['received_at'], 'workflow_key': 'general_expense'}], handled_rows=[])}
    status = tmp_path / 'status.json'
    raw = json.dumps(snapshot).encode()
    status.write_bytes(raw)
    status.chmod(0o600)
    receipt = {'path': str(status), 'sha256': hashlib.sha256(raw).hexdigest()}
    if variant == 'old_gate':
        with pytest.raises(RuntimeError, match='maintenance_frozen_inputs_untrusted'):
            maintenance_runtime.read_current_inputs(tmp_path, receipt)
    if variant in ('old_gate', 'new_gate'):
        result = maintenance_runtime.read_current_inputs(tmp_path, receipt, allow_admission_blocked=True)
        assert result['admission_blocked_actions'] == [str(attempt / 'action.json')]
        assert result['pending_frozen_count'] == result['validated_frozen_count'] == 1
    else:
        with pytest.raises(RuntimeError, match='maintenance_frozen_inputs_untrusted'):
            maintenance_runtime.read_current_inputs(tmp_path, receipt, allow_admission_blocked=True)
    assert {p: p.read_bytes() for p in attempt.iterdir() if p.is_file()} == frozen
    assert submissions == []


@pytest.mark.parametrize('scenario,code', [
    ('valid_preview', None),
    ('valid_collecting', None),
    ('valid_recovered_collecting', None),
    ('valid_recovered_frozen_collecting', None),
    ('valid_recovered_collecting_files', None),
    ('valid_recovered_collecting_review_action', None),
    ('valid_frozen_collecting', None),
    ('valid_packaging_frozen_collecting', None),
    ('valid_collecting_files', None),
    ('valid_collecting_review_action', None),
    ('damaged_collecting_files_packet', 'maintenance_frozen_inputs_untrusted'),
    ('collecting_review_action_identity', 'maintenance_frozen_inputs_untrusted'),
    ('collecting_review_action_provenance', 'maintenance_frozen_inputs_untrusted'),
    ('damaged_frozen_collecting_material', 'maintenance_frozen_inputs_untrusted'),
    ('damaged_frozen_collecting_packet', 'maintenance_frozen_inputs_untrusted'),
    ('frozen_collecting_claim_drift', 'maintenance_frozen_inputs_untrusted'),
    ('frozen_collecting_writer_artifact', 'maintenance_frozen_inputs_untrusted'),
    ('incomplete_frozen_collecting', 'maintenance_frozen_inputs_untrusted'),
    ('collecting_review_action_without_frozen', 'maintenance_frozen_inputs_untrusted'),
    ('collecting_review_action_writer_artifact', 'maintenance_frozen_inputs_untrusted'),
    ('damaged_recovered_frozen_collecting_receipt', 'maintenance_frozen_inputs_untrusted'),
    ('damaged_collecting_identity', 'maintenance_frozen_inputs_untrusted'),
    ('collecting_writer_artifact', 'maintenance_frozen_inputs_untrusted'),
    ('isolated_material_failure', None),
    ('damaged_nonmaterial_isolation', 'maintenance_frozen_inputs_untrusted'),
    ('damaged_material_failure_receipt', 'maintenance_frozen_inputs_untrusted'),
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
    workflow = 'packaging_layout_approval' if 'packaging_frozen' in scenario else 'travel_expense'
    engine = _engine(flows, 'preview-current-inputs')
    recovering = 'recovered_' in scenario
    previous = None
    if recovering:
        from apps.oa_finance_preaudit import flow_engine
        from tests.finance_preaudit.test_flow_engine_submission import _grant, _verify_grant
        engine = _engine(flows, 'preview-current-inputs', authorization_source=_grant,
            authorization_verifier=_verify_grant, submit_frozen=lambda _job: pytest.fail('must not submit'))
        initial = engine.next_action('travel_expense', mode='controlled_write')
        previous = Path(initial['action_file'])
        engine.quarantine_invalid_review(previous)
        engine = flow_engine.FlowEngine(root=flows, discover_candidates=engine._discover_candidates,
            discover_handled=lambda: [], collect_material=engine._collect_material,
            authorization_source=_grant, authorization_verifier=_verify_grant,
            submit_frozen=lambda _job: pytest.fail('must not submit'))
    def proceed(current):
        if recovering:
            return current.recover_confirmed_no_write(previous, mode='controlled_write', confirmed=True)
        return current.next_action(workflow, mode='controlled_write' if current._authorization_source else 'preview')
    if 'collecting' in scenario:
        if 'collecting_files' in scenario or 'collecting_review_action' in scenario:
            from apps import json_artifact
            from tests.finance_preaudit.test_flow_engine_submission import _grant, _verify_grant
            if not recovering:
                engine = _engine(flows, 'preview-current-inputs', authorization_source=_grant,
                    authorization_verifier=_verify_grant, submit_frozen=lambda _job: pytest.fail('must not submit'))
            real_write = json_artifact.write_json_atomic
            def interrupt_state_write(target, payload, **kwargs):
                if Path(target).name == 'state.json' and (
                    ('collecting_files' in scenario and payload.get('execution_state') == 'collecting' and payload.get('material_file'))
                    or ('collecting_review_action' in scenario and payload.get('execution_state') == 'review_required')
                ):
                    if 'collecting_review_action' in scenario:
                        from apps.oa_finance_preaudit import attempt_state
                        reference_path = Path(target).with_name('action.json')
                        raw = reference_path.read_bytes()
                        legacy = attempt_state.view(reference_path, json.loads(raw), raw=raw, state=payload)
                        saved = json.loads(Path(target).read_bytes())
                        saved.pop('action_reference_sha256')
                        real_write(reference_path, legacy)
                        real_write(target, saved)
                    raise KeyboardInterrupt('pause-between-files')
                return real_write(target, payload, **kwargs)
            with monkeypatch.context() as interrupt:
                interrupt.setattr(json_artifact, 'write_json_atomic', interrupt_state_write)
                with pytest.raises(KeyboardInterrupt, match='pause-between-files'):
                    proceed(engine)
        elif 'frozen_collecting' in scenario:
            def interrupted_grant(_context):
                raise KeyboardInterrupt('pause-after-material')
            if recovering:
                engine._authorization_source = interrupted_grant
            else:
                if workflow == 'packaging_layout_approval':
                    from tests.finance_preaudit.test_flow_engine_packaging import _engine as packaging_engine
                    engine = packaging_engine(flows, calls=[])
                    engine._authorization_source = interrupted_grant
                else:
                    engine = _engine(flows, 'preview-current-inputs', authorization_source=interrupted_grant,
                        authorization_verifier=lambda *_args: False, submit_frozen=lambda _job: pytest.fail('must not submit'))
            with pytest.raises(KeyboardInterrupt, match='pause-after-material'):
                proceed(engine)
        else:
            monkeypatch.setattr(engine, '_prepare_attempt', lambda _root: (_ for _ in ()).throw(SystemExit('pause-before-collect')))
            with pytest.raises(SystemExit, match='pause-before-collect'):
                proceed(engine)
        attempt = 2 if recovering else 1
        path = next(flows.glob(f'{workflow}/cases/*/attempt-{attempt}/action.json'))
        if 'writer_artifact' in scenario:
            path.with_name('writer-boundary.json').write_text('{}')
        elif scenario == 'damaged_frozen_collecting_material':
            path.with_name('material.json').write_text('{}')
        elif scenario in {'damaged_frozen_collecting_packet', 'damaged_collecting_files_packet'}:
            path.with_name('review-packet.json').write_text('{}')
        elif scenario == 'damaged_recovered_frozen_collecting_receipt':
            path.with_name('recovery-receipt.json').write_text('{}')
        elif scenario in {'incomplete_frozen_collecting', 'collecting_review_action_without_frozen'}:
            partial = json.loads(path.with_name('state.json').read_text())
            for key in ('material_file', 'review_packet_file', 'material_fingerprint', 'evidence_fingerprint'):
                if scenario == 'collecting_review_action_without_frozen' or key == 'evidence_fingerprint':
                    partial.pop(key, None)
            path.with_name('state.json').write_text(json.dumps(partial))
        elif scenario in {'frozen_collecting_claim_drift', 'collecting_review_action_identity', 'collecting_review_action_provenance'}:
            claim = json.loads(path.read_text())
            if scenario == 'collecting_review_action_identity':
                claim['case_key'] = 'different-case'
            elif scenario == 'collecting_review_action_provenance':
                claim['recovered_from_attempt'] = 42
            else:
                claim['request_id'] = 'different-claim'
            path.write_text(json.dumps(claim))
    else:
        action = engine.next_action('travel_expense')
        path = Path(action['action_file'])
        if 'material_failure' in scenario:
            path.with_name('material.json').write_text('{}')
            engine._persist_attempt_needs_admin(path, 'material_collection_failed',
                failure_details={'failure_detail_code': 'attachment_download_failed'})
            if scenario == 'damaged_material_failure_receipt':
                path.with_name('manual-review-receipt.json').write_text('{}')
    if scenario == 'damaged_nonmaterial_isolation':
        path.with_name('material.json').write_text('{}')
        engine._persist_attempt_needs_admin(path, 'codex_decision_invalid')
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
        pending_rows=[{'request_id': state['request_id'], 'received_at': state['received_at'], 'workflow_key': workflow}],
        handled_rows=[])}
    if scenario == 'damaged_collecting_identity':
        state['request_id'] = 'another-request'
        path.with_name('state.json').write_text(json.dumps(state))
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
        if scenario == 'valid_packaging_frozen_collecting':
            from tests.finance_preaudit.test_flow_engine_packaging import _engine as packaging_engine
            resumed = packaging_engine(flows, calls=[]).next_action(workflow, mode='controlled_write')
            assert resumed['action'] == 'review_required'
            assert resumed['attempt'] == state['attempt']
        if scenario in {'valid_collecting', 'valid_frozen_collecting', 'valid_collecting_files', 'valid_collecting_review_action'} or recovering:
            from tests.finance_preaudit.test_flow_engine_submission import _grant, _verify_grant
            resumed = _engine(flows, state['request_id'], authorization_source=_grant,
                authorization_verifier=_verify_grant, submit_frozen=lambda _job: pytest.fail('must not submit')).next_action(
                    'travel_expense', mode='controlled_write')
            assert resumed['action'] == 'review_required'
            assert resumed['attempt'] == state['attempt']
            if recovering:
                assert resumed['recovered_from_attempt'] == state['recovered_from_attempt'] == 1
                assert resumed['recovery_reason'] == state['recovery_reason'] == 'codex_decision_invalid'


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


def test_stopped_readonly_status_refuses_unreaped_child_even_after_parent_exit(tmp_path):
    import sys
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!' + sys.executable + '\n'
        'import os,sys,time,json,hashlib\nfrom pathlib import Path\n'
        'if os.fork() == 0:\n'
        ' os.close(1); os.close(2); time.sleep(60); os._exit(0)\n'
        'target=Path(sys.argv[sys.argv.index("--output-file")+1])\n'
        'raw=b"{}\\n"; target.write_bytes(raw); target.chmod(0o600)\n'
        'print(json.dumps({"status_file":{"path":str(target),"bytes":len(raw),'
        '"sha256":hashlib.sha256(raw).hexdigest()}}))\n')
    interpreter.chmod(0o700)
    with pytest.raises(RuntimeError, match='maintenance_readonly_children_unreaped'):
        maintenance_runtime.read_live_status(tmp_path, require_reaped=True)


@pytest.mark.parametrize('fault_kind', ['interrupt', 'oserror', 'timeout'])
def test_stopped_readonly_exception_reaps_own_group_before_lease_release(tmp_path, monkeypatch, fault_kind):
    import os
    import signal
    import subprocess
    import sys
    import time
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    ready = tmp_path / 'readonly-ready'
    interpreter.write_text('#!' + sys.executable + '\n'
        'import time\nfrom pathlib import Path\n'
        + 'Path(' + repr(str(ready)) + ').write_text("ready")\n'
        + 'time.sleep(1)\n')
    interpreter.chmod(0o700)
    fault = {'interrupt': KeyboardInterrupt('cancel-readonly'), 'oserror': OSError('read-channel-failed'),
             'timeout': subprocess.TimeoutExpired('readonly', 1)}[fault_kind]
    original_communicate = subprocess.Popen.communicate
    processes = []
    def interrupted_communicate(process, *args, **kwargs):
        if not processes:
            processes.append(process)
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.exists()
            raise fault
        return original_communicate(process, *args, **kwargs)
    monkeypatch.setattr(subprocess.Popen, 'communicate', interrupted_communicate)
    try:
        with maintenance_runtime.target_runtime_leases(tmp_path):
            expected = KeyboardInterrupt if fault_kind == 'interrupt' else RuntimeError
            with pytest.raises(expected) as raised:
                maintenance_runtime.read_live_status(tmp_path, require_reaped=True)
            if fault_kind == 'interrupt':
                assert raised.value is fault
            else:
                assert raised.value.__cause__ is fault
            assert processes[0].poll() is not None
            assert processes[0].returncode == -signal.SIGKILL
            with pytest.raises(ProcessLookupError):
                os.killpg(processes[0].pid, 0)
            assert runtime_control.controller_active(tmp_path / 'runtime/control/rolling-controller.lock')
        assert not runtime_control.controller_active(tmp_path / 'runtime/control/rolling-controller.lock')
    finally:
        if processes:
            try:
                os.killpg(processes[0].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            processes[0].wait(timeout=2)


def test_original_maintenance_readback_uses_owned_terminal_receipt_and_rejects_tampering(tmp_path, monkeypatch):
    import sys
    from uuid import uuid4
    from scripts.finance_preaudit_run import supervise, bind_tool_session
    source = Path(maintenance_runtime.__file__).resolve().parents[2]
    for name in ('apps', 'scripts'):
        shutil.copytree(source / name, tmp_path / name, ignore=shutil.ignore_patterns('__pycache__'))
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    directory = tmp_path / 'runtime/control/executions' / uuid4().hex
    owner = str(uuid4())
    child = '''import os,json,datetime
from pathlib import Path
Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']).write_text(json.dumps({
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'failed',
 'children_reaped':True,'code_revision':'c'*40,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
print('offline fake only')
raise SystemExit(1)'''
    receipt = supervise([sys.executable, '-c', child], directory=directory, owner_task_id=owner)
    original = maintenance_runtime.OriginalExecution(Path(receipt['receipt_file']), owner, 'original-session')
    with monkeypatch.context() as binding_environment:
        binding_environment.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'runtime/control/rolling-controller.lock'))
        bind_tool_session(original.file, owner_task_id=owner, session_id=original.tool_session_id)
    mirror = original.file.with_name('controller-manifest.json').read_bytes()
    report = maintenance_runtime.read_original_execution(tmp_path, original)
    maintenance_runtime.assert_original_execution_safe(tmp_path, original, report, mirror)
    assert report['terminal'] is True
    assert report['exit_code'] == 1
    assert 'controller_stdout' not in report
    original.file.with_name('stdout.log').write_text('tampered')
    with pytest.raises(RuntimeError, match='maintenance_original_execution_untrusted'):
        maintenance_runtime.read_original_execution(tmp_path, original)


@pytest.mark.parametrize('section', ['live_check', 'controller_handoff', 'reconciliation'])
def test_live_safety_rejects_incomplete_sections_with_stable_code(section):
    snapshot = live_snapshot()
    snapshot[section] = None
    with pytest.raises(RuntimeError, match='maintenance_live_evidence_invalid'):
        maintenance_runtime.assert_live_safe(snapshot)
