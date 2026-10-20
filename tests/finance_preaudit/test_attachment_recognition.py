from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import fitz
import pytest

from apps.oa_approval import attachment_recognition as recognition


def image(tmp_path, name='one.png', width=8, height=8):
    path = tmp_path / name
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pix.clear_with(255)
    pix.save(path)
    return path


def tiff_image(path, *, frame_count):
    from PIL import Image

    frames = [Image.new('RGB', (16, 16), color) for color in ('red', 'blue')[:frame_count]]
    try:
        frames[0].save(path, format='TIFF', save_all=True, append_images=frames[1:])
    finally:
        for frame in frames:
            frame.close()
    return path


def fake_api(monkeypatch):
    calls = []
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-secret')
    def post(body, *, key, timeout):
        calls.append(body)
        return {'model': 'deepseek-flash', 'choices': [{'finish_reason': 'stop',
            'message': {'content': '读数 12345；右下角日期无法确认。'}}],
            'usage': {'total_tokens': 15}}
    monkeypatch.setattr(recognition, '_post', post)
    return calls


def fake_http(monkeypatch, respond):
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-secret')
    calls = []
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, size):
            return json.dumps(self.payload).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == recognition.ENDPOINT
            body = json.loads(request.data)
            calls.append(body)
            return Response(respond(body))
    monkeypatch.setattr(recognition, 'build_opener', lambda *args: Opener())
    return calls


@pytest.fixture
def frozen_finance_action(tmp_path, monkeypatch, request):
    from apps.oa_finance_preaudit import FlowEngine
    from tests.finance_preaudit.test_flow_engine import _travel_material_with_attachment

    def forbidden(*_args, **_kwargs):
        pytest.fail('Recognition test must not use live credentials or prepare new material')

    monkeypatch.setattr(recognition, '_api_key', lambda: 'offline-key')
    monkeypatch.setattr(recognition.auth, 'env', forbidden)
    monkeypatch.setattr(recognition.auth, 'load_local_env', forbidden)
    monkeypatch.setattr(recognition.attachment_extractors, 'prepare_materials', forbidden)

    dimensions = dict(getattr(request, 'param', {}))
    tiff_frames = dimensions.pop('tiff_frames', None)

    def collect(request, root):
        material = _travel_material_with_attachment(request, root)
        page = image(root / 'prepared', 'evidence.png', **dimensions)
        if tiff_frames is not None:
            tiff_image(page, frame_count=tiff_frames)
        material['prepared']['images'][0].update(
            content_sha256=hashlib.sha256(page.read_bytes()).hexdigest(), owner_detail_id='row-2')
        material['prepared']['manifest'][0]['status'] = 'image'
        return material

    engine = FlowEngine(root=tmp_path / 'flows', discover_candidates=lambda workflow: [{
        'request_id': 'offline-recognition', 'received_at': '2026-10-18T09:00:00+08:00',
        'workflow_key': workflow}], collect_material=collect)
    action = engine.next_action('travel_expense')
    assert action['action'] == 'review_required'
    return action


@pytest.mark.parametrize('storage', ['canonical', 'legacy'])
@pytest.mark.parametrize('frozen_finance_action,expected_calls', [
    ({}, 1), ({'width': 9042, 'height': 80}, 2),
], indirect=['frozen_finance_action'])
def test_action_file_cli_reads_frozen_finance_pages(
        tmp_path, monkeypatch, capsys, frozen_finance_action, storage, expected_calls):
    from apps import json_artifact
    from apps.oa_finance_preaudit import read_material

    action = frozen_finance_action
    path = Path(action['action_file'])
    if storage == 'legacy':
        state_path = Path(action['state_file'])
        state = json.loads(state_path.read_bytes())
        state.pop('action_reference_sha256')
        json_artifact.write_json_atomic(path, action)
        json_artifact.write_json_atomic(state_path, state)
    before = {p: p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    calls = fake_http(monkeypatch, lambda _: {'choices': [{'finish_reason': 'stop',
        'message': {'content': '读数 12345；右下角日期无法确认。'}}]})

    routes = json.loads(read_material.read(path).split('[reader.image_tool_routes]\n')[1])
    result = recognition.main([*routes['original_read']['argv'][4:], '--output-dir', str(tmp_path / 'out')])
    assert (result, len(calls)) == (0, expected_calls)

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_bytes())
    assert summary['status'] == 'complete' and summary['parts'] == 1
    assert len(calls) == expected_calls
    assert report['parts'][0]['owner_detail_id'] == 'row-2'
    assert report['parts'][0]['evidence_ref'] == 'material:prepared.images.evidence-1.1'
    assert report['frozen_binding']['action_file'] == str(path)
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_dispatch_region_command_keeps_frozen_source_and_partial_coverage(
        tmp_path, monkeypatch, capsys, frozen_finance_action):
    from apps.oa_finance_preaudit import read_material

    action = Path(frozen_finance_action['action_file'])
    before = {p: p.read_bytes() for p in action.parent.rglob('*') if p.is_file()}
    routes = json.loads(read_material.read(action).split('[reader.image_tool_routes]\n')[1])
    page = routes['pages'][0]
    calls = fake_http(monkeypatch, lambda _: {'choices': [{'finish_reason': 'stop',
        'message': {'content': '局部文字无法确认'}}]})

    assert recognition.main([*page['region_read']['argv_prefix'][4:],
        '--region', '1', '1', '7', '7', '--prompt', '读取选区', '--output-dir', str(tmp_path / 'out')]) == 0

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_bytes())
    assert len(calls) == 1
    assert report['coverage'] == 'image_region'
    assert report['sources'][0]['path'] == page['source']['path']
    assert report['sources'][0]['sha256'] == page['source']['content_sha256']
    assert report['parts'][0]['region'] == [1, 1, 7, 7]
    assert page['source']['owner_detail_id'] == 'row-2'
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('damage', ['reference', 'packet', 'material', 'page'])
def test_action_file_cli_rejects_changed_frozen_finance_input(
        tmp_path, monkeypatch, capsys, frozen_finance_action, damage):
    action = frozen_finance_action
    path = Path(action['action_file'])
    changed = {'reference': path, 'packet': path.with_name('review-packet.json'),
               'material': path.with_name('material.json'),
               'page': path.parent / 'prepared/evidence.png'}[damage]
    if damage == 'packet':
        packet = json.loads(changed.read_bytes())
        packet['workflow_key'] = 'other-workflow'
        changed.write_text(json.dumps(packet))
    else:
        changed.write_bytes(changed.read_bytes() + b' ')
    calls = fake_http(monkeypatch, lambda _: pytest.fail('Changed input must not reach HTTP'))

    assert recognition.main(['--action-file', str(path), '--output-dir', str(tmp_path / 'out')]) == 1

    summary = json.loads(capsys.readouterr().out)
    assert summary['status'] == 'failed'
    assert calls == []


