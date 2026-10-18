from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import fitz
import pytest

from apps.oa_approval import api, attachments, auth, flow, invoice_buyers, official_sql
from apps.oa_finance_preaudit.case_bundle import collect_case
from tests.finance_preaudit.test_invoice_evidence_coverage import CONFIG, NUMBER, InvoiceApi


@pytest.fixture
def original_transport(monkeypatch, tmp_path):
    client = InvoiceApi()
    workflow, _ = client.request_context('101')
    workflow['requestId'] = '101'
    client.request_context = lambda _request_id: (workflow, [])
    original_get = client.get
    client.get = lambda path, params=None: (
        {'status': '1', 'targetIds': '9001'} if path == invoice_buyers.MAPPING_PATH
        else original_get(path, params)
    )
    document = fitz.open()
    page = document.new_page()
    page.insert_text((40, 40), 'Original invoice ' + NUMBER)
    state = SimpleNamespace(
        client=client, workflow=workflow, initialized=False, requests=[], actor='77',
        main_file='202', still_denied=False, pdf=document.tobytes(), cloud_pdf=None,
        after_cloud_download=None, cloud_failure=None, cloud_headers={},
        detail={'id': '9001', 'fid': '9001', 'number': NUMBER,
                'flist': [{'id': '8001', 'type': 'pdf', 'size': 0, 'del': 0}],
                'modify_info': {'pdf': '8001', 'furl': '8001'},
                'oa': {'doc_id': '303', 'img_id': '202'}},
    )
    document.close()
    monkeypatch.setattr(api, 'build_api', lambda _config: client)
    monkeypatch.setattr(flow, 'load_all_configs', lambda: [CONFIG])
    monkeypatch.setattr(official_sql, 'workflow_attachment_document', lambda *_args: '')
    monkeypatch.setattr(attachments, 'OA_HOST', 'https://oa.example.test')
    monkeypatch.setattr(attachments, 'EXTRA_DOWNLOAD_HOSTS', set())
    monkeypatch.setattr(attachments, '_rpa_cookie_header', lambda *_a, **_kw: 'session=fixed')
    monkeypatch.setattr(auth, 'env', lambda key, default='': '77' if key == 'OA_UID' else default)
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', str(tmp_path / 'cache.sqlite3'))

    class Response(BytesIO):
        def __init__(self, body, url):
            super().__init__(body)
            self.url = url
            self.headers = {'Content-Type': 'application/pdf', 'Content-Length': str(len(body))}

        def geturl(self):
            return self.url

    def open_request(request, **_kwargs):
        path = urlsplit(request.full_url).path
        params = parse_qs(urlsplit(request.full_url).query)
        state.requests.append((path, params, request.get_header('Cookie')))
        if request.full_url == invoice_buyers.DETAIL_URL:
            body = {'actionMsg': {'code': '0'}, 'data': state.detail}
        elif path == '/invoiceApi/file/download':
            assert urlsplit(request.full_url).hostname == 'api.mypiaojia.com'
            assert params == {'id': ['8001'], 'fid': ['9001'], 'dataId': ['101'],
                              'token': ['synthetic-token']}
            assert request.get_header('Cookie') is None
            if state.cloud_failure == 'http':
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 403, 'forbidden', {}, None)
            if state.cloud_failure == 'redirect':
                from apps.oa_approval.invoice_originals import _NoCloudRedirect
                return _NoCloudRedirect().redirect_request(request, None, 302, '', {}, 'https://other.invalid/collect')
            if state.after_cloud_download:
                state.after_cloud_download()
            response = Response(state.cloud_pdf or b'{"actionMsg":{"code":"denied"}}', request.full_url)
            response.headers.update(state.cloud_headers)
            return response
        elif path == '/api/hrm/login/getAccountList':
            body = {'data': {'userid': state.actor}}
        elif path == '/api/doc/detail/basicInfo':
            assert params.get('docid') == ['303']
            if 'requestid' in params:
                assert params['requestid'] == ['101'] and params['isrequest'] == ['1']
                state.initialized = True
            body = {'status': 1, 'mainimagefile': state.main_file}
        else:
            assert path == '/weaver/weaver.file.FileDownload'
            assert params == {'fileid': ['202']}
            if not state.initialized or state.still_denied:
                return Response(b'<html>permission denied</html>', 'https://oa.example.test/notice/noright.jsp')
            return Response(state.pdf, request.full_url)
        return Response(json.dumps(body).encode(), request.full_url)

    monkeypatch.setattr(attachments.urllib.request, 'urlopen', open_request)
    monkeypatch.setattr(attachments.urllib.request, 'build_opener', lambda *_a: SimpleNamespace(open=open_request))
    monkeypatch.setattr(attachments, '_download_resource_with_browser', lambda *_a, **_kw: pytest.fail('classified denial must stop'))
    return state


