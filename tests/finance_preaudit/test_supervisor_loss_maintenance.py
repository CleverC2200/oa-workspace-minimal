from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import maintenance_runtime, production_config, production_release, reconciliation
from scripts.finance_preaudit_run import bind_tool_session, supervise
from tests.finance_preaudit.test_flow_engine_submission import _engine
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment, live_snapshot
from tests.finance_preaudit.test_production_release import git


@pytest.fixture
def lost_execution(tmp_path, monkeypatch):
    tmp_path = tmp_path / 'oa-workspace-minimal'
    tmp_path.mkdir()
    source = Path(maintenance_runtime.__file__).resolve().parents[2]
    for name in ('apps', 'scripts'):
        shutil.copytree(source / name, tmp_path / name, ignore=shutil.ignore_patterns('__pycache__'))
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    auth = tmp_path.parent / (tmp_path.name + '-offline.env')
    auth.write_text('')
    auth.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(auth))
    flows = tmp_path / 'runtime/finance_preaudit_flows'
    engine = _engine(flows, 'offline-cancelled-collection')
    engine._collect_material = lambda *_: (_ for _ in ()).throw(ValueError('oa_run_stopped'))
    action = engine.next_action('travel_expense')
    action_file = Path(action['action_file'])
    directory = tmp_path / 'runtime/control/executions' / uuid4().hex
    owner = str(uuid4())
    child = '''import os,json,datetime,sys
from pathlib import Path
os.chdir(sys.argv[2])
from apps.oa_finance_preaudit import approval_lifecycle
root = Path.cwd()
manifest = {'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'schema_version':1,'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'failed',
 'children_reaped':True,'code_revision':'c'*40,
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
with approval_lifecycle.run_scope(manifest, root):
 approval_lifecycle.heartbeat(manifest['run_id'],manifest['controller_instance_id'],
  {Path(__import__('sys').argv[1])},[],writer_active=False)
for path in (Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']),
 root/'runtime/control/rolling-controller-manifest.json'):
 path.write_text(json.dumps(manifest))
 path.chmod(0o600)
print('offline fake only')
raise SystemExit(1)'''
    # Both processes are real offline children. Only the fixture simulates the
    # historical waiting receipt whose OS wait result was lost with its caller.
    with monkeypatch.context() as child_environment:
        child_environment.chdir(tmp_path)
        child_environment.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
            str(tmp_path / 'runtime/control/rolling-controller.lock'))
        receipt = supervise([sys.executable, '-B', '-c', child, str(action_file), str(tmp_path)],
            directory=directory, owner_task_id=owner)
        original = maintenance_runtime.OriginalExecution(Path(receipt['receipt_file']), owner, 'original-session')
        bind_tool_session(original.file, owner_task_id=owner, session_id=original.tool_session_id)
    saved = json.loads(original.file.read_bytes())
    saved.update(state='waiting', supervisor_pid=99999999, exit_code=None, exit_signal=None,
        completed_at=None, children_reaped='unknown', controller_state='unknown',
        run_id=None, controller_instance_id=None, code_revision=None)
    for name in ('supervisor_process', 'output_sha256', 'output_files', 'result', 'manifest_sha256'):
        saved.pop(name, None)
    runtime_control.write_control_bytes_atomic(original.file, json.dumps(saved).encode())
    policy = tmp_path / 'runtime/control/write-policy.json'
    runtime_control.write_control_bytes_atomic(policy, json.dumps({'schema_version': 1,
        'kind': 'preaudit_write_policy',
        'authorized_workflows': [*production_config.FINANCE_WORKFLOWS, 'packaging_layout_approval']}).encode())
    return tmp_path, original, action_file


def test_loss_source_only_reads_owned_closed_chain_without_fabricating_wait(lost_execution):
    root, original, action = lost_execution
    before = {path: path.read_bytes() for path in original.file.parent.iterdir() if path.is_file()}
    report = maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
    manifest = (root / 'runtime/control/rolling-controller-manifest.json').read_bytes()
    maintenance_runtime.assert_original_execution_safe(root, original, report, manifest,
        supervisor_loss_source_only=True)
    assert report['terminal'] is False
    assert report['exit_code'] is None
    assert report['source_only_proof']['original_wait_verified'] is False
    assert report['source_only_proof']['actions'] == [str(action)]
    assert report['source_only_proof']['readonly_reaped'] is True
    assert report['source_only_proof']['log_evidence_source'] == 'observed_after_supervisor_loss'
    assert {path: path.read_bytes() for path in before} == before
    with pytest.raises(RuntimeError, match='maintenance_original_execution_untrusted'):
        maintenance_runtime.assert_original_execution_safe(root, original, report, manifest)


