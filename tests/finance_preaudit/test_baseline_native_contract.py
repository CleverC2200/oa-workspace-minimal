"""Baseline protocol observations never grant full-input or financial qualification."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tests.finance_preaudit.test_review_experiment import sources

BASE = '280fc829e7f30d13d399790e8c8adfc7a7d5612b'
PRODUCTION = 'dd55792abf891121d8ceb4f9a8528a39b48432b6'
ROOT = Path(__file__).resolve().parents[2]


def test_missing_baseline_proof_is_not_comparison_ready(tmp_path):
    from apps.oa_finance_preaudit import baseline_native_contract as baseline

    result = baseline.readiness(tmp_path / 'missing.json', source=tmp_path,
        source_revision='0' * 40, host_file=Path(__file__), binary=tmp_path / 'missing-cli')
    assert result['status'] == 'not_verified'
    assert result['baseline_comparison_ready'] is False
    assert result['paid_dispatch_ready'] is False
    assert result['input_complete'] is False
    assert result['sample_eligible'] is False


@pytest.fixture
def local_protocol_cli(tmp_path):
    """Controlled external process, not evidence about the installed native CLI."""
    binary = tmp_path / 'fixture-cli'
    binary.write_text('#!' + sys.executable + '\n' + r'''
import base64,json,os,subprocess,sys,urllib.request
from pathlib import Path
if '--version' in sys.argv:
    print('codex-cli 0.160.0')
    raise SystemExit
args=sys.argv[1:]
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
project=Path(args[args.index('--cd')+1])
config=[args[i+1] for i,a in enumerate(args[:-1]) if a=='--config']
provider=next(c for c in config if c.startswith('model_providers.oa_deepseek='))
endpoint=provider.split('base_url="')[1].split('"')[0]+'/responses'
assert endpoint.startswith('http://127.0.0.1:')
q={'model':args[args.index('--model')+1], 'reasoning':{'effort':next(c for c in config if c.startswith('model_reasoning_effort=')).split('"')[1]},
   'text':{'format':{'schema':schema}}, 'input':[{'role':'user','content':[{'type':'input_text','text':sys.stdin.read()}]}]}
rollout=Path(os.environ['CODEX_HOME'])/'fixture-rollout.jsonl'
while True:
    request=urllib.request.Request(endpoint,json.dumps(q).encode(),headers={'Authorization':'Bearer '+os.environ['OA_REVIEW_API_KEY'],'Content-Type':'application/json'})
    body=urllib.request.urlopen(request).read().decode()
    response=[json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')][-1]['response']
    item=response['output'][0]
    if item['type']=='message':
        Path(args[args.index('--output-last-message')+1]).write_text(item['content'][0]['text'])
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'cached_input_tokens':0,'output_tokens':1}}))
        break
    arguments=json.loads(item['arguments'])
    if item['name']=='exec_command':
        run=subprocess.run(arguments['cmd'],cwd=project,shell=True,capture_output=True,text=True,check=True)
        print(json.dumps({'type':'item.completed','item':{'type':'command_execution','aggregated_output':run.stdout,'exit_code':0}}))
        result=run.stdout[:1000]+'\nWarning: truncated output\n'
    else:
        result=[{'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(Path(arguments['path']).read_bytes()).decode(),'detail':'original'}]
    output={'type':'function_call_output','call_id':item['call_id'],'output':result}
    with rollout.open('a') as stream:
        stream.write(json.dumps({'payload':output})+'\n')
    q['input'].append(output)
''')
    binary.chmod(0o700)
    return binary


@pytest.fixture
def production_source(tmp_path):
    source = tmp_path / 'production-source'
    names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', PRODUCTION], cwd=ROOT, text=True).splitlines()
    for name in names:
        if (name.startswith('apps/') and name.endswith(('.py', '.json'))
                or name.startswith('.agents/skills/oa-finance-preaudit/') and name.endswith('.md')
                or name in {'AGENTS.md', 'requirements-dev.txt'}):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(['git', 'show', PRODUCTION + ':' + name], cwd=ROOT))
    return source


def test_current_production_still_uses_its_own_default_protocol(tmp_path, production_source, local_protocol_cli):
    from apps.oa_finance_preaudit import baseline_native_contract as baseline

    source = production_source
    tmp_path.chmod(0o700)
    result = baseline.check(tmp_path / 'production-observation.json', source=source,
        repository=ROOT, source_revision=PRODUCTION,
        host_file=ROOT / 'scripts/review_experiment_host.py', binary=local_protocol_cli)
    assert result['baseline_comparison_ready'] is True, result
    assert result['binding']['source_revision'] == PRODUCTION
    assert not (source / 'apps/oa_finance_preaudit/native_review_contract.py').exists()
    assert not any('tool_output_token_limit=' in x for x in result['binding']['call_profiles']['original'])
    assert result['cases']['default_reading']['history_limit'] is None
    assert result['cases']['default_reading']['model_context']['truncated'] is True
    assert result['input_complete'] is False


def test_cancelled_baseline_child_reaps_original_workers_independent_cli(tmp_path, production_source):
    """Exercise the original Worker with a local waiting CLI and real SIGTERM."""
    driver = tmp_path / 'cancel-driver.py'
    child_pid_file, cli_pid_file = tmp_path / 'child-pid.json', tmp_path / 'cli-pid.json'
    binary = tmp_path / 'waiting-cli'
    binary.write_text('#!' + sys.executable + '\n' +
        'import json,os,time\nfrom pathlib import Path\n' +
        'Path(' + repr(str(cli_pid_file)) + ').write_text(json.dumps({"pid":os.getpid(),'
        '"parent":os.getppid(),"group":os.getpgrp()}))\n' +
        'while True: time.sleep(.1)\n')
    binary.chmod(0o700)
    driver.write_text('''import json,subprocess,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from apps.oa_finance_preaudit import baseline_native_contract as baseline
original=subprocess.Popen
def start(command,**kwargs):
 process=original(command,**kwargs)
 Path(sys.argv[5]).write_text(json.dumps(process.pid))
 return process
subprocess.Popen=start
baseline._child(Path(sys.argv[2]),mode='case',root=Path(sys.argv[3]),binary=Path(sys.argv[4]),case='invalid_json')
''')
    process = subprocess.Popen([sys.executable, '-B', str(driver), str(ROOT), str(production_source),
        str(tmp_path / 'cancel-case'), str(binary), str(child_pid_file)], cwd=ROOT,
        env={'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while not cli_pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert cli_pid_file.exists(), process.communicate(timeout=1)
        cli = json.loads(cli_pid_file.read_text())
        child_pid = json.loads(child_pid_file.read_text())
        assert cli['parent'] == child_pid
        assert cli['group'] == cli['pid'] and cli['group'] != child_pid
        process.terminate()
        assert process.wait(timeout=15) == 128 + signal.SIGTERM
        for pid in (child_pid, cli['pid']):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for receipt in (cli_pid_file, child_pid_file):
            if receipt.exists():
                value = json.loads(receipt.read_text())
                pid = value['pid'] if isinstance(value, dict) else value
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def test_source_host_reads_external_baseline_proof_with_exact_archive_binding(
        tmp_path, sources, local_protocol_cli, monkeypatch):
    from apps.oa_finance_preaudit import baseline_native_contract as baseline, review_experiment
    from tests.finance_preaudit.test_review_experiment import arm, CANDIDATE

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    tmp_path.chmod(0o700)
    before = arm(tmp_path, sources, 'baseline', 'business_entertainment', BASE)
    after = arm(tmp_path, sources, 'candidate', 'business_entertainment', CANDIDATE)
    plan_file = tmp_path / 'pair.json'
    plan = review_experiment.freeze_pair(before, after, plan_file, repository=ROOT,
        reviewer=local_protocol_cli, purpose='diagnostic', timeout_seconds=60, maximum_cli_starts=2)
    manifest = json.loads((before / 'manifest.json').read_text())
    source_files = {str(Path(p).relative_to(ROOT)): digest for p, digest in manifest['source_files'].items()}
    record = tmp_path / 'host-baseline.json'
    result = baseline.check(record, source=before / 'calling-source', repository=ROOT,
        source_revision=BASE, host_file=review_experiment.HOST,
        binary=local_protocol_cli, source_files=source_files)
    assert result['baseline_comparison_ready'] is True, result
    reused = review_experiment._host(plan_file, 'baseline', native_record=record)
    assert reused['baseline_comparison_ready'] is True
    assert reused['paid_dispatch_ready'] is False
    assert reused['binding']['source_snapshot_sha256'] == plan['preparations']['baseline']['source_snapshot_sha256']
    assert reused['binding']['source_files'] == source_files
    assert not (before / 'cli-start.json').exists()
    wrong_record = tmp_path / 'unrelated-current-proof.json'
    wrong_record.write_text(json.dumps({'status': 'verified', 'paid_dispatch_ready': True}))
    wrong_record.chmod(0o600)
    assert review_experiment._host(plan_file, 'baseline', native_record=wrong_record)['baseline_comparison_ready'] is False


def test_original_baseline_runs_without_new_worker_module_or_full_input_claim(
        tmp_path, sources, local_protocol_cli):
    from apps.oa_finance_preaudit import baseline_native_contract as baseline

    source = sources[BASE]
    assert not (source / 'apps/oa_finance_preaudit/native_review_contract.py').exists()
    record = tmp_path / 'baseline.json'
    tmp_path.chmod(0o700)
    result = baseline.check(record, source=source, repository=ROOT,
        source_revision=BASE, host_file=ROOT / 'scripts/review_experiment_host.py',
        binary=local_protocol_cli)
    assert result['status'] == 'verified_baseline_observation', result
    assert result['baseline_comparison_ready'] is True
    assert result['paid_dispatch_ready'] is False
    assert result['input_complete'] is False
    assert result['sample_eligible'] is False
    observed = result['cases']['default_reading']
    assert observed['history_limit'] is None
    assert observed['model_context']['truncated'] is True
    assert observed['model_context']['exact_stdout'] is False
    assert observed['cli_stdout_complete'] is True
    assert observed['preview'] == 'verified_closed'
    assert observed['consumer_preserved_original_object'] is True
    assert result['cases']['invalid_json']['invalid_original_rejected'] is True
    assert result['binding']['source_revision'] == BASE
    reuse = baseline.readiness(record, source=source, source_revision=BASE,
        host_file=ROOT / 'scripts/review_experiment_host.py', binary=local_protocol_cli)
    assert reuse['baseline_comparison_ready'] is True
    assert reuse['binding'] == result['binding']
    value = json.loads(record.read_text())
    for kind in ('default_budget', 'current_status', 'input_complete', 'image', 'consumer'):
        damaged = json.loads(record.read_text())
        if kind == 'default_budget':
            damaged['cases']['default_reading']['history_limit'] = 60000
        elif kind == 'current_status':
            damaged['status'] = 'verified'
            damaged['paid_dispatch_ready'] = True
        elif kind == 'input_complete':
            damaged['input_complete'] = True
        elif kind == 'image':
            damaged['cases']['default_reading']['tool_image_sha256'] = []
        else:
            damaged['cases']['default_reading']['consumer_preserved_original_object'] = False
        target = tmp_path / (kind + '.json')
        target.write_text(json.dumps(damaged))
        target.chmod(0o600)
        assert baseline.readiness(target, source=source, source_revision=BASE,
            host_file=ROOT / 'scripts/review_experiment_host.py', binary=local_protocol_cli)['baseline_comparison_ready'] is False
    moved = tmp_path / 'moved-source'
    shutil.copytree(source, moved)
    changed = moved / 'apps/oa_finance_preaudit/codex_review_worker.py'
    changed.write_text(changed.read_text() + '\n# changed\n')
    assert baseline.readiness(record, source=moved, source_revision=BASE,
        host_file=ROOT / 'scripts/review_experiment_host.py', binary=local_protocol_cli)['baseline_comparison_ready'] is False
    changed_host = tmp_path / 'other-host.py'
    changed_host.write_text('# changed host\n')
    assert baseline.readiness(record, source=source, source_revision=BASE,
        host_file=changed_host, binary=local_protocol_cli)['baseline_comparison_ready'] is False
    assert baseline.readiness(record, source=source, source_revision='0' * 40,
        host_file=ROOT / 'scripts/review_experiment_host.py', binary=local_protocol_cli)['baseline_comparison_ready'] is False
    first_evidence = Path(next(iter(value['artifacts'])))
    first_evidence.write_bytes(first_evidence.read_bytes() + b'\n')
    assert baseline.readiness(record, source=source, source_revision=BASE,
        host_file=ROOT / 'scripts/review_experiment_host.py', binary=local_protocol_cli)['baseline_comparison_ready'] is False
