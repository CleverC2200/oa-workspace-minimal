import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import host_chat_review as host, read_material
from tests.finance_preaudit.test_host_chat_review import exchange, native_call
from tests.finance_preaudit.test_read_material import files


def test_heartbeat_cli_renews_from_current_call_without_manual_source(exchange, capsys, monkeypatch):
    action, _, owner, child, _, manifest, _ = exchange
    parent = child.with_name('parent.jsonl')
    request = host.publish(action, prompt='Offline frozen request.', timeout_seconds=1800)
    arguments = ['--operation', 'heartbeat', '--include-pending', '--owner-task-id', owner,
                 '--run-id', manifest['run_id']]
    native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'automatic-heartbeat', {})

    assert host.main(arguments) == 0

    result = json.loads(capsys.readouterr().out)
    assert result['renew_by'] == result['heartbeat_at'] + 60
    assert result['expires_at'] == result['heartbeat_at'] + 300
    assert 0 <= result['max_wait_seconds'] <= 30
    assert [item['request_file'] for item in result['requests']] == [str(request)]
    original = result['heartbeat_at']
    monkeypatch.setattr(host.time, 'time', lambda: original + 90)
    assert host.main(arguments) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['heartbeat_at'] == original
    assert repeated['max_wait_seconds'] == 0
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'fresh-heartbeat', {})
    records = parent.read_text().splitlines()
    current = json.loads(records[line - 1])
    current['timestamp'] = datetime.fromtimestamp(original + 90, timezone.utc).isoformat()
    records[line - 1] = json.dumps(current)
    parent.write_text('\n'.join(records) + '\n')
    invocation = shlex.split(result['next_invocation']['cmd'])
    assert host.main(invocation[invocation.index('--operation'):]) == 0
    renewed = json.loads(capsys.readouterr().out)
    assert renewed['heartbeat_at'] == original + 90
    assert renewed['expires_at'] == original + 390
    monkeypatch.setattr(host.time, 'time', lambda: original + 390)
    with pytest.raises(ValueError, match='parent_lost_or_closed'):
        host.main(arguments)


def test_reader_command_uses_verified_python_and_utf8_without_path_python(tmp_path):
    action = files(tmp_path, {
        'workflow_key': 'general_expense', 'declared_fields': [{'value': '中文与引号\'、换行\n保持原样'}],
        'prepared': {'images': []},
    })
    command = read_material.command(action)
    assert shlex.split(command)[:4] == [sys.executable, '-X', 'utf8', '-B']
    result = subprocess.run(command, shell=True, executable='/bin/sh', cwd=Path(host.__file__).parents[2],
        env=dict(os.environ, PATH='', LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0'),
        capture_output=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    assert '中文与引号' in result.stdout


def assigned_request(exchange, *, timeout_seconds=1800):
    action, draft, owner, child, *_ = exchange
    request = host.publish(action, prompt='Offline frozen request.', timeout_seconds=timeout_seconds)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)},
                       'direct-json-dispatch', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=line)
    return request, draft.with_name('direct-json.json'), json.loads(draft.read_text())