def test_source_only_retains_trusted_same_run_cancelled_collection_and_blocked_continuation(lost_execution):
    root, original, action = lost_execution
    state_before = action.with_name('state.json').read_bytes()
    report = maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
    state = json.loads(action.with_name('state.json').read_bytes())
    reconciled = reconciliation.reconcile(root / 'runtime/finance_preaudit_flows',
        pending_rows=[{'request_id': state['request_id'], 'received_at': state['received_at'],
            'workflow_key': 'travel_expense'}], handled_rows=[])
    assert reconciled['continuation']['blocking_reasons'] == {'pending_failure_not_isolated': 1}
    status_file = root / 'status.json'
    raw = json.dumps({'reconciliation': reconciled}).encode()
    runtime_control.write_control_bytes_atomic(status_file, raw)
    receipt = {'path': str(status_file), 'sha256': hashlib.sha256(raw).hexdigest()}
    ordinary = maintenance_runtime.read_current_inputs(root, receipt)
    snapshot = live_snapshot()
    snapshot.update(reconciliation=reconciled, maintenance_status_file=receipt, maintenance_current_inputs=ordinary)
    with pytest.raises(RuntimeError, match='maintenance_reconciliation_untrusted'):
        maintenance_runtime.assert_live_safe(snapshot)
    proof = maintenance_runtime.read_current_inputs(root, receipt, source_only_proof=report['source_only_proof'])
    snapshot = live_snapshot()
    snapshot.update(reconciliation=reconciled, maintenance_status_file=receipt, maintenance_current_inputs=proof)
    maintenance_runtime.assert_live_safe(snapshot, source_only_proof=report['source_only_proof'])
    assert snapshot['reconciliation']['continuation']['status'] == 'blocked'
    assert proof['preserved_stop_failures'] == [str(action)]
    assert proof['pending_frozen_count'] == proof['validated_frozen_count'] == 1
    assert action.with_name('state.json').read_bytes() == state_before


@pytest.fixture
def source_only_production(lost_execution, monkeypatch):
    root, original, action = lost_execution
    (root / '.gitignore').write_text('runtime/\n.venv/\nstatus.json\n')
    (root / 'source.txt').write_text('first')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'first')
    before = git(root, 'rev-parse', 'HEAD')
    remote = root.parent / 'remote.git'
    git(root.parent, 'init', '--bare', '-q', '--initial-branch=main', str(remote))
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', '-q', '-u', 'origin', 'main')
    source = root.parent / 'source'
    git(root.parent, 'clone', '-q', str(remote), str(source))
    (source / 'source.txt').write_text('second')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'second')
    git(source, 'push', '-q')
    git(root, 'fetch', '-q', 'origin')
    expected = git(source, 'rev-parse', 'HEAD')
    runtime_control.write_control_bytes_atomic(root / 'runtime/stop-preaudit.flag',
        b'stop_requested:codex_review_technical_failure\n')
    native_home = root.parent / 'native-codex'
    monkeypatch.setenv('CODEX_HOME', str(native_home))
    cron = native_home / 'automations/oa-skill/automation.toml'
    cron.parent.mkdir(parents=True)
    prompt = (Path(production_release.__file__).resolve().parents[2] / 'config/automations/oa-skill-prompt.txt').read_text()
    cron.write_text('id = "oa-skill"\nstatus = "PAUSED"\nkind = "cron"\nexecution_environment = "local"\n'
        'model = "gpt-6.1-sol"\nreasoning_effort = "medium"\nrrule = "preserved"\n'
        + 'cwds = ' + json.dumps([str(root)]) + '\nprompt = ' + json.dumps(prompt, ensure_ascii=False)
        + '\n[target]\ntype = "project"\nproject_id = "production"\n')
    cron.chmod(0o600)
    return root, original, action, before, expected, remote, production_release.ProductionAutomation(cron, 'production', 'preserved')


def current_snapshot(root, original, action):
    report = maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
    state = json.loads(action.with_name('state.json').read_bytes())
    snapshot = live_snapshot()
    snapshot['reconciliation'] = reconciliation.reconcile(root / 'runtime/finance_preaudit_flows',
        pending_rows=[{'request_id': state['request_id'], 'received_at': state['received_at'],
            'workflow_key': state['workflow_key']}], handled_rows=[])
    raw = json.dumps(snapshot).encode()
    status_file = root / 'status.json'
    runtime_control.write_control_bytes_atomic(status_file, raw)
    snapshot['maintenance_status_file'] = {'path': str(status_file), 'sha256': hashlib.sha256(raw).hexdigest()}
    snapshot['maintenance_current_inputs'] = maintenance_runtime.read_current_inputs(root,
        snapshot['maintenance_status_file'], source_only_proof=report['source_only_proof'])
    return snapshot


