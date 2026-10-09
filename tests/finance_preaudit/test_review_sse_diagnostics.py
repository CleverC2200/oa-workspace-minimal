"""Opt-in diagnostic delivery through the existing reviewer/preview interface."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from apps.oa_finance_preaudit import codex_review_worker as worker
from apps.oa_finance_preaudit import review_experiment
from tests.finance_preaudit.test_entertainment_input_delivery import prepare, reviewer, close
from tests.finance_preaudit.test_review_experiment import sources, arm, fake_cli, sha, BASE, ROOT


TRACE_ENV = 'OA_REVIEW_SSE_TRACE'
FIXED_TARGET = 'error,codex_api::sse=trace'


def controlled_reviewer(monkeypatch, tmp_path, task, *, traced, prefix=False):
    calls = reviewer(monkeypatch, tmp_path, task, image_count=0)
    script = tmp_path / 'reviewer.py'
    script.write_text('''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2]==['debug','prompt-input']:
 print('[]');sys.exit(0)
prompt=sys.stdin.read()
assert os.environ.get('RUST_LOG')==''' + repr(FIXED_TARGET if traced else None) + '''
assert not os.environ.get('OA_REVIEW_SSE_TRACE')
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
constants={k:v['const'] for k,v in schema['properties'].items() if 'const' in v}
action=json.loads(Path(''' + repr(task['action_file']) + ''').read_text())
packet=json.loads(Path(action['review_packet_file']).read_text())
decision={**constants,'overall':'needs_human','items':[{
 'item_key':item['item_key'],'status':'uncertain','conclusion':'离线资料未核实。',
 'evidence_refs':[],'missing_evidence':['离线缺口，非真实业务验收。']
} for item in packet['items']]}
raw=('MODEL_PREFIX_CANARY\\n' if ''' + repr(prefix) + ''' else '')+json.dumps(decision,ensure_ascii=False)
if ''' + repr(traced) + ''':
 print('TRACE codex_api::sse: SSE event: '+json.dumps({'type':'response.output_text.done','text':raw}),file=sys.stderr)
Path(args[args.index('--output-last-message')+1]).write_text(raw)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':raw}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}))
''')
    return calls


def review_with_archive(task, audit, *, read_only=True):
    def execute(project, action, seconds):
        return worker._execute_codex_raw(
            project, action, seconds, schema_builder=worker._decision_schema,
            read_only=read_only, audit_directory=audit, stderr_summary={},
        )
    return worker.review(task, executor=execute)


def test_read_only_private_archive_can_opt_in_to_fixed_sse_trace(tmp_path, monkeypatch):
    engine, task = prepare(tmp_path, page_count=0)
    audit = tmp_path / 'private-audit'
    audit.mkdir(mode=0o700)
    monkeypatch.setenv(TRACE_ENV, '1')
    monkeypatch.setenv('RUST_LOG', 'unsafe-broad-trace')
    calls = controlled_reviewer(monkeypatch, tmp_path, task, traced=True)
    reviewed = review_with_archive(task, audit)
    closed = close(engine, task, reviewed)
    assert closed['action'] == 'closed'
    assert closed['write_state'] == 'not_requested'
    assert len(calls) == 1
    assert 'SSE event:' in (audit / 'stderr.log').read_text()
    assert json.loads((audit / 'decision.json').read_text()) == reviewed['decision']


@pytest.mark.parametrize('context', ['not_read_only', 'missing_archive', 'public_archive', 'inside_workspace'])
def test_requested_trace_refuses_non_private_or_non_read_only_review(tmp_path, monkeypatch, context):
    _engine, task = prepare(tmp_path, page_count=0)
    audit = Path(task['project_root']) / 'audit' if context == 'inside_workspace' else tmp_path / 'audit'
    audit.mkdir(mode=0o755 if context == 'public_archive' else 0o700)
    audit.chmod(0o755 if context == 'public_archive' else 0o700)
    monkeypatch.setenv(TRACE_ENV, '1')
    calls = controlled_reviewer(monkeypatch, tmp_path, task, traced=True)
    reviewed = review_with_archive(
        task, None if context == 'missing_archive' else audit,
        read_only=context != 'not_read_only',
    )
    assert reviewed['status'] == 'failed'
    assert reviewed['error_code'] == 'codex_review_task_invalid'
    assert calls == []
    assert not (audit / 'events.jsonl').exists()


@pytest.mark.parametrize('setting', [None, '0'])
def test_default_and_disabled_review_do_not_forward_ambient_rust_logging(tmp_path, monkeypatch, setting):
    engine, task = prepare(tmp_path, page_count=0)
    audit = tmp_path / 'audit'
    audit.mkdir(mode=0o700)
    if setting is None:
        monkeypatch.delenv(TRACE_ENV, raising=False)
    else:
        monkeypatch.setenv(TRACE_ENV, setting)
    monkeypatch.setenv('RUST_LOG', 'trace')
    controlled_reviewer(monkeypatch, tmp_path, task, traced=False)
    closed = close(engine, task, review_with_archive(task, audit, read_only=False))
    assert closed['action'] == 'closed'
    assert (audit / 'stderr.log').read_text() == ''


def test_sse_trace_preserves_prefix_failure_and_exact_original_message(tmp_path, monkeypatch):
    _engine, task = prepare(tmp_path, page_count=0)
    audit = tmp_path / 'audit'
    audit.mkdir(mode=0o700)
    monkeypatch.setenv(TRACE_ENV, '1')
    controlled_reviewer(monkeypatch, tmp_path, task, traced=True, prefix=True)
    reviewed = review_with_archive(task, audit)
    assert reviewed['error_code'] == 'codex_decision_invalid'
    assert reviewed['token_usage']['status'] == 'recorded'
    assert reviewed['token_usage']['total_tokens'] == 15
    raw = (audit / 'decision.json').read_text()
    logged = json.loads((audit / 'stderr.log').read_text().split('SSE event: ', 1)[1])
    events = [json.loads(line) for line in (audit / 'events.jsonl').read_text().splitlines()]
    native = next(event['item']['text'] for event in events if event.get('item', {}).get('type') == 'agent_message')
    assert logged['text'] == native == raw
    assert raw.startswith('MODEL_PREFIX_CANARY\n')
    assert 'decision' not in reviewed


@pytest.mark.parametrize('execute', [False, True])
def test_existing_source_host_transfers_only_the_execute_opt_in_flag(tmp_path, monkeypatch, execute):
    import hashlib

    candidate = tmp_path / 'candidate'
    candidate.mkdir(mode=0o700)
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'runtime_files': {}, 'host_sha256': hashlib.sha256(review_experiment.HOST.read_bytes()).hexdigest(),
        'arms': {'candidate': str(candidate)}, 'timeout_seconds': 60}))
    plan.chmod(0o600)
    monkeypatch.setenv(TRACE_ENV, '1')
    monkeypatch.setenv('RUST_LOG', 'trace')
    seen = []

    class SourceHost:
        returncode = 0

        def communicate(self, timeout):
            return '{}', ''

        def poll(self):
            return 0

    def start(command, **kwargs):
        seen.append(kwargs['env'])
        return SourceHost()

    monkeypatch.setattr(review_experiment.subprocess, 'Popen', start)
    assert review_experiment._host(plan, 'candidate', execute=execute) == {}
    assert seen[0].get(TRACE_ENV) == ('1' if execute else None)
    assert 'RUST_LOG' not in seen[0]


@pytest.mark.parametrize('prefix', [False, True])
def test_source_bound_execute_retains_trace_and_original_contract(tmp_path, sources, monkeypatch, prefix):
    import subprocess

    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    before = arm(tmp_path, sources, 'baseline', 'business_entertainment', BASE)
    after = arm(tmp_path, sources, 'candidate', 'business_entertainment', revision)
    cli = fake_cli(tmp_path)
    script = cli.read_text().replace('prompt=sys.stdin.read()',
        "prompt=sys.stdin.read()\nassert os.environ.get('RUST_LOG')=='error,codex_api::sse=trace'\nassert not os.environ.get('OA_REVIEW_SSE_TRACE')")
    script = script.replace("Path(args[args.index('--output-last-message')+1]).write_text(raw)",
        "raw=" + repr('MODEL_PREFIX_CANARY\n' if prefix else '') + "+raw\nprint('TRACE codex_api::sse: SSE event: '+json.dumps({'type':'response.output_text.done','text':raw}),file=sys.stderr)\nPath(args[args.index('--output-last-message')+1]).write_text(raw)")
    cli.write_text(script)
    plan = tmp_path / 'plan.json'
    review_experiment.freeze_pair(before, after, plan, repository=ROOT, reviewer=cli,
        purpose='diagnostic', timeout_seconds=60, maximum_cli_starts=2)
    # This task-local test claim never authorizes OA or an installed paid CLI.
    review_experiment._save(plan.with_name('plan.json.started.json'), {'plan_sha256': sha(plan), 'maximum_cli_starts': 1})
    monkeypatch.setenv(TRACE_ENV, '1')
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    result = review_experiment._host(plan, 'candidate', execute=True)
    raw = (after / 'reviewer/decision.json').read_text()
    logged = json.loads((after / 'reviewer/stderr.log').read_text().split('SSE event: ', 1)[1])
    assert logged['text'] == raw
    assert result['cli_starts'] == 1  # The controlled Python CLI, not a paid model.
    assert result['write_state'] == 'not_requested'
    assert result['preview'] == ('not_closed' if prefix else 'closed')
    assert result['execution']['error_code'] == ('codex_decision_invalid' if prefix else None)
    assert not (before / 'cli-start.json').exists()