def collect_original(state, tmp_path):
    return collect_case('101', oa_api=state.client, configs=[state.client.config],
                        allow_browser_download=True, attachment_root=tmp_path / 'attachments',
                        prepared_root=tmp_path / 'prepared')


def test_collect_reads_linked_original_body_via_native_context(original_transport, tmp_path):
    state = original_transport
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['resource_id'] == '202'
    assert len(material['resources']) == 1
    assert material['resources'][0]['source_field_path'] == 'detail[0].row[0].field[0]'
    assert material['prepared']['manifest'][0]['page_count'] == 1
    assert NUMBER in material['prepared']['structured_contents'][0]['key_text'][0]
    assert state.initialized
    downloads = [r for r in state.requests if r[0] == '/weaver/weaver.file.FileDownload']
    assert len(downloads) == 2 and downloads[0] == downloads[1]
    assert 'session=fixed' not in json.dumps(material, default=str)


def test_collect_reads_cloud_original_when_native_document_remains_denied(original_transport, tmp_path):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = state.pdf
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['original_source']['cloud_file_id'] == '8001'
    assert material['prepared']['manifest'][0]['page_count'] == 1
    assert NUMBER in material['prepared']['structured_contents'][0]['key_text'][0]
    assert sum(r[0] == '/invoiceApi/file/download' for r in state.requests) == 1
    assert 'synthetic-token' not in json.dumps(material, default=str)
    assert not any('synthetic-token' in p.read_text(errors='ignore')
                   for p in (tmp_path / 'attachments').rglob('*.json'))


def test_cloud_body_is_not_prepared_after_linked_file_changes(original_transport, tmp_path):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = state.pdf
    state.after_cloud_download = lambda: state.detail['modify_info'].update(furl='8002')
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not list((tmp_path / 'attachments').rglob('*.cloud.part'))
    assert not list((tmp_path / 'prepared').rglob('*.png'))


@pytest.mark.parametrize('failure', ['json', 'truncated', 'oversized', 'http', 'redirect'])
def test_cloud_failure_keeps_collection_unavailable(original_transport, tmp_path, monkeypatch, failure):
    from apps.oa_approval import invoice_originals
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = b'{}' if failure == 'json' else state.pdf
    if failure == 'truncated':
        state.cloud_headers['Content-Length'] = str(len(state.pdf) + 1)
    if failure == 'oversized':
        monkeypatch.setattr(invoice_originals, '_MAX_ORIGINAL_BYTES', 16)
    state.cloud_failure = failure
    expected = {'http': 'attachment_access_denied', 'redirect': 'attachment_download_failed'}.get(
        failure, 'attachment_content_incomplete')
    with pytest.raises(RuntimeError, match='^' + expected + '$'):
        collect_original(state, tmp_path)
    assert sum(r[0] == '/invoiceApi/file/download' for r in state.requests) == 1
    assert not list((tmp_path / 'attachments').rglob('*.cloud.part'))


@pytest.mark.parametrize('change', ['request', 'record', 'number', 'cloud_file', 'doc', 'main_file', 'actor'])
def test_untrusted_original_never_reaches_body_preparation(original_transport, tmp_path, change):
    state = original_transport
    if change == 'request':
        state.workflow['requestId'] = '999'
    elif change == 'record':
        state.detail['id'] = '999'
    elif change == 'number':
        state.detail['number'] = '999'
    elif change == 'cloud_file':
        state.detail['modify_info']['furl'] = '999'
    elif change == 'doc':
        state.detail['oa']['doc_id'] = '999'
    elif change == 'main_file':
        state.main_file = '999'
    else:
        state.actor = '88'
    if change == 'cloud_file':
        material = collect_original(state, tmp_path)
        assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'not_read'
        assert not material['resources']
    else:
        with pytest.raises(RuntimeError, match='resource_ownership_untrusted|invoice_buyer_read_failed|rpa_session_invalid'):
            collect_original(state, tmp_path)
    assert not state.initialized
    assert not any(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)