def test_public_release_source_only_sync_preserves_unknown_wait_blocker_and_controls(source_only_production):
    root, original, action, before, expected, remote, cron = source_only_production
    stop = root / 'runtime/stop-preaudit.flag'
    before_files = {path: path.read_bytes() for path in (*original.file.parent.iterdir(),
        action, action.with_name('state.json'), action.with_name('manual-review-receipt.json'), stop, cron.file)}
    def live(target):
        assert runtime_control.controller_active(target / 'runtime/control/rolling-controller.lock')
        assert runtime_control.active_request_writers(target / 'runtime/control/frozen-writer.lock') == 1
        return current_snapshot(target, original, action)
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, stopped_maintenance=True, supervisor_loss_source_only=True,
        automation=cron, original_execution=original, check_live=live, check_configuration=lambda *_: [])
    assert result['status'] == 'synced'
    assert result['before_revision'] == before
    assert result['after_revision'] == expected
    assert result['next_process_ready'] is False
    assert result['original_wait_verified'] is False
    assert result['approval_recovery_allowed'] is False
    assert result['original_evidence_preserved'] is True
    assert result['continuation']['status'] == 'blocked'
    assert result['original_execution']['exit_code'] is None
    assert result['stop_preserved'] is True
    assert result['automation_state'] == 'PAUSED'
    assert {path: path.read_bytes() for path in before_files} == before_files


@pytest.mark.parametrize('scenario', ['active_supervisor', 'wrong_session', 'unreaped_controller',
    'other_failure', 'missing_manual_receipt', 'writer_artifact', 'unbound_action',
    'unknown_write', 'other_continuation_block', 'active_cron', 'manual_stop', 'default_mode'])
def test_source_only_rejects_untrusted_loss_or_business_conditions_without_updating(source_only_production, scenario):
    from apps.oa_finance_preaudit import item_failure
    root, original, action, before, expected, remote, cron = source_only_production
    if scenario == 'active_supervisor':
        saved = json.loads(original.file.read_bytes())
        saved['supervisor_pid'] = os.getpid()
        runtime_control.write_control_bytes_atomic(original.file, json.dumps(saved).encode())
    elif scenario == 'wrong_session':
        binding = json.loads(original.file.with_name('tool-session.json').read_bytes())
        binding['tool_session_id'] = 'different-session'
        runtime_control.write_control_bytes_atomic(original.file.with_name('tool-session.json'), json.dumps(binding).encode())
    elif scenario == 'unreaped_controller':
        mirror = original.file.with_name('controller-manifest.json')
        manifest = json.loads(mirror.read_bytes())
        manifest['children_reaped'] = False
        for path in (mirror, root / 'runtime/control/rolling-controller-manifest.json'):
            runtime_control.write_control_bytes_atomic(path, json.dumps(manifest).encode())
    elif scenario == 'other_failure':
        for path in (action, action.with_name('state.json')):
            state = json.loads(path.read_bytes())
            state['failure_detail_code'] = 'unknown_transport_failure'
            runtime_control.write_control_bytes_atomic(path, json.dumps(state).encode())
        item_failure.persist(action.parent, state)
    elif scenario == 'missing_manual_receipt':
        action.with_name('manual-review-receipt.json').unlink()
    elif scenario == 'writer_artifact':
        runtime_control.write_control_bytes_atomic(action.with_name('writer-boundary.json'), b'{}')
    elif scenario == 'unbound_action':
        progress_file = root / ('runtime/control/approval-guard/runs/' + 'a' * 32 + '/progress.json')
        progress = json.loads(progress_file.read_bytes())
        progress['actions'] = []
        runtime_control.write_control_bytes_atomic(progress_file, json.dumps(progress).encode())
    elif scenario == 'active_cron':
        cron.file.write_text(cron.file.read_text().replace('PAUSED', 'ACTIVE'))
    elif scenario == 'manual_stop':
        runtime_control.write_control_bytes_atomic(root / 'runtime/stop-preaudit.flag', b'stop_requested:manual\n')
    def live(target):
        snapshot = current_snapshot(target, original, action)
        if scenario == 'unknown_write':
            snapshot['reconciliation']['findings'][0]['local_write_state'] = 'unknown'
        if scenario == 'other_continuation_block':
            snapshot['reconciliation']['continuation']['blocking_reasons']['identity_conflict'] = 1
        return snapshot
    with pytest.raises(RuntimeError):
        production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
            confirmed_sync=True, stopped_maintenance=True, supervisor_loss_source_only=scenario != 'default_mode',
            automation=cron, original_execution=original, check_live=live, check_configuration=lambda *_: [])
    assert git(root, 'rev-parse', 'HEAD') == before


@pytest.mark.parametrize('file', ['execution.json', 'tool-session.json', 'stdout.log', 'stderr.log',
    'controller-manifest.json', 'progress.json', 'manual-review-receipt.json'])
