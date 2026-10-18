from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import fitz
import pytest

from apps.oa_approval import attachment_recognition as recognition


def image(tmp_path, name='one.png'):
    path = tmp_path / name
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pix.clear_with(255)
    pix.save(path)
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


@pytest.mark.parametrize('error_code', ['attachment_api_http_429', 'attachment_response_incomplete'])
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
            raise HTTPError(recognition.ENDPOINT, 429, 'offline', {}, None)
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