@pytest.mark.parametrize('failure', ['denied', 'invalid_pdf'])
def test_bound_original_download_or_preparation_failure_stops_collection(original_transport, tmp_path, failure):
    state = original_transport
    if failure == 'denied':
        state.still_denied = True
    else:
        state.pdf = b'broken PDF'
    with pytest.raises(RuntimeError):
        collect_original(state, tmp_path)
    assert state.initialized
    assert sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests) == 2


def test_forged_original_hint_cannot_initialize_unlinked_document(original_transport, tmp_path):
    state = original_transport
    state.detail.pop('oa')
    files, errors = attachments.download_resources([
        {'id': '202', 'name': 'original.pdf',
         'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202',
         '_invoice_original': {'document_id': '303', 'native_file_id': '202'}},
    ], dest_dir=tmp_path / 'files', timeout_sec=1, allow_browser_download=True,
        request_id='101', flow_key=CONFIG['flow_key'])
    assert not files and errors[0]['code'] == 'resource_ownership_untrusted'
    assert not state.initialized
    assert not any(r[0] == '/api/doc/detail/basicInfo' for r in state.requests)


def test_multiple_file_records_keep_partial_body_coverage(original_transport, tmp_path):
    state = original_transport
    state.detail['flist'].append({'id': '8002', 'type': 'ofd', 'del': 0})
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'partial_read'
    assert [f['body_read_status'] for f in invoice['files']] == ['content_read', 'not_read']
    from apps.oa_approval.invoice_originals import body_coverage
    assert body_coverage(invoice, material['resources'], material['prepared']) == 'partial_read'


@pytest.mark.parametrize('mapping', ['missing_document', 'multiple_pdfs', 'deleted_pdf'])
def test_unmapped_original_keeps_authoritative_invoice_without_guessing_download(original_transport, tmp_path, mapping):
    state = original_transport
    if mapping == 'missing_document':
        state.detail.pop('oa')
    elif mapping == 'multiple_pdfs':
        state.detail['flist'].append({'id': '8002', 'type': 'pdf', 'del': 0})
    else:
        state.detail['flist'][0]['del'] = 1
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['invoice_no'] == NUMBER
    assert invoice['body_read_status'] == 'not_read'
    assert not material['resources'] and not material['prepared']['images']
    assert not state.initialized
    assert not any(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)


def test_original_prepares_every_page_before_claiming_complete_body(original_transport, tmp_path):
    state = original_transport
    with fitz.open(stream=state.pdf, filetype='pdf') as document:
        document.new_page().insert_text((40, 40), 'Invoice continuation page')
        state.pdf = document.tobytes()
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['page_count'] == 2
    assert material['prepared']['manifest'][0]['page_count'] == 2
    assert len(material['prepared']['images']) == 2
    assert 'Invoice continuation page' in material['prepared']['structured_contents'][0]['key_text'][1]


def test_existing_exact_native_resource_is_downloaded_once(original_transport, tmp_path):
    state = original_transport
    resource = {'id': '202', 'name': 'ordinary.pdf',
                'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202'}
    state.client.request_context = lambda _request_id: (state.workflow, [resource])
    material = collect_original(state, tmp_path)
    assert len(material['resources']) == 1
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'
    assert sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests) == 2