def test_direct_api_custom_question_and_duplicate_source_mapping(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    one = image(tmp_path)
    two = tmp_path / 'two.png'
    two.write_bytes(one.read_bytes())
    report = recognition.recognize(files=[one, two], prompt='只读仪表总里程，保留单位。',
                                   output_dir=tmp_path / 'result')
    assert report['status'] == 'complete'
    assert len(calls) == 1
    blocks = calls[0]['messages'][-1]['content']
    assert sum(block['type'] == 'image_url' for block in blocks) == 1
    assert '只读仪表总里程' in blocks[0]['text']
    assert len(report['parts']) == 2
    assert report['parts'][1]['reused_from'] == report['parts'][0]['part_id']
    assert report['parts'][1]['resource_id'] != report['parts'][0]['resource_id']
    assert all('12345' in part['answer'] for part in report['parts'])
    assert 'base64' not in json.dumps(report)
    assert 'offline-secret' not in json.dumps(report)
    assert Path(report['report_file']).stat().st_mode & 0o777 == 0o600


def test_default_output_records_lifecycle_in_managed_root(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import retention
    fake_api(monkeypatch)
    root = tmp_path / 'flows'
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_FLOW_ROOT', str(root))
    before = datetime.now(timezone.utc)

    report = recognition.recognize(files=[image(tmp_path)])

    assert report['status'] == 'complete'
    assert Path(report['report_file']).parent.parent == root / '.attachment-recognitions'
    assert (root / retention.MARKER_NAME).is_file()
    assert before <= datetime.fromisoformat(report['created_at']) <= datetime.fromisoformat(report['completed_at'])
    assert report['retention'] == {'scope': 'standalone', 'owner_attempts': []}
    assert Path(report['report_file']).parent.stat().st_mode & 0o777 == 0o700
    lifecycle_file = Path(report['report_file']).with_name('retention.json')
    lifecycle = json.loads(lifecycle_file.read_text())
    assert lifecycle == {key: report[key] for key in (
        'schema_version', 'kind', 'report_file', 'created_at', 'completed_at', 'status', 'retention')}
    assert lifecycle_file.stat().st_size < 64 * 1024
    assert lifecycle_file.stat().st_mode & 0o777 == 0o600
    assert 'offline-secret' not in lifecycle_file.read_text()


@pytest.mark.parametrize('location', ['custom', 'control/attachment-recognitions/legacy-output'])
def test_custom_output_is_not_added_to_managed_root(tmp_path, monkeypatch, location):
    from apps.oa_finance_preaudit import retention, runtime_maintenance
    fake_api(monkeypatch)
    root = tmp_path / 'flows'
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_FLOW_ROOT', str(root))
    output = tmp_path / location
    report = recognition.recognize(files=[image(tmp_path)], output_dir=output)
    assert report['status'] == 'complete'
    assert not root.exists()
    assert not (output / 'retention.json').exists()
    root.mkdir()
    retention.initialize_root(root)
    runtime_maintenance.prune(root, now=datetime.fromisoformat(report['completed_at']) + timedelta(days=31))
    assert (output / 'recognition.json').is_file()


@pytest.mark.parametrize('alias', ['direct', 'parent_component', 'symlink'])
def test_custom_output_refuses_reserved_recognition_namespace(tmp_path, monkeypatch, alias):
    calls = fake_api(monkeypatch)
    root = tmp_path / 'flows'
    reserved = root / '.attachment-recognitions'
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_FLOW_ROOT', str(root))
    if alias == 'direct':
        output = reserved / ('a' * 32)
    elif alias == 'parent_component':
        output = root / 'unused' / '..' / '.attachment-recognitions' / ('a' * 32)
    else:
        reserved.mkdir(parents=True)
        link = tmp_path / 'alias'
        link.symlink_to(reserved, target_is_directory=True)
        output = link / ('a' * 32)

    with pytest.raises(ValueError, match='^attachment_output_reserved$'):
        recognition.recognize(files=[image(tmp_path)], output_dir=output)

    assert calls == []
    assert not output.resolve().exists()
    assert not list(reserved.glob('*/recognition.json'))


def test_pdf_all_pages_are_separate_requests(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    source = tmp_path / 'pages.pdf'
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((50, 50), f'page {i + 1}')
    doc.save(source)
    report = recognition.recognize(files=[source], prompt='逐页读内容', output_dir=tmp_path / 'out')
    images = [p for p in report['parts'] if p['kind'] == 'image']
    assert len(images) == 2
    assert all(p['answer'] for p in images)
    assert len([c for c in calls if any(b['type'] == 'image_url'
                                      for b in c['messages'][-1]['content'])]) == 2
    assert source.read_bytes().startswith(b'%PDF')


def test_truncated_response_is_failed_and_preserves_partial_results(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    one = image(tmp_path)
    def truncated(body, **kwargs):
        return {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}
    monkeypatch.setattr(recognition, '_post', truncated)
    report = recognition.recognize(files=[one], prompt='读取', output_dir=tmp_path / 'out')
    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_response_incomplete'
    assert report['parts'] == []
    assert report['failed_part']['resource_id'] == 'file-1'
    assert json.loads(Path(report['report_file']).read_text())['status'] == 'failed'
    assert calls == []


def test_direct_http_transcription_disables_default_thinking(tmp_path, monkeypatch, capsys):
    def respond(body):
        disabled = body.get('thinking') == {'type': 'disabled'}
        return {'choices': [{'finish_reason': 'stop' if disabled else 'length',
            'message': {'content': '完整转写' if disabled else ''}}]}
    calls = fake_http(monkeypatch, respond)
    output = tmp_path / 'out'
    assert recognition.main(['--file', str(image(tmp_path)), '--prompt', '完整读取本页',
                             '--output-dir', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert len(calls) == 1
    assert calls[0]['thinking'] == {'type': 'disabled'}
    assert calls[0]['max_tokens'] == 8192
    assert json.loads((output / 'recognition.json').read_text())['parts'][0]['answer'] == '完整转写'


@pytest.mark.parametrize(('finish_reason', 'content', 'tool_calls'), [
    ('length', 'partial private content', []),
    ('stop', '', []),
    ('tool_calls', None, [{'secret': 'private-tool-arguments'}]),
    ('private-untrusted-reason', 'partial private content', []),
])
def test_incomplete_response_keeps_safe_diagnostics_without_retry(
        tmp_path, monkeypatch, capsys, finish_reason, content, tool_calls):
    def respond(body):
        return {'choices': [{'finish_reason': finish_reason, 'message': {
            'content': content, 'tool_calls': tool_calls, 'reasoning_content': 'private-reasoning'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 8192, 'total_tokens': 8292,
                      'completion_tokens_details': {'reasoning_tokens': 8192},
                      'private': 'private-usage'}}
    calls = fake_http(monkeypatch, respond)
    output = tmp_path / 'out'
    assert recognition.main(['--file', str(image(tmp_path)), '--prompt', '读图',
                             '--output-dir', str(output)]) == 1
    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_text())
    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_response_incomplete'
    assert report['parts'] == []
    assert len(calls) == 1
    assert report['failed_response'] == {
        'finish_reason': finish_reason if finish_reason in {'length', 'stop', 'tool_calls'} else 'unknown',
        'content_chars': len(content or ''), 'has_tool_calls': bool(tool_calls),
        'usage': {'prompt_tokens': 100, 'completion_tokens': 8192, 'total_tokens': 8292,
                  'reasoning_tokens': 8192}}
    saved = (Path(report['report_file'])).read_text()
    assert all(value not in saved for value in (
        'partial private content', 'private-reasoning', 'private-tool-arguments',
        'private-untrusted-reason', 'private-usage', 'offline-secret'))
    assert json.loads(saved)['failed_response'] == report['failed_response']


def test_no_model_process_or_subprocess_is_started(tmp_path, monkeypatch):
    fake_api(monkeypatch)
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail('Recognition must use HTTP, not a model process')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    result = recognition.recognize(files=[image(tmp_path)], prompt='读取文字', output_dir=tmp_path / 'out')
    assert result['status'] == 'complete'


def test_frozen_action_preserves_page_identity_and_inputs(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    path = image(tmp_path)
    material = {'workflow_key': 'travel_expense', 'prepared': {'images': [{
        'resource_id': 'trip', 'path': str(path), 'kind': 'image',
        'owner_detail_id': 'row-2', 'content_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}],
        'structured_contents': []}}
    raw = json.dumps(material).encode()
    material_file = tmp_path / 'material.json'
    material_file.write_bytes(raw)
    packet = tmp_path / 'review-packet.json'
    packet.write_text(json.dumps({'workflow_key': 'travel_expense', 'material_file': str(material_file),
                                 'material_fingerprint': 'sha256:' + hashlib.sha256(raw).hexdigest()}))
    action = tmp_path / 'action.json'
    action.write_text(json.dumps({'workflow_key': 'travel_expense', 'review_packet_file': str(packet)}))
    before = {p: p.read_bytes() for p in (action, packet, material_file, path)}
    result = recognition.recognize(action_file=action, prompt='只读里程', output_dir=tmp_path.parent / (tmp_path.name + '-output'))
    assert result['parts'][0]['evidence_ref'] == 'material:prepared.images.trip.1'
    assert result['parts'][0]['owner_detail_id'] == 'row-2'
    assert result['retention'] == {'scope': 'unmanaged', 'owner_attempts': []}
    assert len(calls) == 1
    assert all(p.read_bytes() == data for p, data in before.items())


def test_missing_key_fails_before_request(tmp_path, monkeypatch):
    monkeypatch.delenv('OA_REVIEW_API_KEY', raising=False)
    monkeypatch.setattr(recognition, '_api_key', lambda: '')
    with pytest.raises(ValueError, match='attachment_api_key_missing'):
        recognition.recognize(files=[image(tmp_path)], prompt='读图', output_dir=tmp_path / 'out')


def test_text_attachment_is_chunked_without_omission(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    source = tmp_path / 'details.txt'
    source.write_text('每笔金额保留原值\n' * 7000)
    report = recognition.recognize(files=[source], prompt='检查金额', output_dir=tmp_path / 'out')
    assert report['status'] == 'complete'
    parts = report['parts']
    assert len(parts) > 1
    assert {p['chunk_count'] for p in parts} == {len(parts)}
    sent = ''.join(c['messages'][-1]['content'][1]['text'] for c in calls)
    assert json.loads(sent)['text'] == source.read_text()


def test_page_changed_during_api_call_does_not_complete(tmp_path, monkeypatch):
    fake_api(monkeypatch)
    source = image(tmp_path)
    def post(body, **kwargs):
        source.write_bytes(b'changed')
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'answer'}}]}
    monkeypatch.setattr(recognition, '_post', post)
    result = recognition.recognize(files=[source], prompt='读图', output_dir=tmp_path / 'out')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_source_changed'


def test_http_request_uses_only_deepseek_endpoint_and_custom_prompt(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, size):
            return b'{"choices":[]}'
    class Opener:
        def open(self, request, timeout):
            captured.update(url=request.full_url, body=json.loads(request.data), timeout=timeout,
                            authorization=request.get_header('Authorization'))
            return Response()
    monkeypatch.setattr(recognition, 'build_opener', lambda *args: Opener())
    body = {'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': '自定义问题'}]}
    assert recognition._post(body, key='private', timeout=15) == {'choices': []}
    assert captured == {'url': 'https://api.deepseek.com/chat/completions', 'body': body,
                        'timeout': 15, 'authorization': 'Bearer private'}


@pytest.mark.parametrize('error_code', ['attachment_api_http_400', 'attachment_api_http_429', 'attachment_response_incomplete'])
def test_failed_second_page_keeps_first_page_answer(tmp_path, monkeypatch, error_code):
    first = image(tmp_path)
    second = image(tmp_path, 'second.png')
    pix = fitz.Pixmap(second)
    pix.clear_with(0)
    pix.save(second)
    count = 0
    def respond(body):
        nonlocal count
        count += 1
        if count == 2:
            if error_code == 'attachment_response_incomplete':
                return {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}
            raise HTTPError(recognition.ENDPOINT, int(error_code.rsplit('_', 1)[1]), 'offline', {}, None)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'first answer'}}]}
    calls = fake_http(monkeypatch, respond)
    report = recognition.recognize(files=[first, second], prompt='读取', output_dir=tmp_path / 'out')
    assert report['status'] == 'failed'
    assert report['parts'][0]['answer'] == 'first answer'
    assert report['failed_part']['resource_id'] == 'file-2'
    assert report['error_code'] == error_code
    assert count == 2
    assert len(calls) == 2


@pytest.mark.parametrize('response', [None, {'choices': []}, {'choices': [{'message': None}]}])
def test_malformed_response_diagnostic_does_not_hide_original_error(tmp_path, monkeypatch, response):
    fake_api(monkeypatch)
    monkeypatch.setattr(recognition, '_post', lambda *args, **kwargs: response)
    report = recognition.recognize(files=[image(tmp_path)], prompt='读图', output_dir=tmp_path / 'out')
    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_response_invalid'
    assert report['failed_response'] == {
        'finish_reason': 'unknown', 'content_chars': 0, 'has_tool_calls': False, 'usage': {}}


@pytest.mark.parametrize(('prepared', 'code'), [
    ({'images': [], 'structured_contents': [], 'manifest': []}, 'attachment_content_empty'),
    ({'images': [{'resource_id': 'pdf', 'content_sha256': 'a' * 64}],
      'manifest': [{'resource_id': 'pdf', 'status': 'pdf_rendered', 'image_count': 2, 'page_count': 2}]},
     'attachment_page_coverage_incomplete'),
    ({'images': [{'resource_id': 'x', 'content_sha256': 'a' * 64}],
      'manifest': [{'status': 'missing_file'}]}, 'attachment_content_incomplete'),
    ({'images': [{'resource_id': 'x'}]}, 'attachment_page_digest_missing'),
])
def test_frozen_incomplete_preparation_never_calls_api(tmp_path, monkeypatch, prepared, code):
    calls = fake_api(monkeypatch)
    action = tmp_path / 'action.json'
    action.write_text('{}')
    monkeypatch.setattr(recognition, '_frozen', lambda _: ({'prepared': prepared}, {}))
    report = recognition.recognize(action_file=action, prompt='读附件',
                                   output_dir=tmp_path.parent / (tmp_path.name + '-out'))
    assert report['status'] == 'failed'
    assert report['error_code'] == code
    assert calls == []


def test_frozen_docx_embedded_image_uses_preparer_digest(tmp_path, monkeypatch):
    import zipfile
    from apps.oa_approval import attachment_extractors
    calls = fake_api(monkeypatch)
    picture = image(tmp_path)
    document = tmp_path / 'evidence.docx'
    with zipfile.ZipFile(document, 'w') as archive:
        archive.writestr('word/document.xml', '<document><body><p><t>invoice 123</t></p></body></document>')
        archive.writestr('word/media/image1.png', picture.read_bytes())
    images, manifest, structured = attachment_extractors.prepare_materials(
        [{'path': str(document), 'resource_id': 'doc', 'name': document.name}], out_dir=tmp_path / 'prepared')
    assert images[0]['content_sha256'] == hashlib.sha256(picture.read_bytes()).hexdigest()
    material = {'workflow_key': 'general_expense', 'prepared': {
        'images': [{**page, 'path': str(page['path'])} for page in images],
        'manifest': manifest, 'structured_contents': structured}}
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    packet = tmp_path / 'review-packet.json'
    packet.write_text(json.dumps({'workflow_key': 'general_expense', 'material_file': str(material_file),
        'material_fingerprint': 'sha256:' + hashlib.sha256(material_file.read_bytes()).hexdigest()}))
    action = tmp_path / 'action.json'
    action.write_text(json.dumps({'workflow_key': 'general_expense', 'review_packet_file': str(packet)}))
    result = recognition.recognize(action_file=action, prompt='读取文字和图片',
        output_dir=tmp_path.parent / (tmp_path.name + '-result'))
    assert result['status'] == 'complete'
    assert len(result['parts']) == 2
    assert result['parts'][0]['parent_resource_id'] == 'doc'
    assert len(calls) == 2


def test_container_larger_than_page_limit_is_streamed(tmp_path, monkeypatch):
    import openpyxl
    calls = fake_api(monkeypatch)
    source = tmp_path / 'large-container.xlsx'
    book = openpyxl.Workbook()
    book.active.append(['item', 'amount'])
    book.active.append(['service', 82917])
    book.save(source)
    monkeypatch.setattr(recognition, 'MAX_FILE_BYTES', 1024)
    assert source.stat().st_size > recognition.MAX_FILE_BYTES
    result = recognition.recognize(files=[source], prompt='读取表格', output_dir=tmp_path / 'out')
    assert result['status'] == 'complete'
    assert len(calls) == 1
    assert result['parts'][0]['kind'] == 'text'


@pytest.mark.parametrize('width,height,boxes,format', [
    (9042, 80, [[0, 0, 4585, 80], [4457, 0, 9042, 80]], 'PNG'),
    (80, 9042, [[0, 0, 80, 4585], [0, 4457, 80, 9042]], 'PNG'),
    (9042, 80, [[0, 0, 4585, 80], [4457, 0, 9042, 80]], 'WEBP'),
])
def test_wide_page_is_read_without_resampling_or_changing_source(tmp_path, monkeypatch, width, height, boxes, format):
    import base64
    from io import BytesIO
    from PIL import Image

    source = tmp_path / ('wide.' + format.lower())
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pix.clear_with(255)
    pix.set_pixel(0, 0, (255, 0, 0))
    pix.set_pixel(width - 1, height - 1, (0, 0, 255))
    Image.open(BytesIO(pix.tobytes('png'))).save(source, format=format, lossless=True)
    original = source.read_bytes()
    received = []

    def respond(body):
        blocks = body['messages'][-1]['content']
        url = next(b['image_url']['url'] for b in blocks if b['type'] == 'image_url')
        page = fitz.Pixmap(base64.b64decode(url.split(',', 1)[1]))
        received.append(page)
        if max(page.width, page.height) > 8192:
            raise HTTPError(recognition.ENDPOINT, 400, 'offline dimension limit', {}, None)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '完整分区读取'}}]}

    fake_http(monkeypatch, respond)
    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == 'complete'
    assert source.read_bytes() == original
    assert len(report['parts']) == 1
    part = report['parts'][0]
    tiles = part['image_tiles']
    assert len(tiles) == len(received) == 2
    assert [t['box'] for t in tiles] == boxes
    assert received[0].pixel(0, 0) == (255, 0, 0)
    assert received[1].pixel(received[1].width - 1, received[1].height - 1) == (0, 0, 255)
    assert all(p.width <= 8192 and p.height <= 8192 for p in received)
    assert part['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert all(t['answer'] == '完整分区读取' for t in tiles)
    assert part['usage'] == {}


@pytest.mark.parametrize('failure', ['http_400', 'incomplete'])
@pytest.mark.parametrize('format', ['PNG', 'JPEG'])
def test_failed_tile_keeps_partial_read_without_completing_page(tmp_path, monkeypatch, failure, format):
    from PIL import Image

    source = tmp_path / ('wide.' + format.lower())
    Image.new('RGB', (9042, 80), 'white').save(source, format=format)
    if format == 'JPEG':
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 200000)
    count = 0

    def respond(body):
        nonlocal count
        count += 1
        if count == 2:
            if failure == 'http_400':
                raise HTTPError(recognition.ENDPOINT, 400, 'private upstream error', {}, None)
            return {'choices': [{'finish_reason': 'length', 'message': {'content': 'private partial'}}]}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'first tile'}}],
                'usage': {'total_tokens': 7}}

    fake_http(monkeypatch, respond)
    with pytest.warns(Image.DecompressionBombWarning) if format == 'JPEG' else nullcontext():
        report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == 'failed'
    assert report['error_code'] == ('attachment_api_http_400' if failure == 'http_400'
                                    else 'attachment_response_incomplete')
    assert count == 2
    assert report['parts'] == []
    assert report['failed_part']['failed_tile_box'] == [4457, 0, 9042, 80]
    assert report['failed_part']['image_tiles'] == [
        {'box': [0, 0, 4585, 80], 'answer': 'first tile', 'usage': {'total_tokens': 7}}]
    saved = Path(report['report_file']).read_text()
    assert 'private upstream error' not in saved and 'private partial' not in saved
    assert json.loads(saved) == report


