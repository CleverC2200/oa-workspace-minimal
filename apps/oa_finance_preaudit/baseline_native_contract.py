"""Externally verified observations of an immutable production review protocol.

This verifier grants neither full-input proof nor model or OA authorization.
The child loads application code only from the explicitly frozen baseline.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
VERSION = 'codex-cli 0.160.0'
PLACEHOLDER = 'native-contract-placeholder'


def _sha(path: Path) -> str:
    if path != path.resolve() or not path.is_file():
        raise ValueError('baseline_contract_path_invalid')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(files: dict) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _save(path: Path, value: dict) -> None:
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def _version(binary: Path) -> str:
    _sha(binary)
    with tempfile.TemporaryDirectory(prefix='oa-baseline-identity-') as temporary:
        home = Path(temporary)
        (home / 'codex').mkdir()
        return subprocess.check_output([str(binary), '--version'], text=True, timeout=10,
            env={'HOME': str(home), 'CODEX_HOME': str(home / 'codex'), 'PATH': '/usr/bin:/bin'}).strip()


def _source_files(source: Path, repository: Path, revision: str, expected: dict | None = None) -> dict:
    if source != source.resolve() or len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('baseline_source_invalid')
    names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', revision], cwd=repository, text=True).splitlines()
    required = {p for p in names if p.startswith('apps/') and p.endswith('.py')
                or p.startswith('apps/oa_approval/configs/') and p.endswith('.json')
                or p in {'AGENTS.md', 'requirements-dev.txt'}}
    required.update('.agents/skills/oa-finance-preaudit/' + p for p in
        ('SKILL.md', 'references/result-contract.md', 'references/observation-contract.md',
         'references/flow-engine-contract.md', 'references/flows/business-entertainment.md'))
    selected = (set(expected) if expected is not None else
        required | {p for p in names if p.startswith('.agents/skills/oa-finance-preaudit/') and p.endswith('.md')})
    if not {'AGENTS.md', 'requirements-dev.txt', 'apps/oa_finance_preaudit/codex_review_worker.py',
            'apps/oa_finance_preaudit/flow_engine.py'}.issubset(selected) or not required.issubset(selected):
        raise ValueError('baseline_source_incomplete')
    files = {}
    for name in sorted(selected):
        if name not in names or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('baseline_source_invalid')
        digest = hashlib.sha256(subprocess.check_output(['git', 'show', revision + ':' + name], cwd=repository)).hexdigest()
        if _sha(source / name) != digest or expected is not None and expected[name] != digest:
            raise ValueError('baseline_source_changed')
        files[name] = digest
    return files


def _child(source: Path, *, mode: str, root: Path | None = None, binary: Path | None = None,
           case: str | None = None) -> dict:
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, 'pthread_sigmask'):
        raise ValueError('baseline_cancellation_guard_unavailable')
    command = [sys.executable, '-B', str(Path(__file__).resolve()), mode, str(source)]
    if mode == 'case':
        command.extend((str(root), str(binary), str(case)))
    process = None
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    previous = signal.getsignal(signal.SIGTERM)

    def cancel(signum, _frame):
        if process is not None:
            process.terminate()
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, cancel)
        try:
            process = subprocess.Popen(command, cwd=source, env={'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin',
                'PYTHONDONTWRITEBYTECODE': '1'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        output, errors = process.communicate(timeout=90)
    finally:
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
        finally:
            signal.signal(signal.SIGTERM, previous)
    if process.returncode:
        if root is not None and root.is_dir():
            (root / 'child-failure.txt').write_text(errors)
        raise ValueError('baseline_source_child_failed')
    return json.loads(output)


def _binding(source: Path, revision: str, files: dict, host: Path, binary: Path) -> dict:
    return {'source_revision': revision, 'source_files': files, 'source_snapshot_sha256': _snapshot(files),
            'call_profiles': _child(source, mode='inspect'),
            'cli_path': str(binary), 'cli_sha256': _sha(binary), 'cli_version': _version(binary),
            'host_path': str(host), 'host_sha256': _sha(host),
            'verifier_files': {str(p): _sha(p) for p in (Path(__file__).resolve(), Path(__file__).with_name('native_review_protocol.py').resolve())}}


def _verified_cases(cases: dict) -> bool:
    observed, invalid = cases['default_reading'], cases['invalid_json']
    context = observed['model_context']
    return (set(cases) == {'default_reading', 'invalid_json'}
            and observed['history_limit'] is None and context['truncated'] is True
            and context['exact_stdout'] is False and context['rollout_matches_next_request'] is True
            and observed['cli_stdout_complete'] is True and observed['preview'] == 'verified_closed'
            and observed['consumer_preserved_original_object'] is True
            and observed['tool_image_sha256'] == observed['fixture_image_sha256']
            and len(set(observed['tool_image_sha256'])) == 2
            and observed['rollout_tool_image_sha256'] == observed['tool_image_sha256']
            and observed['initial_images'] == [] and invalid['invalid_original_rejected'] is True
            and invalid['history_limit'] is None and invalid['preview'] == 'rejected'
            and all(c['process_state'] == 'exited' and c['native_raw_equals_vendor'] is True
                    and c['input_complete'] is False and c['sample_eligible'] is False for c in cases.values()))


def _unverified(reason: str) -> dict:
    return {'status': 'not_verified', 'baseline_comparison_ready': False,
            'paid_dispatch_ready': False, 'input_complete': False,
            'sample_eligible': False, 'reason': reason}


def readiness(record: Path, *, source: Path, source_revision: str,
              host_file: Path, binary: Path, source_files: dict | None = None) -> dict:
    """Validate a baseline observation without granting execution authorization."""
    try:
        record, source, host_file, binary = map(Path, (record, source, host_file, binary))
        if record != record.resolve() or record.stat().st_mode & 0o077:
            raise ValueError('baseline_record_not_private')
        value = json.loads(record.read_text())
        binding = value['binding']
        for name, expected in binding['source_files'].items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or _sha(source / name) != expected:
                raise ValueError('baseline_source_changed')
        if (value.get('schema_version') != 1 or value.get('status') != 'verified_baseline_observation'
                or value.get('baseline_comparison_ready') is not True
                or any(value.get(k) is not False for k in ('paid_dispatch_ready', 'input_complete', 'sample_eligible'))
                or not _verified_cases(value['cases']) or not binding['source_files']
                or source_files is not None and binding['source_files'] != source_files
                or binding != _binding(source, source_revision, binding['source_files'], host_file, binary)
                or binding['cli_version'] != VERSION or not value['artifacts']):
            raise ValueError('baseline_contract_binding_changed')
        if not all(_sha(Path(p)) == digest and not Path(p).stat().st_mode & 0o077 for p, digest in value['artifacts'].items()):
            raise ValueError('baseline_evidence_changed')
        return {'status': 'verified_baseline_observation', 'baseline_comparison_ready': True,
                'paid_dispatch_ready': False, 'input_complete': False, 'sample_eligible': False,
                'binding': binding, 'record_sha256': _sha(record)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, subprocess.SubprocessError):
        return _unverified('baseline_contract_not_current')


def check(record: Path, *, source: Path, repository: Path, source_revision: str,
          host_file: Path, binary: Path, source_files: dict | None = None) -> dict:
    """Run two neutral baseline cases through its Worker and original consumer."""
    record, source, repository, host_file, binary = map(Path, (record, source, repository, host_file, binary))
    if (record != record.resolve() or record.is_relative_to(ROOT) or record.exists()
            or not record.parent.is_dir() or record.parent.stat().st_mode & 0o077):
        raise ValueError('baseline_contract_record_target_invalid')
    result = {'schema_version': 1, **_unverified('baseline_contract_unavailable')}
    root = None
    try:
        files = _source_files(source, repository, source_revision, source_files)
        binding = _binding(source, source_revision, files, host_file, binary)
        if binding['cli_version'] != VERSION:
            raise ValueError('baseline_cli_version_unverified')
        root = Path(tempfile.mkdtemp(prefix='baseline-native-', dir=record.parent))
        cases = {name: _child(source, mode='case', root=root / name, binary=binary, case=name)
                 for name in ('default_reading', 'invalid_json')}
        if not _verified_cases(cases):
            raise ValueError('baseline_observation_not_verified')
        if binding != _binding(source, source_revision, files, host_file, binary) or any(_sha(source / p) != h for p, h in files.items()):
            raise ValueError('baseline_binding_changed_during_check')
        result.update(status='verified_baseline_observation', baseline_comparison_ready=True,
            binding=binding, cases=cases, reason=None,
            evidence_scope='Original baseline configuration observed with synthetic loopback I/O; truncation retained, no full-input or financial acceptance.')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, subprocess.SubprocessError) as error:
        result['reason'] = str(error)
    if root is not None:
        retained = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                    and p.relative_to(root).parts[1:4] != ('reviewer', 'codex-home', '.tmp')]
        for path in retained:
            path.chmod(0o600)
        result['artifacts'] = {str(p): _sha(p) for p in retained}
    _save(record, result)
    return result


def _protocol():
    path = Path(__file__).with_name('native_review_protocol.py')
    spec = importlib.util.spec_from_file_location('oa_neutral_wire', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(source: Path, root: Path, binary: Path, name: str) -> dict:
    # This import occurs only in a fresh child after its sys.path selects baseline.
    from apps import json_artifact
    from apps.oa_finance_preaudit import codex_review_worker as worker, flow_engine, flow_evals, read_material, runtime_maintenance
    import fitz

    if not Path(worker.__file__).resolve().is_relative_to(source):
        raise ValueError('baseline_application_import_changed')
    protocol = _protocol()
    root.mkdir(mode=0o700)
    project = root / 'project'
    project.mkdir()
    (project / 'apps').symlink_to(source / 'apps', target_is_directory=True)
    long = name == 'default_reading'
    fields = [{'path': f'detail[0].row[{i}].field[0]', 'value': f'CANARY_FIELD_{i:04d}|' + '0123456789' * 14,
               'read_status': 'synthetic', 'nullable': None, 'flag': False} for i in range(798 if long else 2)]
    fields[0]['value'] = 'BEGIN_CANARY|' + fields[0]['value']
    fields[-1]['value'] += '|END_CANARY'

    def forbidden(*_args, **_kwargs):
        raise AssertionError('OA and submission forbidden in neutral checks')

    def collect(request, attempt):
        images, resources, manifest = [], [], []
        for index in range(2 if long else 1):
            resource = f'neutral-{index}'
            page = attempt / 'prepared' / f'canary-{index}.png'
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 32, 24), False)
            pix.clear_with(255 if index == 0 else 127)
            page.write_bytes(pix.tobytes('png'))
            original = attempt / 'attachments' / page.name
            original.write_bytes(page.read_bytes())
            resources.append({'request_id': request, 'resource_id': resource, 'source_resource_id': resource,
                'local_readonly_path': str(original), 'owner_detail_id': f'neutral-row-{index}', 'source_system': 'oa'})
            images.append({'resource_id': resource, 'path': str(page), 'kind': 'image', 'page_number': 1, 'content_sha256': _sha(page)})
            manifest.append({'resource_id': resource, 'status': 'image', 'image_count': 1})
        return {'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request,
                'workflow_key': 'business_entertainment', 'workflow_name': 'Neutral baseline observation',
                'declared_fields': fields, 'resources': resources,
                'prepared': {'images': images, 'structured_contents': [], 'manifest': manifest}, 'authority_facts': [], 'warnings': []}

    engine = flow_engine.FlowEngine(root=project / 'runtime/finance_preaudit_flows',
        discover_candidates=lambda key: [{'request_id': 'neutral-baseline', 'workflow_key': key,
            'received_at': '2026-10-01T00:00:00+00:00'}], collect_material=collect,
        submit_frozen=forbidden, recover_frozen=forbidden)
    action = engine.next_action('business_entertainment')
    if action['action'] != 'review_required':
        raise ValueError('baseline_neutral_fixture_failed')
    action_file = Path(action['action_file'])
    packet = json.loads(action_file.with_name('review-packet.json').read_text())
    schema = worker._decision_schema(action_file)
    obj = {k: v['const'] for k, v in schema['properties'].items() if 'const' in v}
    obj.update(overall='needs_human', items=[{'item_key': i['item_key'], 'status': 'uncertain',
        'conclusion': 'Neutral mechanism observation requires no financial finding.',
        'evidence_refs': ['material:prepared.images.neutral-0.page-1'],
        'missing_evidence': ['Synthetic material only.']} for i in packet['items']])
    vendor = ('Neutral prefix\n' if name == 'invalid_json' else '') + json.dumps(obj, ensure_ascii=False)
    pages = json.loads(action_file.with_name('material.json').read_text())['prepared']['images']
    requests, errors, commands, executions = [], [], [], []
    count = 2 + len(pages) if long else 1
    reader = 'python3 -B -m apps.oa_finance_preaudit.read_material ' + shlex.quote(str(action_file))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                q = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if (self.path != '/responses' or len(requests) >= count
                        or self.headers.get('Authorization') != 'Bearer ' + PLACEHOLDER
                        or q.get('model') != worker.MODEL
                        or q.get('reasoning', {}).get('effort') != worker.REASONING_EFFORT
                        or q.get('text', {}).get('format', {}).get('schema') != schema):
                    raise ValueError('baseline_local_request_changed')
                requests.append(q)
                tool = long and len(requests) < count
                if tool and len(requests) == 1:
                    item = {'id': 'neutral-tool', 'type': 'function_call', 'status': 'completed', 'name': 'exec_command',
                        'call_id': 'neutral-reader', 'arguments': json.dumps({'cmd': reader, 'max_output_tokens': 60000, 'yield_time_ms': 1000})}
                elif tool:
                    index = len(requests) - 2
                    item = {'id': f'neutral-view-{index}', 'type': 'function_call', 'status': 'completed', 'name': 'view_image',
                        'call_id': f'neutral-view-{index}', 'arguments': json.dumps({'path': pages[index]['path']})}
                else:
                    item = {'id': 'neutral-final', 'type': 'message', 'status': 'completed', 'role': 'assistant', 'phase': 'final_answer',
                        'content': [{'type': 'output_text', 'text': vendor, 'annotations': []}]}
                body = protocol.events(item, schema, tool=tool, model=worker.MODEL)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, KeyError, TypeError) as error:
                errors.append(str(error))
                self.send_response(400)
                self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home, audit = root / 'home', root / 'reviewer'
    home.mkdir(mode=0o700)
    audit.mkdir(mode=0o700)
    original_command = worker._codex_command
    original_profile = original_command(source, Path('{SCHEMA}'), Path('{DECISION}'))

    def command(project_root, schema_file, decision_file):
        result = original_command(project_root, schema_file, decision_file)
        expected = ('model_providers.oa_deepseek={name="DeepSeek",'
            'base_url="https://api.deepseek.com",wire_api="responses",'
            'env_key="OA_REVIEW_API_KEY",requires_openai_auth=false}')
        if result.count(expected) != 1:
            raise ValueError('baseline_provider_profile_unverified')
        result[result.index(expected)] = expected.replace('https://api.deepseek.com', f'http://127.0.0.1:{server.server_port}')
        result[0] = str(binary)
        result.insert(2, '--skip-git-repo-check')
        # The original Worker removes --ephemeral when retaining audit evidence.
        # Keep its command object so the receipt reflects that final mutation.
        commands.append(result)
        return result

    worker._codex_command = command
    # Only the synthetic key is put into this isolated child environment.
    os.environ.clear()
    os.environ.update(HOME=str(home), PATH=str(Path(sys.executable).parent) + ':/usr/bin:/bin', OA_REVIEW_API_KEY=PLACEHOLDER)

    def executor(project_root, file, seconds):
        if executions:
            raise ValueError('baseline_native_retry_forbidden')
        execution = worker._execute_codex_raw(source, file, seconds, read_only=True,
            audit_directory=audit, stderr_summary={}, prompt_builder=worker._prompt,
            schema_builder=lambda _: schema,
            decision_parser=lambda raw: worker._normalize_decision(file, worker._parse_decision_json(raw)))
        executions.append(execution)
        return execution

    try:
        reviewed = worker.run_read_only({'project_root': str(project), 'action_file': str(action_file), 'timeout_seconds': 60}, executor=executor)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    json_artifact.write_json_atomic(root / 'requests.json', requests)
    json_artifact.write_json_atomic(root / 'commands.json', {'original': original_profile, 'local': commands})
    if errors or len(requests) != count or not executions or executions[0].process_state != 'exited':
        raise ValueError('baseline_native_execution_unverified')
    raw = (audit / 'decision.json').read_bytes()
    closed = engine.complete_review(action_file, audit / 'decision.json') if reviewed['status'] == 'completed' else None
    preview = 'verified_closed' if closed and closed['action'] == 'closed' and closed['write_state'] == 'not_requested' else 'rejected'
    acceptance = flow_evals.build_acceptance_report(runtime_maintenance.status(action_file.parents[4])['case_metrics'], replay_report={}, cli_samples=[])
    json_artifact.write_json_atomic(root / 'acceptance.json', acceptance)
    rollout_output, rollout_images = '', []
    for file in (audit / 'codex-home').rglob('*.jsonl'):
        for line in file.read_text().splitlines():
            value = json.loads(line).get('payload', {})
            if value.get('type') == 'function_call_output':
                rollout_images.extend(protocol.image_hashes([value]))
                if value.get('call_id') == 'neutral-reader':
                    rollout_output = value['output']
    output = next(q['output'] for q in requests[-1]['input'] if q.get('type') == 'function_call_output' and q.get('call_id') == 'neutral-reader') if long else ''
    stdout = read_material.read(action_file) if long else ''
    events = [q['item'] for q in map(json.loads, (audit / 'events.jsonl').read_text().splitlines())
              if q.get('type') == 'item.completed' and q.get('item', {}).get('type') == 'command_execution']
    return {'process_state': executions[0].process_state, 'preview': preview,
        'consumer_preserved_original_object': bool(closed and json.loads(Path(closed['decision_file']).read_text()) == obj),
        'invalid_original_rejected': name == 'invalid_json' and reviewed['status'] == 'failed' and executions[0].decision_state == 'unreadable' and raw == vendor.encode(),
        'model_context': {'exact_stdout': bool(stdout and stdout in output), 'truncated': 'truncated output' in output.lower(),
            'rollout_matches_next_request': output == rollout_output, 'stdout_bytes': len(stdout.encode()),
            'next_request_output_bytes': len(output.encode()), 'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
            'next_request_output_sha256': hashlib.sha256(output.encode()).hexdigest()},
        'cli_stdout_complete': bool(stdout and events and stdout in events[0]['aggregated_output'] and events[0]['exit_code'] == 0),
        'tool_image_sha256': protocol.image_hashes([q for q in requests[-1]['input'] if q.get('type') == 'function_call_output']) if long else [],
        'fixture_image_sha256': [_sha(Path(p['path'])) for p in pages] if long else [],
        'rollout_tool_image_sha256': rollout_images if long else [], 'initial_images': protocol.image_hashes(requests[0]['input']),
        'history_limit': None if not any('tool_output_token_limit=' in c for c in commands[0]) else 'changed',
        'native_raw_equals_vendor': raw == vendor.encode(), 'input_complete': False,
        'sample_eligible': acceptance['sample_eligible']}


if __name__ == '__main__':
    # The parent blocks SIGTERM while assigning child ownership. Restore signal
    # delivery before the original Worker can take ownership of its CLI group.
    if hasattr(signal, 'pthread_sigmask'):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    # Script directory has no apps package; baseline is the only application root.
    source_root = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(source_root))
    from apps.oa_finance_preaudit import codex_review_worker

    if not Path(codex_review_worker.__file__).resolve().is_relative_to(source_root):
        raise ValueError('baseline_application_import_changed')
    if sys.argv[1] == 'inspect':
        result = {'original': codex_review_worker._codex_command(Path('{PROJECT}'), Path('{SCHEMA}'), Path('{DECISION}')),
                  'model': codex_review_worker.MODEL, 'reasoning_effort': codex_review_worker.REASONING_EFFORT}
    elif sys.argv[1] == 'case':
        result = _case(source_root, Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
    else:
        raise ValueError('baseline_check_mode_invalid')
    print(json.dumps(result))
