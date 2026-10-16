from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from apps.oa_finance_preaudit import shutdown_stop_recovery as recovery, maintenance_runtime, production_release
from tests.finance_preaudit.test_owner_restart_recovery import restart_target  # noqa: F401
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment  # noqa: F401
from tests.finance_preaudit.test_app_reviewer_slots import native_sessions


def candidate_git(_root, *args):
    if args == ('status', '--porcelain'):
        return ''
    return 'main' if args == ('branch', '--show-current') else 'f' * 40


@pytest.fixture
def sealed_target(restart_target, tmp_path, monkeypatch):
    root, original, action, cron, args, live = restart_target
    home = Path(__import__('os').environ['CODEX_HOME'])
    owner, parent, reviewers = native_sessions(tmp_path / 'native', monkeypatch)
    monkeypatch.setenv('CODEX_HOME', str(home))
    sessions = home / 'sessions'
    sessions.mkdir()
    for file in parent.parent.iterdir():
        target = sessions / file.name
        target.write_text(file.read_text().replace(owner, original.owner_task_id))
        target.chmod(0o600)
    parent = sessions / parent.name
    for row in reviewers:
        row['session_file'] = str(sessions / Path(row['session_file']).name)
    spec = tmp_path / 'reviewers.json'
    spec.write_text(json.dumps(reviewers))
    program = r'''
import json,sys,time
from pathlib import Path
from datetime import datetime,timezone
from apps.oa_finance_preaudit import host_chat_review as h,app_review_shutdown as s,controller_handoff as c
owner,parent,spec,execution=sys.argv[1:];parent=Path(parent)
manifest=c._read_manifest();registered=datetime.fromisoformat(manifest['started_at']).timestamp()+.001
value={'schema_version':1,'kind':'preaudit_app_review_round','owner_task_id':owner,
 'run_id':manifest['run_id'],'controller_instance_id':manifest['controller_instance_id'],
 'reviewers':h._reviewers(json.loads(Path(spec).read_text()),owner,parent),
 'original_execution':{'receipt_file':execution,'owner_task_id':owner,
  'invocation_id':Path(execution).parent.name,'tool_session_id':'original-session'},
 'phase':'finance','state':'cancelled','close_reason':'reviewer_failed','assignments':{},
 'registered_at':registered,'heartbeat_at':registered}
def call(name,args,output,before_output=None):
 line=sum(1 for _ in parent.open())+1
 with parent.open('a') as stream:
  stream.write(json.dumps({'timestamp':datetime.now(timezone.utc).isoformat(),'type':'response_item',
   'payload':{'type':'custom_tool_call' if name=='functions.exec' else 'function_call','name':name,
    'call_id':str(line),'input' if name=='functions.exec' else 'arguments':args if name=='functions.exec' else json.dumps(args)}})+'\n')
  if before_output: output=before_output()
  stream.write(json.dumps({'timestamp':datetime.now(timezone.utc).isoformat(),'type':'response_item',
   'payload':{'type':'function_call_output','call_id':str(line),'output':json.dumps(output)}})+'\n')
 return line
plan={}
def stop():
 plan.update(status='pending',reason='reviewer_failed',started_at=time.time(),
  interrupt_targets=[r['agent_path'] for r in value['reviewers']],original_tool_session_id='original-session')
 marker=Path.cwd()/'runtime/stop-preaudit.flag';marker.write_bytes(b'stop_requested:controller_cycle_stop\n');marker.chmod(0o600)
 value['shutdown']=dict(plan);h.guard.write(h.registry_file(owner),value)
 return {'exit_code':0,'output':json.dumps(plan)}
cmd='.venv/bin/python -B -m apps.oa_finance_preaudit.host_chat_review --operation begin-shutdown --owner-task-id '+owner+' --reason reviewer_failed'
begin=call('functions.exec','text(await tools.exec_command('+json.dumps({'cmd':cmd,'max_output_tokens':1800})+'));',None,stop)
interrupts=[call('interrupt_agent',{'target':r['agent_path']},{'previous_status':'completed'}) for r in value['reviewers']]
status=call('list_agents',{}, {'agents':[{'agent_name':r['agent_path'],'agent_status':'completed'} for r in value['reviewers']]})
wait=call('functions.exec','text(await tools.write_stdin('+json.dumps({'session_id':'original-session','chars':'','yield_time_ms':1000})+'));',{'exit_code':1,'output':'failed'})
s.finish(owner=owner,source=parent,interrupt_lines=interrupts,status_line=status,wait_line=wait)
print(begin)
'''
    result = subprocess.run([str(root / '.venv/bin/python'), '-B', '-c', program,
        original.owner_task_id, str(parent), str(spec), str(original.file)], cwd=root,
        text=True, capture_output=True, check=True)
    begin = int(result.stdout)
    return root, original, action, cron, live, begin, parent