def execute_native_template(code):
    from playwright._impl._driver import compute_driver_executable

    script = '''const {spawnSync} = require("node:child_process");
const tools = {exec_command: async (args) => {
    const result = spawnSync("/bin/sh", ["-c", args.cmd], {cwd: args.workdir, encoding: "utf8"});
    return {exit_code: result.status, output: result.stdout, stderr: result.stderr};
}};
const text = value => process.stdout.write(JSON.stringify(value));
(async () => {''' + code + '''})().catch(error => {console.error(error); process.exitCode = 1;});'''
    result = subprocess.run([compute_driver_executable()[0], '-e', script],
        env=dict(os.environ, UV_OFFLINE='1', PYTHONPATH=str(Path(host.__file__).parents[2]), LC_ALL='C',
                 PYTHONUTF8='0', PYTHONCOERCECLOCALE='0'), capture_output=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['exit_code'] == 0, output.get('stderr')
    return output


def test_direct_json_publication_preserves_chinese_and_every_evidence_reference(exchange):
    request, target, payload = assigned_request(exchange)
    _, _, owner, child, *_ = exchange
    payload['items'][0]['conclusion'] = '中文、单引号\'、双引号"、换行\n、反斜线\\、$(false)、`false`、表情😀'
    payload['items'][0]['missing_evidence'] = ['逐页核对，仍缺关联']
    code = host.draft_invocation(request, target, payload, owner=owner)
    arguments = host.draft_arguments(code)
    assert shlex.split(arguments['cmd'])[-1] == json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    output = execute_native_template(code)
    assert json.loads(target.read_text()) == payload
    line = native_call(child, 'functions.exec', code, 'direct-json-publication',
                       output)
    assert host.submit(request, target, owner=owner, source=child, source_line=line)['status'] == 'published'
    assert host.read_result(request)['status'] == 'reviewed'


@pytest.mark.parametrize('number', [120.0, 1e-7, 1e20, -0.0])
def test_actual_javascript_numeric_publication_binds_the_real_byte_receipt(exchange, number):
    request, target, payload = assigned_request(exchange)
    _, _, owner, child, *_ = exchange
    payload['fuel_trip_observation'] = {'outcome': 'uncertain', 'groups': [], 'total_km': number}
    code = host.draft_invocation(request, target, payload, owner=owner)
    output = execute_native_template(code)
    line = native_call(child, 'functions.exec', code, 'numeric-publication', output)
    registered = json.loads(host.registry_file(owner).read_text())
    proof = host.validate_source(child, line, registered['reviewers'][0]['reviewer_task_id'], target,
                                 dispatched_at=registered['assignments'][json.loads(request.read_text())['request_id']]['dispatched_at'])
    assert proof['decision_input_sha256'] == json.loads(output['output'])['decision_sha256']
    assert json.loads(target.read_text())['fuel_trip_observation']['total_km'] == number


def test_actual_javascript_cannot_round_an_authored_integer_silently(exchange):
    request, target, payload = assigned_request(exchange)
    _, _, owner, child, *_ = exchange
    payload['fuel_trip_observation'] = {'outcome': 'uncertain', 'groups': [], 'total_km': 9007199254740993}
    code = host.draft_invocation(request, target, payload, owner=owner)
    output = execute_native_template(code)
    line = native_call(child, 'functions.exec', code, 'rounded-integer', output)
    registered = json.loads(host.registry_file(owner).read_text())
    with pytest.raises(ValueError, match='source_untrusted'):
        host.validate_source(child, line, registered['reviewers'][0]['reviewer_task_id'], target,
                             dispatched_at=json.loads(request.read_text())['created_at'])


def test_source_cannot_replace_a_number_with_a_boolean(exchange):
    request, target, payload = assigned_request(exchange)
    _, _, owner, child, *_ = exchange
    payload['fuel_trip_observation'] = {'outcome': 'uncertain', 'groups': [], 'total_km': 1}
    code = host.draft_invocation(request, target, payload, owner=owner)
    payload['fuel_trip_observation']['total_km'] = True
    receipt = host.publish_draft(request, target, payload, owner=owner)
    line = native_call(child, 'functions.exec', code, 'boolean-instead-of-number',
                       {'exit_code': 0, 'output': json.dumps(receipt)})
    registered = json.loads(host.registry_file(owner).read_text())
    with pytest.raises(ValueError, match='source_untrusted'):
        host.validate_source(child, line, registered['reviewers'][0]['reviewer_task_id'], target,
                             dispatched_at=json.loads(request.read_text())['created_at'])


def test_direct_json_source_keeps_multi_page_refs_without_another_payload_copy(exchange):
    request, target, payload = assigned_request(exchange)
    _, _, owner, child, *_ = exchange
    refs = ['material:prepared.images.offline-page-a.1', 'material:prepared.images.offline-page-b.1']
    for item in payload['items']:
        item['evidence_refs'] = list(refs)
    code = host.draft_invocation(request, target, payload, owner=owner)
    arguments = host.draft_arguments(code)
    receipt = host.publish_draft(request, target, json.loads(shlex.split(arguments['cmd'])[-1]), owner=owner)
    line = native_call(child, 'functions.exec', code, 'multi-page-publication',
                       {'exit_code': 0, 'output': json.dumps(receipt)})
    proof = host.validate_source(child, line, json.loads(host.registry_file(owner).read_text())['reviewers'][0]['reviewer_task_id'],
                                 target, dispatched_at=json.loads(request.read_text())['created_at'])
    assert proof['decision_input_sha256'] == receipt['decision_sha256']
    assert all(item['evidence_refs'] == refs for item in json.loads(target.read_text())['items'])
    altered = code.replace('offline-page-b.1', 'offline-page-a.1')
    changed = native_call(child, 'functions.exec', altered, 'transcribed-reference',
                          {'exit_code': 0, 'output': json.dumps(receipt)})
    with pytest.raises(ValueError, match='source_untrusted'):
        host.validate_source(child, changed, proof['reviewer_task_id'], target,
                             dispatched_at=json.loads(request.read_text())['created_at'])


@pytest.mark.parametrize('damage', ['suffix', 'expression', 'duplicate_key', 'workdir', 'extra_tool_option'])
def test_direct_json_parser_rejects_dynamic_or_additional_execution(exchange, damage):
    request, target, payload = assigned_request(exchange)
    _, _, owner, *_ = exchange
    code = host.draft_invocation(request, target, payload, owner=owner)
    if damage == 'suffix':
        code += 'text(await tools.exec_command({"cmd":"false"}));'
    elif damage == 'expression':
        code = code.replace('JSON.stringify(', 'JSON.stringify(process.exit() || ')
    elif damage == 'duplicate_key':
        code = code.replace('{"cmd":', '{"cmd":"false","cmd":', 1)
    elif damage == 'workdir':
        code = code.replace(json.dumps(str(runtime_control.PROJECT_ROOT.absolute())), '"/tmp"')
    else:
        code = code.replace('"workdir":', '"shell":"/bin/sh","workdir":')
    with pytest.raises(ValueError, match='source_untrusted'):
        host.draft_arguments(code)


@pytest.mark.parametrize('damage', ['wrong_slot', 'expired', 'frozen', 'failed_output', 'changed_file', 'wrong_request'])
def test_direct_publication_keeps_original_submission_boundaries(exchange, monkeypatch, damage):
    request, target, payload = assigned_request(exchange, timeout_seconds=60)
    action, _, owner, child, *_ = exchange
    code = host.draft_invocation(request, target, payload, owner=owner)
    receipt = host.publish_draft(request, target, payload, owner=owner)
    output = {'exit_code': 0, 'output': json.dumps(receipt)}
    if damage == 'wrong_slot':
        registered = json.loads(host.registry_file(owner).read_text())
        child = Path(registered['reviewers'][1]['child_source']['session_file'])
    elif damage == 'expired':
        monkeypatch.setattr(host.time, 'time', lambda: json.loads(request.read_text())['expires_at'])
    elif damage == 'frozen':
        material = action.with_name('material.json')
        material.write_bytes(material.read_bytes() + b'\n')
    elif damage == 'failed_output':
        output['exit_code'] = 1
    elif damage == 'changed_file':
        payload['items'][0]['conclusion'] = 'Changed after publication'
        host.guard.write(target, payload)
    else:
        code = code.replace(str(request), str(request.with_name('other-request.json')))
    line = native_call(child, 'functions.exec', code, 'rejected-publication', output)
    with pytest.raises(ValueError):
        host.submit(request, target, owner=owner, source=child, source_line=line)
    assert not host._submission_file(request).exists()
    assert not action.with_name('execution-receipt.json').exists()