@pytest.mark.parametrize('format', ['WEBP', 'JPEG'])
def test_normal_image_sends_original_bytes(tmp_path, monkeypatch, format):
    import base64
    from PIL import Image

    source = tmp_path / ('normal.' + format.lower())
    Image.new('RGB', (8, 8), 'blue').save(source, format=format)
    original = source.read_bytes()
    calls = fake_http(monkeypatch, lambda _: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'blue'}}]})

    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == 'complete'
    assert len(calls) == 1
    block = calls[0]['messages'][-1]['content'][1]
    assert block['image_url']['url'].startswith(f'data:image/{format.lower()};base64,')
    assert base64.b64decode(block['image_url']['url'].split(',', 1)[1]) == original
    assert 'image_tiles' not in report['parts'][0]
    assert source.read_bytes() == original


@pytest.mark.parametrize('suffix', ['.tiff', '.png'])
def test_direct_multipage_tiff_is_rejected_before_http(tmp_path, monkeypatch, suffix):
    source = tiff_image(tmp_path / ('two-pages' + suffix), frame_count=2)
    original = source.read_bytes()
    calls = fake_http(monkeypatch, lambda _: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'first page only'}}]})

    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_multipage_image_unsupported'
    assert report['parts'] == [] and calls == []
    assert report['failed_part']['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert report['sources'][0]['sha256'] == hashlib.sha256(original).hexdigest()
    assert json.loads(Path(report['report_file']).read_bytes()) == report
    assert source.read_bytes() == original


@pytest.mark.parametrize('frozen_finance_action', [{'tiff_frames': 2}], indirect=True)
def test_frozen_multipage_tiff_is_rejected_before_http(tmp_path, monkeypatch, frozen_finance_action):
    action_file = Path(frozen_finance_action['action_file'])
    before = {p: p.read_bytes() for p in action_file.parent.rglob('*') if p.is_file()}
    calls = fake_http(monkeypatch, lambda _: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'first page only'}}]})

    report = recognition.recognize(action_file=action_file, output_dir=tmp_path / 'out')

    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_multipage_image_unsupported'
    assert report['parts'] == [] and calls == []
    assert report['failed_part']['evidence_ref'] == 'material:prepared.images.evidence-1.1'
    assert report['failed_part']['owner_detail_id'] == 'row-2'
    assert report['frozen_binding']['action_file'] == str(action_file)
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_single_page_tiff_keeps_pixels_and_source_binding(tmp_path, monkeypatch):
    import base64

    source = tiff_image(tmp_path / 'single-page.tiff', frame_count=1)
    original = source.read_bytes()
    calls = fake_http(monkeypatch, lambda _: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'red page'}}]})

    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == 'complete'
    assert len(calls) == len(report['parts']) == 1
    url = calls[0]['messages'][-1]['content'][1]['image_url']['url']
    assert url.startswith('data:image/png;base64,')
    sent = fitz.Pixmap(base64.b64decode(url.split(',', 1)[1]))
    assert (sent.width, sent.height, sent.pixel(1, 1)) == (16, 16, (255, 0, 0))
    assert report['parts'][0]['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original


@pytest.mark.parametrize('width,expected', [(8, 'complete'), (9042, 'failed')])
def test_animated_gif_is_never_silently_replaced_by_first_frame(tmp_path, monkeypatch, width, expected):
    import base64
    from PIL import Image

    source = tmp_path / 'animated.gif'
    frames = [Image.new('RGB', (width, 80), color) for color in ('red', 'blue')]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
    original = source.read_bytes()
    calls = fake_http(monkeypatch, lambda _: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'two frames'}}]})

    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == expected
    assert source.read_bytes() == original
    if expected == 'complete':
        assert len(calls) == 1
        url = calls[0]['messages'][-1]['content'][1]['image_url']['url']
        assert base64.b64decode(url.split(',', 1)[1]) == original
    else:
        assert report['error_code'] == 'attachment_animated_image_too_large'
        assert report['parts'] == []
        assert calls == []