def test_target_owned_bound_reader_revalidates_original_native_shutdown(sealed_target):
    root, original, _, _, _, begin, parent = sealed_target
    protected = {p:p.read_bytes() for p in [parent,original.file,root/'runtime/control/rolling-controller-manifest.json']}
    bound = recovery._bound(root, original, begin)
    assert bound['execution']['exit_code'] == 1
    assert bound['shutdown']['status'] == 'verified'
    assert {p:p.read_bytes() for p in protected} == protected


def test_bound_reader_accepts_only_bound_readback_after_begin(sealed_target):
    root, original, _, _, _, begin, parent = sealed_target
    report = recovery._bound(root, original, begin)['execution']
    rows = parent.read_text().splitlines()
    call = json.loads(rows[begin - 1])
    command = ('.venv/bin/python -m scripts.finance_preaudit_run --owner-task-id '
        + original.owner_task_id + ' --receipt-file ' + str(original.file)
        + ' --invocation-id ' + original.file.parent.name
        + ' --tool-session-id ' + original.tool_session_id + ' --retrieve-result')
    call['payload']['input'] += '\ntext(await tools.exec_command(' + json.dumps({'cmd': command, 'max_output_tokens': 6000}) + '));'
    output = json.loads(rows[begin])
    output['payload']['output'] = json.dumps([json.loads(output['payload']['output']),
        {'exit_code': report['exit_code'], 'output': json.dumps(report)}])
    rows[begin - 1], rows[begin] = json.dumps(call), json.dumps(output)
    parent.write_text('\n'.join(rows) + '\n')
    assert recovery._bound(root, original, begin)['shutdown']['status'] == 'verified'


@pytest.mark.parametrize('identity', ['reused', 'missing_identity', 'live_group'])
def test_bound_reader_distinguishes_reused_pid_from_live_or_unknown_model(sealed_target, identity):
    import time
    from apps.oa_finance_preaudit import approval_guard as guard
    root, original, _, _, _, begin, _ = sealed_target
    with subprocess.Popen([str(root / '.venv/bin/python'), '-c', 'import time; time.sleep(30)'],
                          start_new_session=identity == 'live_group') as process:
        try:
            actual = guard.process_identity(process.pid)
            assert actual is not None
            saved = dict(actual, birth='original closed model', pgid=process.pid)
            slot = {'kind': 'approval_model_slot', 'schema_version': 1, 'slot': 0,
                'state': 'closed', 'launch_id': 'a' * 32, 'worker': saved,
                'deadline': time.time() + 60, 'created_at': time.time(),
                'run_id': None, 'controller_instance_id': None, 'model': 'gpt-6.1-sol',
                'project_root': str(root), 'action_sha256': 'a' * 64,
                'action_file': str(root / 'action.json'), 'pid': process.pid,
                'process': None if identity == 'missing_identity' else saved}
            file = root / 'runtime/control/approval-guard/model-slot-0.json'
            file.write_text(json.dumps(slot))
            file.chmod(0o600)
            if identity == 'reused':
                assert recovery._bound(root, original, begin)['shutdown']['status'] == 'verified'
            else:
                with pytest.raises(ValueError):
                    recovery._bound(root, original, begin)
        finally:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize('damage', ['wrong_begin','wrong_owner','stop_time','wait','status','unverified','receipt','extra_command','extra_result'])
def test_bound_reader_rejects_mismatched_shutdown_evidence(sealed_target, damage):
    root, original, _, _, _, begin, parent = sealed_target
    if damage == 'wrong_begin': begin += 2
    if damage == 'wrong_owner':
        original = maintenance_runtime.OriginalExecution(original.file,'00000000-0000-0000-0000-000000000001',original.tool_session_id)
    if damage == 'stop_time':
        import os
        os.utime(root/'runtime/stop-preaudit.flag',(1,1))
    if damage in {'wait','status'}:
        rows=parent.read_text().splitlines()
        for index,line in enumerate(rows):
            row=json.loads(line);payload=row.get('payload',{})
            if payload.get('name') == ('list_agents' if damage=='status' else 'functions.exec') and (damage=='status' or 'write_stdin' in payload.get('input','')):
                result=json.loads(rows[index+1]);result['payload']['output']=json.dumps({'agents':[]} if damage=='status' else {'exit_code':0,'output':'forged'})
                rows[index+1]=json.dumps(result)
        parent.write_text('\n'.join(rows)+'\n')
    if damage in {'extra_command','extra_result'}:
        rows=parent.read_text().splitlines()
        record=json.loads(rows[begin-1 if damage=='extra_command' else begin])
        if damage=='extra_command':
            record['payload']['input']+='\ntext(await tools.exec_command({\"cmd\":\"true\",\"max_output_tokens\":100}));'
        else:
            original_output=json.loads(record['payload']['output'])
            record['payload']['output']=json.dumps([original_output,original_output])
        rows[begin-1 if damage=='extra_command' else begin]=json.dumps(record)
        parent.write_text('\n'.join(rows)+'\n')
    if damage in {'unverified','receipt'}:
        registry=root/'runtime/control/app-review-rounds'/original.owner_task_id/'registry.json'
        value=json.loads(registry.read_text())
        if damage=='unverified': value['shutdown']['status']='pending'
        else: value['shutdown']['receipt']['driver_exit_code']=0
        registry.write_text(json.dumps(value))
    with pytest.raises(ValueError): recovery._bound(root,original,begin)