@pytest.mark.parametrize('workflow_key', [
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
])
def test_original_body_uses_shared_finance_collection(workflow_key, original_transport, tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import case_bundle
    state = original_transport
    config = {**CONFIG, 'flow_key': workflow_key}
    state.client.config = config
    monkeypatch.setattr(flow, 'load_all_configs', lambda: [config])
    monkeypatch.setattr(official_sql, 'travel_extra_fields', lambda _workflow: [])
    monkeypatch.setattr(case_bundle, '_travel_resources', lambda **_kwargs: ([], []))
    material = collect_original(state, tmp_path)
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'
    assert material['prepared']['images']


@pytest.mark.parametrize('cloud_fallback', [False, True])
def test_original_body_enters_frozen_review_input_and_tampering_is_rejected(original_transport, tmp_path, cloud_fallback):
    from pathlib import Path
    from apps.oa_finance_preaudit import flow_engine, read_material
    state = original_transport
    state.still_denied = cloud_fallback
    state.cloud_pdf = state.pdf if cloud_fallback else None
    engine = flow_engine.FlowEngine(
        root=tmp_path / 'runtime' / 'finance_preaudit_flows',
        discover_candidates=lambda key: [{'request_id': '101', 'workflow_key': key,
                                         'received_at': '2026-09-01T09:00:00+08:00'}],
        collect_material=lambda _request_id, root: collect_case(
            '101', oa_api=state.client, configs=[CONFIG], allow_browser_download=True,
            attachment_root=root / 'attachments', prepared_root=root / 'prepared',
        ),
    )
    action = engine.next_action(CONFIG['flow_key'], mode='preview')
    view = read_material.read(Path(action['action_file']))
    assert 'original_source' in view and 'content_read' in view and NUMBER in view
    packet = json.loads(Path(action['review_packet_file']).read_text())
    frozen = json.loads(Path(packet['material_file']).read_text())
    assert frozen['invoice_evidence']['invoices'][0]['files'][0]['resource_id'] == '202'
    fingerprint = flow_engine._evidence_fingerprint(frozen)
    source = Path(frozen['resources'][0]['local_readonly_path'])
    source.chmod(0o600)
    source.write_bytes(b'changed original')
    with pytest.raises(ValueError, match='frozen_material_changed'):
        flow_engine.validate_frozen_evidence(
            material_file=Path(packet['material_file']), request_id='101',
            workflow_key=CONFIG['flow_key'],
            attachment_root=Path(packet['material_file']).parent / 'attachments',
            prepared_root=Path(packet['material_file']).parent / 'prepared',
            material_fingerprint=packet['material_fingerprint'], evidence_fingerprint=fingerprint,
        )


def test_invoice_document_warning_clears_only_for_exact_read_document(original_transport, tmp_path):
    state = original_transport
    fields = state.workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']
    for docid in ('303', '404'):
        fields.append({'fieldName': 'fpfj', 'fieldShowName': '发票附件',
                       'fieldValue': docid, 'filedHtmlShow': f'<a onclick="toDocument({docid})">原件</a>'})
    material = collect_original(state, tmp_path)
    assert [w['source_field_path'] for w in material['warnings']] == ['detail[0].row[0].field[2]']


@pytest.mark.parametrize('change', ['binding', 'cached_document'])
def test_original_binding_is_rechecked_even_when_download_is_already_readable(original_transport, tmp_path, change):
    state = original_transport
    state.initialized = True
    if change == 'binding':
        original_context = state.client.request_context
        calls = []

        def context(request_id):
            calls.append(request_id)
            if len(calls) > 1:
                state.detail['oa']['doc_id'] = '404'
            return original_context(request_id)

        state.client.request_context = context
    else:
        collect_original(state, tmp_path)
        state.main_file = '999'
    before = sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests) == before


def test_exact_native_deduplication_retains_original_verification(original_transport, tmp_path):
    state = original_transport
    state.initialized = True
    state.main_file = '999'
    state.client.request_context = lambda _request_id: (state.workflow, [
        {'id': '202', 'name': 'ordinary.pdf',
         'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202'},
    ])
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not any(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)


@pytest.mark.parametrize('partial', [False, True])
def test_material_diagnostic_counts_verified_body_and_rejects_unbound_claim(original_transport, tmp_path, partial):
    from apps.oa_finance_preaudit import review_diagnostics
    state = original_transport
    if partial:
        state.detail['flist'].append({'id': '8002', 'type': 'ofd', 'del': 0})
    material = collect_original(state, tmp_path)
    source = tmp_path / 'material.json'
    source.write_text(json.dumps(material, default=str))
    source.chmod(0o600)
    result = review_diagnostics.inspect_material(source)
    assert result['invoice_evidence']['body_partial_read_entries' if partial else 'body_read_entries'] == 1
    assert result['source_verification'] == 'not_performed'
    material['invoice_evidence']['invoices'][0]['files'][0]['resource_id'] = '999'
    source.write_text(json.dumps(material, default=str))
    result = review_diagnostics.inspect_material(source)
    assert result['invoice_evidence']['body_unknown_entries'] == 1
    assert result['invoice_evidence']['body_read_entries'] == 0