def test_oversized_cmyk_jpeg_reads_all_regions_as_rgb(tmp_path, monkeypatch):
    import base64
    from io import BytesIO
    from PIL import Image

    source = tmp_path / 'cmyk.jpg'
    Image.new('CMYK', (9042, 80), (255, 0, 0, 0)).save(source, format='JPEG')
    original = source.read_bytes()
    received = []

    def respond(body):
        url = body['messages'][-1]['content'][1]['image_url']['url']
        with Image.open(BytesIO(base64.b64decode(url.split(',', 1)[1]))) as tile:
            received.append((tile.size, tile.mode, tile.getpixel((0, 0))))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'cyan'}}]}

    fake_http(monkeypatch, respond)
    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out')

    assert report['status'] == 'complete'
    assert received == [((4585, 80), 'RGB', (0, 255, 255))] * 2
    assert source.read_bytes() == original
    assert report['parts'][0]['content_sha256'] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize('orientation', [None, 6])
@pytest.mark.parametrize('region', [None, (1, 2, 20, 22)])
def test_pixel_protected_baseline_jpeg_preserves_original_pixels_and_guard(tmp_path, monkeypatch, orientation, region):
    import base64
    from io import BytesIO
    from PIL import Image

    source = tmp_path / 'protected.jpg'
    pixels = Image.new('RGB', (64, 48), 'white')
    pixels.paste('red', (0, 0, 23, 17))
    pixels.paste('blue', (31, 19, 64, 48))
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    pixels.save(source, format='JPEG', subsampling=0, dpi=(300, 300), exif=exif)
    original = source.read_bytes()
    with Image.open(BytesIO(original)) as normal:
        expected = normal.copy()
    received = []

    def respond(body):
        url = body['messages'][-1]['content'][1]['image_url']['url']
        with Image.open(BytesIO(base64.b64decode(url.split(',', 1)[1]))) as tile:
            received.append((tile.size, tile.tobytes()))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'visible facts'}}]}

    fake_http(monkeypatch, respond)
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1000)
    monkeypatch.setattr(recognition, 'MAX_IMAGE_DIMENSION', 24)
    monkeypatch.setattr(recognition, 'MAX_DECODED_IMAGE_BYTES', 64 * 48 * 4)
    with pytest.raises(Image.DecompressionBombError):
        Image.open(BytesIO(original))

    report = recognition.recognize(files=[source], output_dir=tmp_path / 'out', region=region)

    assert report['status'] == 'complete'
    tiles = report['parts'][0]['image_tiles']
    assert len(tiles) == len(received)
    assert len(tiles) == 1 if region else len(tiles) > 1
    for tile, (size, raw) in zip(tiles, received):
        with expected.crop(tile['box']) as reference:
            assert (size, raw) == (reference.size, reference.tobytes())
    assert source.read_bytes() == original
    assert report['parts'][0]['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert Image.MAX_IMAGE_PIXELS == 1000


@pytest.mark.parametrize('kind,expected', [
    ('progressive', 'attachment_image_large_decode_unsupported'),
    ('extended', 'attachment_image_large_decode_unsupported'),
    ('CMYK', 'attachment_image_large_decode_unsupported'),
    ('MPO', 'attachment_image_large_decode_unsupported'),
    ('PNG', 'attachment_image_large_decode_unsupported'),
    ('budget', 'attachment_image_decoded_memory_limit'),
    ('corrupt', 'attachment_image_invalid'),
    ('truncated', 'attachment_image_invalid'),
])
@pytest.mark.parametrize('region', [None, (1, 2, 20, 22)])
def test_unsafe_pixel_protected_image_fails_without_http_and_keeps_report(
        tmp_path, monkeypatch, capsys, kind, expected, region):
    from PIL import Image

    source = tmp_path / ('protected.png' if kind == 'PNG' else 'protected.jpg')
    pixels = Image.new('CMYK' if kind == 'CMYK' else 'RGB', (64, 48))
    if kind == 'MPO':
        pixels.save(source, format='MPO', save_all=True,
                    append_images=[Image.new('RGB', (64, 48), 'blue')])
    else:
        pixels.save(source, format='PNG' if kind == 'PNG' else 'JPEG', progressive=kind == 'progressive')
    if kind == 'extended':
        source.write_bytes(source.read_bytes().replace(b'\xff\xc0', b'\xff\xc1', 1))
    if kind == 'corrupt':
        source.write_bytes(b'\xff\xd8\xff\xe0\x00\x10invalid')
    if kind == 'truncated':
        source.write_bytes(source.read_bytes()[:-10])
    original = source.read_bytes()
    calls = fake_http(monkeypatch, lambda _: pytest.fail('Rejected image must not reach HTTP'))
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1000)
    monkeypatch.setattr(recognition, 'MAX_IMAGE_DIMENSION', 24)
    monkeypatch.setattr(recognition, 'MAX_DECODED_IMAGE_BYTES',
                        64 * 48 * 4 - (1 if kind == 'budget' else 0))

    arguments = ['--file', str(source), '--output-dir', str(tmp_path / 'out')]
    if region:
        arguments.extend(['--region', *map(str, region)])
    assert recognition.main(arguments) == 1

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_bytes())
    assert summary['error_code'] == report['error_code'] == expected
    assert report['status'] == 'failed' and report['parts'] == []
    assert report['failed_part']['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original
    assert calls == [] and Image.MAX_IMAGE_PIXELS == 1000


def test_region_cli_sends_only_original_stored_pixels_with_partial_provenance(tmp_path, monkeypatch, capsys):
    import base64
    from io import BytesIO
    from PIL import Image

    source = tmp_path / 'oriented.png'
    pixels = Image.new('RGB', (8, 6), 'white')
    pixels.paste((255, 0, 0), (2, 1, 4, 3))
    pixels.putpixel((4, 3), (0, 0, 255))
    exif = Image.Exif()
    exif[274] = 6
    pixels.save(source, exif=exif)
    original = source.read_bytes()
    guard = Image.MAX_IMAGE_PIXELS
    calls = fake_http(monkeypatch, lambda _: {'choices': [{'finish_reason': 'stop',
        'message': {'content': '原样局部回答'}}]})

    assert recognition.main(['--file', str(source), '--region', '2', '1', '5', '4',
                             '--output-dir', str(tmp_path / 'out')]) == 0

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_bytes())
    assert summary['coverage'] == report['coverage'] == 'image_region'
    assert len(calls) == len(report['parts']) == 1
    raw = base64.b64decode(calls[0]['messages'][-1]['content'][1]['image_url']['url'].split(',', 1)[1])
    with Image.open(BytesIO(raw)) as sent:
        assert sent.format == 'PNG' and sent.size == (3, 3)
        assert sent.getexif().get(274) is None
        assert list(sent.get_flattened_data()) == [(255, 0, 0), (255, 0, 0), (255, 255, 255),
                                       (255, 0, 0), (255, 0, 0), (255, 255, 255),
                                       (255, 255, 255), (255, 255, 255), (0, 0, 255)]
    part = report['parts'][0]
    assert part['source_image_size'] == [8, 6]
    assert part['region'] == [2, 1, 5, 4]
    assert part['image_tiles'][0]['box'] == [2, 1, 5, 4]
    assert part['image_tiles'][0]['png_sha256'] == hashlib.sha256(raw).hexdigest()
    assert part['image_tiles'][0]['answer'] == '原样局部回答'
    assert '仅覆盖指定区域' in part['answer']
    assert '仅覆盖指定区域' in calls[0]['messages'][-1]['content'][0]['text']
    assert part['content_sha256'] == report['sources'][0]['sha256'] == hashlib.sha256(original).hexdigest()
    assert report['sources'][0]['path'] == str(source)
    assert source.read_bytes() == original and Image.MAX_IMAGE_PIXELS == guard