def test_same_content_evidence_replacement_is_rejected_before_sync(source_only_production, file):
    root, original, action, before, expected, remote, cron = source_only_production
    def live(target):
        snapshot = current_snapshot(target, original, action)
        if file == 'progress.json':
            path = root / ('runtime/control/approval-guard/runs/' + 'a' * 32 + '/progress.json')
        elif file == 'manual-review-receipt.json':
            path = action.with_name(file)
        else:
            path = original.file.with_name(file)
        runtime_control.write_control_bytes_atomic(path, path.read_bytes())
        return snapshot
    with pytest.raises(RuntimeError, match='production_release_original_evidence_changed'):
        production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
            confirmed_sync=True, stopped_maintenance=True, supervisor_loss_source_only=True,
            automation=cron, original_execution=original, check_live=live, check_configuration=lambda *_: [])
    assert git(root, 'rev-parse', 'HEAD') == before


def test_source_only_post_sync_error_reports_updated_source_and_unknown_preservation(source_only_production):
    root, original, action, before, expected, remote, cron = source_only_production
    calls = 0
    def live(target):
        nonlocal calls
        calls += 1
        snapshot = current_snapshot(target, original, action)
        if calls == 2:
            runtime_control.write_control_bytes_atomic(original.file.with_name('stdout.log'), b'changed after source sync')
        return snapshot
    result = production_release.release_source(root, expected_revision=expected, expected_origin=str(remote),
        confirmed_sync=True, stopped_maintenance=True, supervisor_loss_source_only=True,
        automation=cron, original_execution=original, check_live=live, check_configuration=lambda *_: [])
    assert result['status'] == 'blocked'
    assert result['code'] == 'production_release_original_evidence_changed'
    assert result['source_sync_state'] == 'updated'
    assert result['after_revision'] == expected
    assert result['original_evidence_preserved'] == 'unknown'
    assert result['original_wait_verified'] is False
    assert result['next_process_ready'] is False
    assert result['stop_preserved'] is True


@pytest.mark.parametrize('role', ['worker', 'pool', 'launch_intent', 'closed_model', 'cooldown'])
def test_loss_reader_refuses_live_registered_children_and_official_cooldown(lost_execution, role):
    from apps.oa_finance_preaudit import approval_guard
    root, original, action = lost_execution
    run = root / ('runtime/control/approval-guard/runs/' + 'a' * 32)
    progress = json.loads((run / 'progress.json').read_bytes())
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            identity = approval_guard.process_identity(process.pid)
            assert identity is not None
            if role == 'worker':
                payload = {'schema_version': 1, 'kind': 'approval_readonly_worker', 'role': 'prepare',
                    'run_id': 'a' * 32, 'controller_instance_id': 'b' * 32,
                    'controller': progress['controller'], 'process': identity}
                path = run / f'worker-{process.pid}.json'
            elif role == 'pool':
                pool = uuid4().hex
                payload = {'schema_version': 1, 'kind': 'approval_readonly_pool', 'role': 'review',
                    'run_id': 'a' * 32, 'controller_instance_id': 'b' * 32, 'state': 'registered',
                    'controller': progress['controller'], 'pool_id': pool, 'processes': [identity]}
                path = run / f'pool-{pool}.json'
            elif role == 'cooldown':
                now = time.time()
                payload = {'schema_version': 1, 'kind': 'approval_model_cooldown', 'triggered_at': now,
                    'blocked_until': now + 10800, 'notice_code': next(iter(approval_guard.NOTICE_CODES)),
                    'sources': ['f' * 64], 'run_id': 'a' * 32}
                path = run.parent.parent / 'cooldown.json'
            else:
                now = time.time()
                payload = {'schema_version': 1, 'kind': 'approval_model_slot', 'slot': 0,
                    'launch_id': uuid4().hex, 'state': 'closed' if role == 'closed_model' else 'launching',
                    'pid': process.pid if role == 'closed_model' else None,
                    'process': identity if role == 'closed_model' else None, 'worker': identity,
                    'run_id': 'a' * 32, 'controller_instance_id': 'b' * 32, 'project_root': str(root),
                    'action_file': str(action), 'action_sha256': hashlib.sha256(action.read_bytes()).hexdigest(),
                    'model': 'gpt-6.1-sol', 'created_at': now, 'deadline': now + 300}
                path = run.parent.parent / 'model-slot-0.json'
            runtime_control.write_control_bytes_atomic(path, json.dumps(payload).encode())
            with pytest.raises(RuntimeError):
                maintenance_runtime.read_original_execution(root, original, supervisor_loss_source_only=True)
        finally:
            process.terminate()
            process.wait(timeout=5)
