"""Offline native I/O proof through Worker and preview, never financial acceptance."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading

import fitz

from apps import json_artifact
from . import codex_review_worker as worker, flow_engine, flow_evals, read_material
from . import review_batch_report, review_input, runtime_maintenance

ROOT = Path(__file__).resolve().parents[2]
VERSION = 'codex-cli 0.160.0'
PLACEHOLDER = 'native-contract-placeholder'


def _sha(path: Path) -> str:
    return review_input._binary_sha256(path)


def _identity(binary: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix='oa-native-identity-') as temporary:
        home = Path(temporary)
        (home / 'codex').mkdir()
        version = subprocess.run([str(binary), '--version'], env={'HOME': str(home), 'CODEX_HOME': str(home / 'codex'), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    paths = [*ROOT.glob('apps/**/*.py'), ROOT / 'scripts/quality.py', ROOT / 'scripts/review_experiment_host.py', ROOT / 'requirements-dev.txt']
    paths.extend((ROOT / '.agents/skills/oa-finance-preaudit').rglob('*.md'))
    return {'cli_path': str(binary), 'cli_sha256': _sha(binary), 'cli_version': version,
            'source_files': {str(p.relative_to(ROOT)): _sha(p) for p in sorted(paths)},
            'call_profiles': {name: worker._codex_command(Path('{PROJECT}'), Path('{SCHEMA}'), Path('{DECISION}'), **options)
                              for name, options in [('direct', {'model_instructions_file': Path('{WORKER_ROLE}')}), ('fallback', {'tool_output_token_limit': review_input._FALLBACK_TOOL_OUTPUT_TOKENS, 'model_instructions_file': Path('{WORKER_ROLE}')})]}}


def readiness(record: Path) -> dict:
    """Recheck existing proof without a model turn; callers still own authorization."""
    try:
        record = Path(record).resolve()
        if record.stat().st_mode & 0o077:
            raise ValueError('record_not_private')
        value = json.loads(record.read_text())
        binary = review_input._native_codex_file()
        if (binary is None or value.get('schema_version') != 1 or value['status'] != 'verified'
                or value['binding'] != _identity(binary) or value['binding']['cli_version'] != VERSION
                or not _verified_cases(value['cases']) or not value['artifacts']):
            raise ValueError('native_contract_binding_changed')
        if not all(Path(p).is_file() and _sha(Path(p)) == digest for p, digest in value['artifacts'].items()):
            raise ValueError('native_contract_evidence_changed')
        return {'status': 'verified', 'paid_dispatch_ready': True, 'record_sha256': _sha(record)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, subprocess.SubprocessError):
        return {'status': 'not_verified', 'paid_dispatch_ready': False, 'reason': 'native_contract_not_current'}


def _forbidden(*_args, **_kwargs):
    raise AssertionError('OA and submission are forbidden in native contract checks')


def _verified_cases(cases: dict) -> bool:
    positive, negative = cases['fallback'], cases['default_truncation']
    return (positive['history_limit'] == 60000 and positive['model_context']['exact_stdout']
            and positive['model_context']['rollout_matches_next_request'] and positive['cli_stdout_complete']
            and positive['tool_images_match_fixture']
            and len(positive['tool_image_sha256']) == 2 and len(set(positive['tool_image_sha256'])) == 2
            and positive['rollout_tool_image_sha256'] == positive['tool_image_sha256']
            and positive['consumer_preserved_original_object'] and positive['preview'] == 'verified_closed'
            and negative['status'] == 'not_verified' and negative['model_context']['truncated']
            and not negative['model_context']['exact_stdout']
            and negative['model_context']['rollout_matches_next_request'] and negative['cli_stdout_complete']
            and negative['tool_images_match_fixture']
            and all(cases[name]['history_limit'] is None for name in ('direct', 'other_workflow', 'default_truncation', 'invalid_json'))
            and cases['direct']['inline_input_complete'] and cases['direct']['initial_images_match_fixture']
            and all(cases[name]['preview'] == 'verified_closed' for name in ('direct', 'other_workflow'))
            and cases['invalid_json']['invalid_original_rejected']
            and all(cases[name]['bounded_role_delivered'] for name in ('fallback', 'default_truncation', 'direct', 'invalid_json'))
            and all(c['process_state'] == 'exited' and c['native_raw_equals_vendor'] and not c['sample_eligible'] for c in cases.values()))


def _fixture(root: Path, *, long: bool, workflow: str):
    project = root / 'project'
    project.mkdir()
    # Only current application code is reachable from the neutral tool working directory.
    (project / 'apps').symlink_to(ROOT / 'apps', target_is_directory=True)
    fields = [{'path': f'detail[0].row[{i}].field[0]', 'value': f'CANARY_FIELD_{i:04d}|' + '0123456789' * 14,
               'read_status': 'synthetic', 'nullable': None, 'flag': False} for i in range(798 if long else 2)]
    fields[0]['value'] = 'BEGIN_CANARY|' + fields[0]['value']
    fields[-1]['value'] += '|END_CANARY'

    def collect(request, attempt):
        resources, images, manifest = [], [], []
        for index in range(2 if long else 1):
            resource = 'neutral' if index == 0 else 'neutral-second'
            page = attempt / 'prepared' / f'canary-{index}.png'
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), False)
            pix.clear_with(255 if index == 0 else 127)
            page.write_bytes(pix.tobytes('png'))
            source = attempt / 'attachments' / page.name
            source.write_bytes(page.read_bytes())
            resources.append({'request_id': request, 'resource_id': resource, 'source_resource_id': resource,
                              'local_readonly_path': str(source), 'owner_detail_id': f'neutral-row-{index}', 'source_system': 'oa'})
            images.append({'resource_id': resource, 'path': str(page), 'kind': 'image', 'page_number': 1, 'content_sha256': _sha(page)})
            manifest.append({'resource_id': resource, 'status': 'image', 'image_count': 1})
        return {'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request,
                'workflow_key': workflow, 'workflow_name': 'Neutral native contract', 'declared_fields': fields,
                'resources': resources,
                'prepared': {'images': images, 'structured_contents': [], 'manifest': manifest},
                'authority_facts': [], 'warnings': []}

    engine = flow_engine.FlowEngine(root=project / 'runtime/finance_preaudit_flows',
        discover_candidates=lambda key: [{'request_id': 'neutral-native-contract', 'workflow_key': key, 'received_at': '2026-10-01T00:00:00+00:00'}],
        collect_material=collect, submit_frozen=_forbidden, recover_frozen=_forbidden)
    action = engine.next_action(workflow)
    if action['action'] != 'review_required':
        raise ValueError('neutral_fixture_not_prepared:' + json.dumps({k: action[k] for k in ('action', 'reason', 'error_code', 'failure_detail_code', 'failure_details') if k in action}))
    return engine, {'project_root': str(project), 'action_file': action['action_file'], 'timeout_seconds': 60}


def _events(item: dict, schema: dict, *, tool: bool) -> bytes:
    response = {'id': 'neutral-response-' + item['id'], 'object': 'response', 'status': 'completed',
                'model': worker.MODEL, 'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
                'text': {'format': {'type': 'json_schema', 'name': 'codex_output_schema', 'strict': True, 'schema': schema}}}
    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
              {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', **({'arguments': ''} if tool else {'content': []})}}]
    if tool:
        events.extend([{'type': 'response.function_call_arguments.delta', 'item_id': item['id'], 'output_index': 0, 'delta': item['arguments']},
                       {'type': 'response.function_call_arguments.done', 'item_id': item['id'], 'output_index': 0, 'arguments': item['arguments']}])
    else:
        text = item['content'][0]['text']
        events.extend([{'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                       {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'delta': text},
                       {'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'text': text},
                       {'type': 'response.content_part.done', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': item['content'][0]}])
    events.extend([{'type': 'response.output_item.done', 'output_index': 0, 'item': item}, {'type': 'response.completed', 'response': response}])
    return ''.join('event: ' + q['type'] + '\ndata: ' + json.dumps(q) + '\n\n' for q in events).encode()


def _case(root: Path, binary: Path, name: str) -> dict:
    root.mkdir(mode=0o700)
    long = name in {'fallback', 'default_truncation'}
    workflow = 'general_expense' if name == 'other_workflow' else 'business_entertainment'
    engine, task = _fixture(root, long=long, workflow=workflow)
    action = Path(task['action_file'])
    packet = json.loads(action.with_name('review-packet.json').read_text())
    frozen = review_input.prepare(action) if workflow == 'business_entertainment' else None
    schema = worker._decision_schema(action)
    obj = {k: v['const'] for k, v in schema['properties'].items() if 'const' in v}
    obj.update(overall='needs_human', items=[{'item_key': i['item_key'], 'status': 'uncertain', 'conclusion': 'Neutral mechanism check requires no financial finding.',
              'evidence_refs': ['material:prepared.images.neutral.page-1'], 'missing_evidence': ['Synthetic material only.']} for i in packet['items']])
    vendor_text = ('Neutral prefix\n' if name == 'invalid_json' else '') + json.dumps(obj, ensure_ascii=False)
    requests, errors = [], []
    reader_cmd = 'python3 -B -m apps.oa_finance_preaudit.read_material ' + shlex.quote(str(action))
    pages = json.loads(action.with_name('material.json').read_text())['prepared']['images']
    request_count = 2 + len(pages) if long else 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                q = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/responses' or len(requests) >= request_count:
                    raise ValueError('unexpected_local_request')
                if self.headers.get('Authorization') != 'Bearer ' + PLACEHOLDER:
                    raise ValueError('unexpected_authentication')
                if q.get('model') != worker.MODEL or q.get('reasoning', {}).get('effort') != worker.REASONING_EFFORT:
                    raise ValueError('model_profile_changed')
                if q.get('text', {}).get('format', {}).get('schema') != schema:
                    raise ValueError('schema_changed')
                requests.append(q)
                tool = long and len(requests) < request_count
                if tool and len(requests) == 1:
                    item = {'id': 'neutral-tool', 'type': 'function_call', 'status': 'completed', 'name': 'exec_command', 'call_id': 'neutral-reader',
                            'arguments': json.dumps({'cmd': reader_cmd, 'max_output_tokens': 60000, 'yield_time_ms': 1000})}
                elif tool:
                    index = len(requests) - 2
                    item = {'id': f'neutral-view-{index}', 'type': 'function_call', 'status': 'completed', 'name': 'view_image',
                            'call_id': f'neutral-view-{index}', 'arguments': json.dumps({'path': pages[index]['path']})}
                else:
                    item = {'id': 'neutral-final', 'type': 'message', 'status': 'completed', 'role': 'assistant', 'phase': 'final_answer',
                            'content': [{'type': 'output_text', 'text': vendor_text, 'annotations': []}]}
                body = _events(item, schema, tool=tool)
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
    home, bin_dir, audit = root / 'home', root / 'bin', root / 'reviewer'
    for path in (home, bin_dir, audit):
        path.mkdir(mode=0o700)
    (bin_dir / 'codex').symlink_to(binary)
    isolated = {'HOME': str(home), 'PATH': str(bin_dir) + ':' + str(Path(sys.executable).parent) + ':/usr/bin:/bin', 'OA_REVIEW_API_KEY': PLACEHOLDER}
    executions = []
    diagnostics = {}

    def executor(project, file, seconds):
        if executions:
            raise ValueError('native_check_retry_forbidden')
        execution = worker._execute_codex_raw(project, file, seconds, read_only=True, audit_directory=audit, stderr_summary=diagnostics,
            prompt_builder=(lambda _: worker._prompt(file, reading_instruction=frozen.instruction)) if frozen else worker._prompt,
            schema_builder=lambda _: schema, frozen_input=None if name == 'default_truncation' else frozen,
            offline_api=(f'http://127.0.0.1:{server.server_port}', isolated))
        executions.append(execution)
        return execution

    try:
        reviewed = worker.run_read_only(task, executor=executor)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    json_artifact.write_json_atomic(root / 'requests.json', requests)
    if errors or len(requests) != request_count or not executions or executions[0].process_state != 'exited':
        raise ValueError('native_check_execution_unverified')
    closed = None
    if reviewed['status'] == 'completed':
        closed = engine.complete_review(action, audit / 'decision.json')
    preview = 'verified_closed' if closed and closed['action'] == 'closed' and closed['write_state'] == 'not_requested' else 'rejected'
    acceptance = flow_evals.build_acceptance_report(runtime_maintenance.status(action.parents[4])['case_metrics'], replay_report={}, cli_samples=[])
    json_artifact.write_json_atomic(root / 'acceptance.json', acceptance)
    output = ''
    rollout_output = ''
    rollout_images = []
    for f in (audit / 'codex-home').rglob('*.jsonl'):
        for line in f.read_text().splitlines():
            value = json.loads(line).get('payload', {})
            if value.get('type') == 'function_call_output' and value.get('call_id') == 'neutral-reader':
                rollout_output = value['output']
            if (value.get('type') == 'function_call_output'
                    or value.get('type') == 'message' and value.get('role') == 'user'):
                rollout_images.extend(_image_hashes([value]))
    if long:
        output = next(q['output'] for q in requests[-1]['input'] if q.get('type') == 'function_call_output' and q.get('call_id') == 'neutral-reader')
    initial_images = _image_hashes(requests[0]['input'])
    expected_images = [_sha(Path(page['path'])) for page in pages]
    tool_images = _image_hashes(requests[-1]['input']) if long else []
    tool_images_match = long and tool_images == expected_images and all(
        _image_hashes(requests[index + 2]['input']) == expected_images[:index + 1] for index in range(len(pages)))
    stdout = read_material.read(action) if long else ''
    field_section = stdout.split('[material.declared_fields]\n', 1)[1].split('\n[material.', 1)[0] if stdout else ''
    rows = [line for line in field_section.splitlines() if re.match(r'^\d+: ', line)]
    cli_outputs = [q['item'] for q in map(json.loads, (audit / 'events.jsonl').read_text().splitlines()) if q.get('type') == 'item.completed' and q.get('item', {}).get('type') == 'command_execution']
    raw = (audit / 'decision.json').read_bytes()
    command = diagnostics['offline_native_command']
    configuration = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg in {'--config', '-c'}]
    limits = [int(q.split('=', 1)[1]) for q in configuration if q.startswith('tool_output_token_limit=')]
    request_text = '\n'.join(part.get('text', '') for message in requests[0]['input'] for part in message.get('content', []) if part.get('type') == 'input_text')
    result = {'process_state': reviewed['process_state'], 'preview': preview,
              'bounded_role_delivered': bool(workflow == 'business_entertainment'
                  and (ROOT / '.agents/skills/oa-finance-preaudit/references/worker-role.md').read_text().strip() in requests[0].get('instructions', '')
                  and 'Before making tool calls, send a brief preamble' not in requests[0].get('instructions', '')),
              'consumer_preserved_original_object': bool(closed and json.loads(Path(closed['decision_file']).read_text()) == obj),
              'invalid_original_rejected': name == 'invalid_json' and reviewed['status'] == 'failed' and reviewed['decision_state'] == 'unreadable' and raw == vendor_text.encode(),
              'model_context': {'exact_stdout': bool(stdout and stdout in output), 'field_rows_matched': sum(line in output.splitlines() for line in rows),
                                'truncated': 'truncated output' in output.lower(), 'rollout_matches_next_request': output == rollout_output,
                                'stdout_bytes': len(stdout.encode()), 'next_request_output_bytes': len(output.encode()),
                                'head_marker_present': 'BEGIN_CANARY' in output, 'tail_marker_present': 'END_CANARY' in output,
                                'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                                'next_request_output_sha256': hashlib.sha256(output.encode()).hexdigest(),
                                'rollout_output_sha256': hashlib.sha256(rollout_output.encode()).hexdigest()},
              'cli_stdout_complete': bool(stdout and cli_outputs and stdout in cli_outputs[0]['aggregated_output'] and cli_outputs[0]['exit_code'] == 0),
              'initial_image_sha256': initial_images, 'initial_images_match_fixture': initial_images == expected_images,
              'tool_image_sha256': tool_images, 'rollout_tool_image_sha256': rollout_images if long else [],
              'tool_images_match_fixture': tool_images_match,
              'image_evidence_scope': 'Encoded-byte delivery and order only; not image understanding or financial acceptance.',
              'default_history_budget': name in {'direct', 'other_workflow', 'default_truncation', 'invalid_json'},
              'history_limit': limits[0] if limits else None,
              'inline_input_complete': bool(frozen and not long and review_input.render_inline(frozen.bundle) in request_text),
              'native_raw_equals_vendor': raw == vendor_text.encode(), 'native_final_sha256': hashlib.sha256(raw).hexdigest(),
              'sample_eligible': acceptance['sample_eligible']}
    result['status'] = 'verified' if not long or (result['model_context']['exact_stdout'] and tool_images_match) else 'not_verified'
    result['paid_dispatch_ready'] = result['status'] == 'verified'
    return result


def _image_hashes(messages: list[dict]) -> list[str]:
    hashes = []
    for message in messages:
        parts = message.get('content', [])
        if message.get('type') == 'function_call_output' and isinstance(message.get('output'), list):
            parts = message['output']
        for part in parts:
            if part.get('type') == 'input_image':
                raw = base64.b64decode(part['image_url'].split(',', 1)[1], validate=True)
                hashes.append(hashlib.sha256(raw).hexdigest())
    return hashes


def check(record: Path) -> dict:
    record = Path(record).absolute()
    if (record != record.resolve() or record.is_relative_to(ROOT) or record.exists()
            or not record.parent.is_dir() or record.parent.stat().st_mode & 0o077):
        raise ValueError('native_contract_record_target_invalid')
    binary = review_input._native_codex_file()
    result = {'schema_version': 1, 'status': 'not_verified', 'reason': 'native_cli_unavailable', 'paid_dispatch_ready': False}
    root = None
    if binary is not None:
        try:
            binding = _identity(binary)
            result['binding'] = binding
            if binding['cli_version'] != VERSION:
                raise ValueError('native_cli_version_unverified')
            root = Path(tempfile.mkdtemp(prefix='native-contract-', dir=record.parent))
            cases = {name: _case(root / name, binary, name) for name in ('fallback', 'default_truncation', 'direct', 'other_workflow', 'invalid_json')}
            if not _verified_cases(cases):
                raise ValueError('native_review_contract_failed')
            result.update(status='verified', reason=None, paid_dispatch_ready=True, cases=cases,
                          evidence_scope='loopback synthetic native I/O, not financial/provider acceptance; not OS full network isolation')
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, StopIteration, subprocess.SubprocessError) as error:
            result['reason'] = str(error)
    if root is not None:
        for path in root.rglob('*'):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
        result['artifacts'] = {str(p): _sha(p) for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
    review_batch_report.write_private_report(result, record, evidence_roots=(ROOT,))
    return result
