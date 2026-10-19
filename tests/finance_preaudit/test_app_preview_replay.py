from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import time

import pytest

from apps.oa_finance_preaudit import app_review_shutdown, host_chat_review as host
from scripts import finance_preaudit_native_replay as replay, finance_preaudit_run
from tests.finance_preaudit.test_native_replay import recorded_case
from tests.finance_preaudit.test_app_reviewer_slots import native_sessions
from tests.finance_preaudit.test_host_chat_review import draft_source, native_call


@pytest.fixture
def app_case(recorded_case):
    arguments, _, _ = recorded_case
    source = Path(arguments[arguments.index('--project') + 1])
    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / 'apps', source / 'apps', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (source / 'scripts').mkdir()
    for name in ('finance_preaudit_native_replay.py', 'finance_preaudit_run.py'):
        shutil.copyfile(root / 'scripts' / name, source / 'scripts' / name)
    subprocess.run(['git', 'add', 'apps', 'scripts'], cwd=source, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--quiet', '-m', 'synthetic App execution sources'], cwd=source, check=True)
    return recorded_case


def wait_for(predicate, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f'preview exited {process.returncode}: {stdout} {stderr}')
        time.sleep(.02)
    pytest.fail('preview did not reach the expected public phase')


def test_app_preview_closes_one_frozen_case_through_native_exchange(app_case, tmp_path, monkeypatch, capsys):
    from apps.oa_finance_preaudit import codex_review_worker as worker
    arguments, original, _ = app_case
    owner, parent, reviewers = native_sessions(tmp_path / 'native', monkeypatch)
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setattr(worker, 'REVIEW_SETTINGS', host.IDENTITY)
    assert replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    audit = Path(prepared['audit'])
    original_bytes = original.read_bytes()
    production = tmp_path / 'production-control'
    production.mkdir()
    sentinel = production / 'stop.flag'
    sentinel.write_bytes(b'preserve-production-stop')
    controls = {
        'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE': str(audit / 'control/controller.lock'),
        'OA_FINANCE_PREAUDIT_STOP_FILE': str(audit / 'control/stop.flag'),
        'OA_FROZEN_WRITER_LOCK_FILE': str(audit / 'control/writer.lock'),
    }
    environment = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'HOME', 'CODEX_HOME', 'LANG', 'TMPDIR', 'UV_CACHE_DIR'}}
    environment.update(OA_REVIEW_PROVIDER='openai', OA_REVIEW_TRANSPORT='host_chat',
                       OA_PASSWORD='synthetic-secret-must-not-inherit',
                       OA_FINANCE_PREAUDIT_STOP_FILE=str(sentinel),
                       OA_CONTROLLER_EXECUTION_MANIFEST_FILE=str(production / 'do-not-create.json'),
                       OA_APPROVAL_RUN_ID='inherited-invalid-run')
    run_arguments = ['--run' if arg == '--prepare' else arg for arg in arguments]
    process = subprocess.Popen([__import__('sys').executable, '-B', '-m',
        'scripts.finance_preaudit_native_replay', *run_arguments, '--owner-task-id', owner],
        cwd=Path(__file__).resolve().parents[2], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for key, value in controls.items():
            monkeypatch.setenv(key, value)
        execution = wait_for(lambda: next((audit / 'control/executions').glob('*/execution.json'), None), process)
        binding = {'receipt_file': execution, 'invocation_id': execution.parent.name,
                   'tool_session_id': 'synthetic-original-tool'}
        finance_preaudit_run.bind_tool_session(execution, owner_task_id=owner,
            session_id=binding['tool_session_id'])
        wait_for(lambda: execution.with_name('controller-manifest.json').is_file(), process)
        with pytest.raises(ValueError):
            host.register(reviewers, owner=str(__import__('uuid').uuid4()), source=parent, **binding)
        registered = host.register(reviewers, owner=owner, source=parent, **binding)
        run = registered['run_id']
        line = native_call(parent, 'functions.exec', 'heartbeat ' + owner + ' ' + run, 'heartbeat', {})
        host.heartbeat(owner=owner, source=parent, source_line=line, run_id=run)
        requests = wait_for(lambda: host.pending(owner=owner, run_id=run), process)
        request = Path(requests[0]['request_file'])
        foreign = tmp_path / 'foreign-action.json'
        foreign.write_bytes(Path(prepared['action_file']).read_bytes())
        foreign.chmod(0o600)
        with pytest.raises(ValueError):
            host.publish(foreign, prompt='An unrelated action must be refused.', timeout_seconds=60, run_id=run)
        dispatched = native_call(parent, 'followup_task',
            {'target': '/root/reviewer_0', 'message': str(request)}, 'dispatch', {})
        host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatched, run_id=run)
        private_binding = execution.with_name('preview-binding.json')
        binding_bytes = private_binding.read_bytes()
        action = Path(prepared['action_file'])
        packet = json.loads(action.with_name('review-packet.json').read_text())
        decision = {'overall': 'needs_human', 'items': [
            {'item_key': item['item_key'], 'status': 'uncertain', 'conclusion': 'Synthetic evidence is incomplete.',
             'evidence_refs': [], 'missing_evidence': ['No synthetic payment proof.']} for item in packet['items']]}
        target = tmp_path / 'native-decision.json'
        child = Path(reviewers[0]['session_file'])
        written = draft_source(child, request, target, owner, payload=decision)
        with pytest.raises(ValueError, match='host_review_source_untrusted'):
            host.submit(request, target, owner=owner, source=child, source_line=written, run_id=run)
        target = tmp_path / 'native-decision-with-directory.json'
        published = host.publish_draft(request, target, decision, owner=owner)
        command = host.draft_command(request, target, decision, owner=owner)
        written = native_call(child, 'functions.exec', 'text(await tools.exec_command(' + json.dumps({
            'cmd': command, 'workdir': str(Path(__file__).resolve().parents[2])}) + '));',
            'draft-with-directory', {'exit_code': 0, 'output': json.dumps(published) + '\n'})
        assert host.submit(request, target, owner=owner, source=child, source_line=written, run_id=run)['status'] == 'published'
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, (stdout, stderr)
        result = json.loads((audit / 'result.json').read_text())
        assert result['preview_closed_no_write'] is True
        assert result['write_state'] == 'not_requested'
        assert result['oa_writes'] == 0
        assert result['business_acceptance'] == 'not_verified'
        assert sentinel.read_bytes() == b'preserve-production-stop'
        assert original.read_bytes() == original_bytes
        assert sorted(item.name for item in production.iterdir()) == ['stop.flag']
        app_review_shutdown.begin(owner=owner, reason='completed', run_id=run)
        interrupts = [native_call(parent, 'interrupt_agent', {'target': f'/root/reviewer_{slot}'},
            f'interrupt-{slot}', {'previous_status': 'completed'}) for slot in range(3)]
        status = native_call(parent, 'list_agents', {}, 'idle', {'agents': [
            {'agent_name': f'/root/reviewer_{slot}', 'agent_status': 'completed'} for slot in range(3)]})
        waited = native_call(parent, 'functions.exec',
            'text(await tools.write_stdin({"session_id":"synthetic-original-tool","chars":""}));',
            'original-wait', {'exit_code': process.returncode, 'output': stdout})
        assert app_review_shutdown.finish(owner=owner, source=parent, interrupt_lines=interrupts,
            status_line=status, wait_line=waited, run_id=run)['status'] == 'verified'
        assert app_review_shutdown.sealed_round(owner=owner, run_id=run)['state'] == 'closed'
        expected = tmp_path / 'expectations.json'
        expected.write_text(json.dumps({'schema_version': 1, 'cases': [{'case': 'sample-a',
            'allowed_statuses': {item['item_key']: ['uncertain'] for item in packet['items']}}]}))
        review = tmp_path / 'independent.json'
        preparation = json.loads((audit / 'prepare.json').read_text())
        review.write_text(json.dumps({'schema_version': 1, 'case': 'sample-a', 'label': 'baseline',
            'source_head': preparation['source_head'], 'expectations_sha256': replay.digest(expected),
            'decision_sha256': replay.digest(action.with_name('native-preview-decision.json')),
            'verdict': 'accepted', 'findings': []}))
        verify_arguments = ['--verify' if arg == '--prepare' else arg for arg in arguments]
        verify_arguments += ['--expectations', str(expected), '--expectations-sha256', replay.digest(expected),
            '--independent-review', str(review), '--review-sha256', replay.digest(review)]
        capsys.readouterr()
        assert replay.main(verify_arguments) == 0
        assert json.loads(capsys.readouterr().out)['business_acceptance'] == 'accepted'
        private_binding.unlink()
        assert replay.main(verify_arguments) == 1
        capsys.readouterr()
        private_binding.write_bytes(binding_bytes)
        private_binding.chmod(0o600)
        parent_bytes = parent.read_bytes()
        source_lines = parent.read_text().splitlines()
        idle_output = json.loads(source_lines[status])
        agents = json.loads(idle_output['payload']['output'])
        agents['agents'][0]['agent_status'] = 'running'
        idle_output['payload']['output'] = json.dumps(agents)
        source_lines[status] = json.dumps(idle_output)
        parent.write_text('\n'.join(source_lines) + '\n')
        assert replay.main(verify_arguments) == 1
        capsys.readouterr()
        parent.write_bytes(parent_bytes)
        execution.with_name('tool-session.json').unlink()
        assert replay.main(verify_arguments) == 1
        assert 'app_native_source_unverified' in json.loads(capsys.readouterr().out)['findings'][0]
    finally:
        if process.poll() is None:
            Path(controls['OA_FINANCE_PREAUDIT_STOP_FILE']).touch()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def test_app_preview_freezes_the_actual_execution_sources(app_case, monkeypatch, capsys):
    from apps.oa_finance_preaudit import codex_review_worker as worker
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setattr(worker, 'REVIEW_SETTINGS', host.IDENTITY)
    arguments, _, _ = app_case
    assert replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    manifest = json.loads((Path(prepared['audit']) / 'prepare.json').read_text())
    paths = {item['path'] for item in manifest['source_manifest']}
    assert {'scripts/finance_preaudit_native_replay.py', 'scripts/finance_preaudit_run.py'} <= paths
    frozen = Path(prepared['project_root']) / 'apps/oa_finance_preaudit/host_chat_review.py'
    frozen.write_bytes(frozen.read_bytes() + b'\n')
    run_arguments = ['--run' if arg == '--prepare' else arg for arg in arguments]
    with pytest.raises(ValueError, match='frozen_source_changed'):
        replay.main(run_arguments + ['--owner-task-id', str(__import__('uuid').uuid4())])
    assert not (Path(prepared['audit']) / 'app-preview.json').exists()