@pytest.mark.parametrize('region', [True, '0123', [0, 0, 1], [0, False, 1, 1],
                                    [0, 0, 1.5, 1], [-1, 0, 1, 1], [1, 0, 1, 1],
                                    [0, 2, 1, 1], [0, 0, 8193, 1]])
def test_region_rejects_invalid_coordinates_before_http(tmp_path, monkeypatch, region):
    source = image(tmp_path)
    calls = fake_http(monkeypatch, lambda _: pytest.fail('Invalid region must not reach HTTP'))
    with pytest.raises(ValueError, match='^attachment_region_invalid$'):
        recognition.recognize(files=[source], region=region, output_dir=tmp_path / 'out')
    assert calls == []


@pytest.mark.parametrize('kind', ['multiple', 'action', 'pdf', 'text', 'animated', 'bounds', 'disguised'])
def test_region_rejects_sources_it_cannot_cover(tmp_path, monkeypatch, kind):
    from PIL import Image

    source = image(tmp_path)
    files, action = [source], None
    if kind == 'multiple':
        files.append(image(tmp_path, 'two.png'))
    elif kind == 'action':
        files, action = None, tmp_path / 'action.json'
    elif kind == 'pdf':
        source = tmp_path / 'one.pdf'
        with fitz.open() as pdf:
            pdf.new_page()
            pdf.save(source)
        files = [source]
    elif kind == 'text':
        source = tmp_path / 'one.txt'
        source.write_text('synthetic text')
        files = [source]
    elif kind == 'animated':
        source = tmp_path / 'one.gif'
        Image.new('RGB', (8, 8), 'red').save(source, save_all=True,
            append_images=[Image.new('RGB', (8, 8), 'blue')], duration=100)
        files = [source]
    elif kind == 'disguised':
        source = tmp_path / 'not-a-jpeg.jpg'
        source.write_text('synthetic text')
        files = [source]
    calls = fake_http(monkeypatch, lambda _: pytest.fail('Rejected source must not reach HTTP'))
    if kind in {'multiple', 'action'}:
        with pytest.raises(ValueError, match='^attachment_region_source_invalid$'):
            recognition.recognize(files=files, action_file=action, region=[0, 0, 2, 2],
                                  output_dir=tmp_path / 'out')
    else:
        report = recognition.recognize(files=files, region=[0, 0, 9 if kind == 'bounds' else 2, 2],
                                       output_dir=tmp_path / 'out')
        assert report['status'] == 'failed' and report['parts'] == []
        assert report['coverage'] == 'image_region'
        assert report['error_code'] == {'bounds': 'attachment_region_out_of_bounds',
            'animated': 'attachment_region_source_invalid',
            'disguised': 'attachment_image_invalid'}.get(kind, 'attachment_image_invalid')
        assert report['sources'][0]['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert report['failed_part']['region'] == [0, 0, 9 if kind == 'bounds' else 2, 2]
    assert calls == []


@pytest.mark.parametrize('failure', ['http_400', 'incomplete', 'source_changed'])
def test_region_failure_keeps_source_and_region_without_retry(tmp_path, monkeypatch, failure):
    source = image(tmp_path)
    original = source.read_bytes()
    def respond(body):
        if failure == 'http_400':
            raise HTTPError(recognition.ENDPOINT, 400, 'private failure', {}, None)
        if failure == 'source_changed':
            source.write_bytes(original + b'changed')
        return {'choices': [{'finish_reason': 'length' if failure == 'incomplete' else 'stop',
                              'message': {'content': 'private response'}}]}
    calls = fake_http(monkeypatch, respond)

    report = recognition.recognize(files=[source], region=(1, 1, 5, 4), output_dir=tmp_path / 'out')

    assert len(calls) == 1 and report['status'] == 'failed'
    assert report['error_code'] == {'http_400': 'attachment_api_http_400',
        'incomplete': 'attachment_response_incomplete', 'source_changed': 'attachment_source_changed'}[failure]
    assert report['coverage'] == 'image_region'
    assert report['sources'][0]['sha256'] == report['failed_part']['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert report['failed_part']['source_image_size'] == [8, 8]
    assert report['failed_part']['region'] == [1, 1, 5, 4]
    assert len(report['failed_part']['region_png_sha256']) == 64
    assert json.loads(Path(report['report_file']).read_bytes()) == report
    if failure != 'source_changed':
        assert report['parts'] == [] and 'private response' not in json.dumps(report)


def test_region_excess_png_bytes_is_rejected_without_splitting(tmp_path, monkeypatch):
    import random
    from PIL import Image

    source = tmp_path / 'noise.jpg'
    Image.frombytes('RGB', (64, 64), random.Random(0).randbytes(64 * 64 * 3)).save(source)
    original = source.read_bytes()
    monkeypatch.setattr(recognition, 'MAX_FILE_BYTES', len(original) + 1)
    calls = fake_http(monkeypatch, lambda _: pytest.fail('Oversize region must not reach HTTP'))

    report = recognition.recognize(files=[source], region=[0, 0, 64, 64], output_dir=tmp_path / 'out')

    assert report['status'] == 'failed' and report['error_code'] == 'attachment_image_too_large'
    assert report['coverage'] == 'image_region' and calls == []
    assert report['failed_part']['region'] == [0, 0, 64, 64]
    assert report['failed_part']['content_sha256'] == hashlib.sha256(original).hexdigest()
    assert len(report['failed_part']['region_png_sha256']) == 64


@pytest.mark.parametrize('kind,error', [('coordinates', 'attachment_region_invalid'),
    ('bounds', 'attachment_region_out_of_bounds'), ('multiple', 'attachment_region_source_invalid'),
    ('action', 'attachment_region_source_invalid')])
def test_region_cli_reports_rejected_input_without_http(tmp_path, monkeypatch, capsys, kind, error):
    source = image(tmp_path)
    args = ['--file', str(source)] if kind != 'action' else ['--action-file', str(tmp_path / 'action.json')]
    if kind == 'multiple':
        args.extend(['--file', str(source)])
    args.extend(['--region', '0', '0', '8193' if kind == 'coordinates' else '9' if kind == 'bounds' else '2', '2',
                 '--output-dir', str(tmp_path / 'out')])
    calls = fake_http(monkeypatch, lambda _: pytest.fail('Invalid region must not reach HTTP'))

    assert recognition.main(args) == 1

    summary = json.loads(capsys.readouterr().out)
    assert summary['status'] == 'failed' and summary['error_code'] == error
    assert calls == []