@pytest.mark.parametrize('confirmed',[False,True])
def test_recovery_uses_live_gates_and_preserves_originals(sealed_target,monkeypatch,confirmed):
    root, original, action, cron, live, begin, _ = sealed_target
    monkeypatch.setattr(recovery.production_release,'_git',candidate_git)
    monkeypatch.setattr(recovery.production_release,'_source',lambda *_args,**_kwargs:'c'*40)
    monkeypatch.setattr(recovery.maintenance_runtime,'read_live_status',lambda target,**_:live(target))
    automation=production_release.ProductionAutomation(cron,'production','preserved')
    files=[*original.file.parent.iterdir(),*action.parent.iterdir(),cron]
    protected={p:p.read_bytes() for p in files if p.is_file()}
    result=recovery.recover(target_root=root,expected_revision='f'*40,original=original,
        automation=automation,begin_line=begin,confirmed=confirmed)
    assert result['status']==('cleared' if confirmed else 'ready')
    assert result['stop_cleared'] is confirmed
    assert (root/'runtime/stop-preaudit.flag').exists() is not confirmed
    assert {p:p.read_bytes() for p in protected}==protected
    if confirmed:
        assert json.loads(Path(result['receipt_file']).read_text())==result


@pytest.mark.parametrize('damage',['plain_stop','cron_active','auth','continuation','stop_replaced','inputs_changed','bound_changed'])
def test_recovery_refuses_current_gate_failures(sealed_target,monkeypatch,damage):
    root,original,_,cron,live,begin,_=sealed_target
    marker=root/'runtime/stop-preaudit.flag'
    monkeypatch.setattr(recovery.production_release,'_git',candidate_git)
    monkeypatch.setattr(recovery.production_release,'_source',lambda *_args,**_kwargs:'c'*40)
    def snapshot(target,**_):
        value=live(target)
        if damage=='auth': value['live_check']['oa']['authentication']='failed'
        if damage=='continuation': value['reconciliation']['continuation']['status']='blocked'
        if damage=='stop_replaced':
            raw=marker.read_bytes();marker.unlink();marker.write_bytes(raw);marker.chmod(0o600)
        return value
    monkeypatch.setattr(recovery.maintenance_runtime,'read_live_status',snapshot)
    if damage=='plain_stop': marker.write_bytes(b'stop_requested\n')
    if damage=='cron_active': cron.write_text(cron.read_text().replace('PAUSED','ACTIVE'))
    if damage=='inputs_changed': monkeypatch.setattr(recovery.maintenance_runtime,'read_current_inputs',lambda *_args,**_kwargs:{})
    if damage=='bound_changed':
        read=recovery._bound
        calls=[]
        def changed(*args):
            value=read(*args);calls.append(1)
            if len(calls)>1: value['stop_mtime_ns']+=1
            return value
        monkeypatch.setattr(recovery,'_bound',changed)
    result=recovery.recover(target_root=root,expected_revision='f'*40,original=original,
        automation=production_release.ProductionAutomation(cron,'production','preserved'),begin_line=begin,confirmed=True)
    assert result['status']=='blocked'
    assert result['stop_cleared'] is False and marker.exists()


def test_audit_failure_after_clear_preserves_actual_clear_result(sealed_target,monkeypatch):
    root,original,_,cron,live,begin,_=sealed_target
    monkeypatch.setattr(recovery.production_release,'_git',candidate_git)
    monkeypatch.setattr(recovery.production_release,'_source',lambda *_args,**_kwargs:'c'*40)
    monkeypatch.setattr(recovery.maintenance_runtime,'read_live_status',lambda target,**_:live(target))
    write=recovery.runtime_control.write_control_bytes_atomic
    def fail_final(path,raw):
        if path.name.startswith('shutdown-stop-recovery-') and not (root/'runtime/stop-preaudit.flag').exists():
            raise OSError('offline audit failure')
        return write(path,raw)
    monkeypatch.setattr(recovery.runtime_control,'write_control_bytes_atomic',fail_final)
    result=recovery.recover(target_root=root,expected_revision='f'*40,original=original,
        automation=production_release.ProductionAutomation(cron,'production','preserved'),begin_line=begin,confirmed=True)
    assert result['status']=='maintenance_completion_failed'
    assert result['stop_cleared'] is True
    assert result['audit_finalization']=='failed'