@pytest.mark.parametrize('field,value', [('mode', 'controlled_write'), ('write_authorized', True),
                                       ('oa_client_config_or_submit_callback', True)])
def test_app_preview_refuses_write_capability_before_execution(app_case, monkeypatch, capsys, field, value):
    from apps.oa_finance_preaudit import codex_review_worker as worker
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setattr(worker, 'REVIEW_SETTINGS', host.IDENTITY)
    arguments, _, _ = app_case
    assert replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    path = Path(prepared['audit']) / 'prepare.json'
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    run_arguments = ['--run' if arg == '--prepare' else arg for arg in arguments]
    with pytest.raises(ValueError, match='app_replay_preview_required'):
        replay.main(run_arguments + ['--owner-task-id', str(__import__('uuid').uuid4())])
    assert not path.with_name('app-preview.json').exists()


@pytest.mark.parametrize('stopping', ['private-stop', 'owner-signal'])
def test_app_preview_stops_and_reaps_its_owned_process(app_case, tmp_path, monkeypatch, capsys, stopping):
    from apps.oa_finance_preaudit import codex_review_worker as worker
    arguments, original, _ = app_case
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'host_chat')
    monkeypatch.setattr(worker, 'REVIEW_SETTINGS', host.IDENTITY)
    assert replay.main(arguments) == 0
    prepared = json.loads(capsys.readouterr().out)
    audit = Path(prepared['audit'])
    original_bytes = original.read_bytes()
    run_arguments = ['--run' if arg == '--prepare' else arg for arg in arguments]
    environment = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'HOME', 'CODEX_HOME', 'LANG', 'TMPDIR'}}
    environment.update(OA_REVIEW_PROVIDER='openai', OA_REVIEW_TRANSPORT='host_chat')
    process = subprocess.Popen([__import__('sys').executable, '-B', '-m',
        'scripts.finance_preaudit_native_replay', *run_arguments, '--owner-task-id', str(__import__('uuid').uuid4())],
        cwd=Path(__file__).resolve().parents[2], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        execution = wait_for(lambda: next((audit / 'control/executions').glob('*/controller-manifest.json'), None), process)
        if stopping == 'private-stop':
            (audit / 'control/stop.flag').touch()
        else:
            process.terminate()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode != 0, (stdout, stderr)
        receipt = json.loads(execution.with_name('execution.json').read_text())
        assert receipt['state'] == 'exited'
        assert receipt['children_reaped'] is True
        assert type(receipt['exit_code']) is int
        assert receipt['controller_state'] == 'failed'
        assert not host.guard.group_alive(receipt['process_group_id'])
        assert original.read_bytes() == original_bytes
        assert not (audit / 'worker-result.json').exists()
    finally:
        if process.poll() is None:
            (audit / 'control/stop.flag').touch()
            process.communicate(timeout=15)
