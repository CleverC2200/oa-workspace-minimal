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
from apps.oa_finance_preaudit import maintenance_runtime, owner_restart_recovery, production_config, reconciliation, flow_engine
from scripts.finance_preaudit_run import bind_tool_session
from tests.finance_preaudit.test_flow_engine import _travel_material_with_attachment
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment, live_snapshot
from tests.finance_preaudit.test_production_release import git


@pytest.fixture
def restart_target(tmp_path, monkeypatch, request):
    root = tmp_path / 'oa-workspace-minimal'
    root.mkdir()
    source = Path(maintenance_runtime.__file__).resolve().parents[2]
    for name in ('apps', 'scripts'):
        shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
    for relative in (*production_config.REPOSITORY_INPUTS[:2], production_config.MODEL_CATALOG):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / relative).read_bytes())
    interpreter = root / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    interpreter.chmod(0o700)
    auth = tmp_path / 'offline.env'
    auth.write_text('')
    auth.chmod(0o600)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(auth))
    flows = root / 'runtime/finance_preaudit_flows'
    rows = [{'request_id':'offline-pending','received_at':'2026-10-10 09:00:00','workflow_key':'travel_expense'}]
    engine = flow_engine.FlowEngine(root=flows, discover_candidates=lambda _:rows, discover_handled=lambda:[],
        collect_material=_travel_material_with_attachment)
    action = Path(engine.next_action('travel_expense')['action_file'])
    directory = root / 'runtime/control/executions' / uuid4().hex
    owner = str(uuid4())
    child = '''import os,json,datetime,sys
from pathlib import Path
from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle
root=Path.cwd()
manifest={'kind':'preaudit_controller_run_manifest','schema_version':1,'controller_pid':os.getpid(),
 'run_id':'a'*32,'controller_instance_id':'b'*32,'state':'failed','children_reaped':True,
 'code_revision':'c'*40,'code_dirty':False,'config_fingerprint':'sha256:'+'f'*64,
 'previous_run_id':None,'previous_controller_instance_id':None,'restart_mode':'fresh_process',
 'controller_lock_verified':True,'writer_inactive_at_start':True,'restart_verified':True,'health':'degraded',
 'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
with runtime_control.global_controller_lease(), lifecycle.run_scope(manifest,root):
 lifecycle.heartbeat(manifest['run_id'],manifest['controller_instance_id'],{Path(sys.argv[1])},[],writer_active=False)
 runtime_control.request_stop('codex_review_technical_failure')
manifest['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
for file in (Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE']),root/'runtime/control/rolling-controller-manifest.json'):
 guard.write(file,manifest)
raise SystemExit(1)'''
    if getattr(request, 'param', None) == 'user_stop':
        child = child.replace("runtime_control.request_stop('codex_review_technical_failure')", "runtime_control.request_stop()")
    supervisor = '''import json,sys
from pathlib import Path
from scripts.finance_preaudit_run import supervise
result=supervise([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]],directory=Path(sys.argv[3]),owner_task_id=sys.argv[4])
print(json.dumps(result))'''
    with monkeypatch.context() as environment:
        environment.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(root / 'runtime/control/rolling-controller.lock'))
        finished = subprocess.run([sys.executable, '-B', '-c', supervisor, child, str(action), str(directory), owner],
            cwd=root, capture_output=True, text=True, check=True, start_new_session=True)
        receipt = json.loads(finished.stdout.strip().splitlines()[-1])
        original = maintenance_runtime.OriginalExecution(Path(receipt['receipt_file']), owner, 'original-session')
        bind_tool_session(original.file, owner_task_id=owner, session_id=original.tool_session_id)
    # Real supervisor wait; this offline fixture simulates the two restart cause flags.
    saved = json.loads(original.file.read_bytes())
    saved.update(owner_process_lost=True, stop_requested_by_supervisor=True)
    runtime_control.write_control_bytes_atomic(original.file, json.dumps(saved).encode())
    runtime_control.write_control_bytes_atomic(root / 'runtime/control/write-policy.json', json.dumps({
        'schema_version':1,'kind':'preaudit_write_policy',
        'authorized_workflows':[*production_config.FINANCE_WORKFLOWS,'packaging_layout_approval']}).encode())
    (root / '.gitignore').write_text('runtime/\n.venv/\nstatus.json\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'offline restart recovery')
    revision = git(root, 'rev-parse', 'HEAD')
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '-q', '--initial-branch=main', str(remote))
    git(root, 'remote', 'add', 'origin', str(remote))
    git(root, 'push', '-q', '-u', 'origin', 'main')
    home = tmp_path / 'native-codex'
    monkeypatch.setenv('CODEX_HOME', str(home))
    cron = home / 'automations/oa-skill/automation.toml'
    cron.parent.mkdir(parents=True)
    cron.write_text('id="oa-skill"\nstatus="PAUSED"\nkind="cron"\nexecution_environment="local"\n'
        'model="gpt-6.1-sol"\nreasoning_effort="medium"\nrrule="preserved"\n'
        + 'cwds=' + json.dumps([str(root)]) + '\nprompt='
        + json.dumps((root / 'config/automations/oa-skill-prompt.txt').read_text(), ensure_ascii=False)
        + '\n[target]\ntype="project"\nproject_id="production"\n')
    cron.chmod(0o600)
    args = ['--target-root',str(root),'--expected-revision',revision,'--expected-origin',str(remote),
        '--automation-file',str(cron),'--production-project-id','production','--expected-schedule','preserved',
        '--original-receipt-file',str(original.file),'--original-owner-task-id',owner,
        '--original-tool-session-id',original.tool_session_id]

    def live(target):
        snapshot = live_snapshot()
        snapshot['reconciliation'] = reconciliation.reconcile(flows, pending_rows=rows, handled_rows=[])
        raw = json.dumps(snapshot).encode()
        status = target / 'status.json'
        runtime_control.write_control_bytes_atomic(status, raw)
        snapshot['maintenance_status_file'] = {'path':str(status),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        snapshot['maintenance_current_inputs'] = maintenance_runtime.read_current_inputs(target, snapshot['maintenance_status_file'])
        return snapshot

    return root, original, action, cron, args, live


@pytest.mark.parametrize('confirmed', [False, True])
def test_public_restart_recovery_preserves_real_failed_wait_and_all_business_evidence(restart_target, capsys, confirmed):
    root, original, action, cron, args, live = restart_target
    stop = root / 'runtime/stop-preaudit.flag'
    protected = {file:file.read_bytes() for file in (*original.file.parent.iterdir(),
        *action.parent.iterdir(), cron) if file.is_file()}
    code = owner_restart_recovery.main([*args, *(['--confirm-recovery','YES'] if confirmed else [])], check_live=live)
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert result['status'] == ('cleared' if confirmed else 'ready')
    assert result['stop_cleared'] is confirmed
    assert result['original_wait_verified'] is True
    assert result['original_exit_code'] == 1
    assert result['approval_started'] is False and result['write_state'] == 'not_requested'
    assert result['automation_state'] == 'PAUSED'
    assert stop.exists() is not confirmed
    assert {file:file.read_bytes() for file in protected} == protected
    if confirmed:
        assert json.loads(Path(result['receipt_file']).read_bytes()) == result
        assert Path(result['receipt_file']).stat().st_mode & 0o077 == 0


def test_prepare_failure_recovery_preflight_preserves_exact_stop_and_material_proof(
    restart_target, capsys, monkeypatch,
):
    root, original, action, cron, args, live = restart_target
    stop = root / 'runtime/stop-preaudit.flag'
    cron_before = cron.read_bytes()
    runtime_control.write_control_bytes_atomic(
        stop, b'stop_requested:production_probe_prepare_failed\n'
    )
    current_inputs = {
        'kind': 'preaudit_maintenance_current_inputs',
        'status': 'verified',
        'read_only': True,
        'target_root': str(root),
        'write_state': 'not_requested',
        'verified_material_failure_actions': [str(action)],
        'material_failure_recovery_ready': True,
        'material_failure_fingerprints': {},
    }
    snapshot = live(root)
    snapshot['maintenance_current_inputs'] = current_inputs
    snapshot['reconciliation'] = {
        'continuation': {
            'status': 'blocked', 'scope': 'controller', 'isolated_pending': 0,
            'blocking_reasons': {'pending_failure_not_isolated': 1},
        },
        'quarantine': {'pending_quarantined': 1},
    }
    report = {
        'kind': 'preaudit_controller_execution_readback',
        'read_only': True,
        'state': 'exited',
        'terminal': True,
        'children_reaped': True,
        'controller_state': 'failed',
        'exit_code': 1,
        'run_id': 'a' * 32,
        'controller_instance_id': 'b' * 32,
        'code_revision': args[args.index('--expected-revision') + 1],
        'receipt_file': str(original.file),
        'owner_task_id': original.owner_task_id,
        'invocation_id': original.file.parent.name,
        'tool_session_id': original.tool_session_id,
        'owner_restart_proof': {'kind': 'preaudit_owner_restart_proof', 'read_only': True},
    }
    monkeypatch.setattr(owner_restart_recovery, '_read_prepare_failure_original', lambda *_, **__: {
        **report,
        'prepare_failure_recovery_proof': report['owner_restart_proof'],
    })
    monkeypatch.setattr(maintenance_runtime, 'assert_live_safe', lambda *_, **__: None)
    monkeypatch.setattr(maintenance_runtime, 'read_current_inputs', lambda *_, **__: current_inputs)
    code = owner_restart_recovery.main(
        [*args, '--prepare-failure-recovery', 'YES', '--recover-action-file', str(action)],
        check_live=lambda _target: snapshot,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert result['status'] == 'ready'
    assert result['prepare_failure_recovery'] is True
    assert result['selected_recovery_action'] == str(action)
    assert result['stop_cleared'] is False
    assert result['approval_started'] is False
    assert result['automation_state'] == 'PAUSED'
    assert stop.read_bytes() == b'stop_requested:production_probe_prepare_failed\n'
    assert cron.read_bytes() == cron_before


@pytest.mark.parametrize('mutation', ['missing_owner_loss','missing_supervisor_stop','received_signal',
    'unknown_wait','manual_stop','cooldown','active_controller','progress_writer','original_changed','stop_replaced','auth','unknown_write','frozen_changed','attachment_replaced','prepared_replaced'])
def test_public_restart_recovery_refuses_other_causes_and_changing_evidence(restart_target, capsys, mutation):
    root, original, action, cron, args, live = restart_target
    stop = root / 'runtime/stop-preaudit.flag'
    saved = json.loads(original.file.read_bytes())
    if mutation in {'missing_owner_loss','missing_supervisor_stop','received_signal','unknown_wait'}:
        if mutation == 'missing_owner_loss': saved['owner_process_lost'] = False
        if mutation == 'missing_supervisor_stop': saved['stop_requested_by_supervisor'] = False
        if mutation == 'received_signal': saved['received_signals'] = [15]
        if mutation == 'unknown_wait': saved.update(state='waiting',exit_code=None)
        runtime_control.write_control_bytes_atomic(original.file, json.dumps(saved).encode())
    if mutation == 'manual_stop': runtime_control.write_control_bytes_atomic(stop, b'stop_requested\n')
    if mutation == 'cooldown':
        now = time.time()
        runtime_control.write_control_bytes_atomic(root / 'runtime/control/approval-guard/cooldown.json', json.dumps({
            'schema_version':1,'kind':'approval_model_cooldown','triggered_at':now,'blocked_until':now+10800,
            'notice_code':'server_overloaded','sources':['d'*64],'run_id':'a'*32}).encode())
    if mutation in {'active_controller','progress_writer'}:
        file = root / ('runtime/control/approval-guard/runs/' + 'a'*32 + '/progress.json')
        progress = json.loads(file.read_bytes())
        if mutation == 'progress_writer': progress['writer_active'] = True
        else: progress['controller']['pid'] = os.getpid()
        runtime_control.write_control_bytes_atomic(file, json.dumps(progress).encode())
    original_live = live
    def live(target):
        snapshot = original_live(target)
        if mutation == 'original_changed':
            saved['owner_process_lost'] = False
            runtime_control.write_control_bytes_atomic(original.file, json.dumps(saved).encode())
        if mutation == 'stop_replaced': runtime_control.write_control_bytes_atomic(stop, stop.read_bytes())
        if mutation == 'auth': snapshot['live_check']['oa']['authentication'] = 'failed'
        if mutation == 'unknown_write': snapshot['reconciliation']['continuation'].update(status='blocked',blocking_reasons={'pending_write_state_unresolved':1})
        if mutation == 'frozen_changed': runtime_control.write_control_bytes_atomic(action.with_name('material.json'), b'{}')
        if mutation in {'attachment_replaced','prepared_replaced'}:
            file = action.parent / ('attachments/evidence.bin' if mutation=='attachment_replaced' else 'prepared/evidence.png')
            runtime_control.write_control_bytes_atomic(file, file.read_bytes())
        return snapshot
    code = owner_restart_recovery.main([*args,'--confirm-recovery','YES'], check_live=live)
    result = json.loads(capsys.readouterr().out)
    assert code == 1, result
    assert result['status'] == 'blocked' and result['stop_cleared'] is False
    assert stop.exists()


@pytest.mark.parametrize('phase', ['validated','cleared','changed_after_audit'])
def test_public_restart_recovery_keeps_actual_clear_state_when_audit_fails(restart_target, capsys, monkeypatch, phase):
    root, original, _action, _cron, args, live = restart_target
    write = runtime_control.write_control_bytes_atomic
    def faulty(file, raw):
        if file.parent.name.startswith('owner-restart-recovery-'):
            status = json.loads(raw)['status']
            if status == phase:
                raise OSError('offline audit failure')
            if status == 'validated' and phase == 'changed_after_audit':
                saved = json.loads(original.file.read_bytes())
                saved['owner_process_lost'] = False
                write(original.file, json.dumps(saved).encode())
        return write(file, raw)
    monkeypatch.setattr(runtime_control, 'write_control_bytes_atomic', faulty)
    code = owner_restart_recovery.main([*args,'--confirm-recovery','YES'], check_live=live)
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result['status'] == 'blocked'
    assert result['stop_cleared'] is (phase=='cleared')
    assert (root/'runtime/stop-preaudit.flag').exists() is (phase!='cleared')
