from copy import deepcopy
from pathlib import Path
import json

import pytest

from apps.oa_approval import overseas_invoice as inv


def case():
    fields = [
        {'fieldName': name, 'fieldValue': value, 'edit': True, 'fieldId': str(i + 10)}
        for i, (name, value) in enumerate([
            ('zfje', '10'), ('fpje', '55'), ('fphm', ''), ('fpdqr', ''), ('scfp', 'doc-1')
        ])
    ]
    fields[-1]['filedHtmlShow'] = '<a href="/news/download.do?fileid=601&filename=invoice.pdf">invoice.pdf</a>'
    return {
        'requestId': '901', 'canEdit': True, 'currentNodeId': '5', 'lastOperateTime': '2026-09-17',
        'workflowBaseInfo': {'workflowId': inv.WORKFLOW_ID, 'workflowName': inv.WORKFLOW_NAME},
        'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
            {'fieldName': 'bz', 'fieldValue': 'USD'}, {'fieldName': 'zfjehj', 'fieldValue': '10'}]}]},
        'workflowDetailTableInfos': [{'tableDBName': 'formtable_main_100_dt1',
            'workflowRequestTableRecords': [{'recordOrder': 4, 'workflowRequestTableFields': fields}]}],
    }


def resources():
    return [{'id': '601', 'name': 'invoice.pdf', 'downloadUrl': '/weaver/weaver.file.FileDownload?fileid=601'}]


def recognized(material):
    return {'material_digest': inv.digest(material), 'invoices': [{
        'resource_id': '601', 'sha256': material['attachments'][0]['sha256'],
        'currency': 'USD', 'fpje': {'status': 'recognized', 'value': '55.00', 'page': 1, 'quote': 'TOTAL USD 55.00'},
        'fphm': {'status': 'recognized', 'value': 'INV-01', 'page': 1, 'quote': 'INVOICE NO INV-01'},
        'fpdqr': {'status': 'missing', 'value': None, 'page': 1, 'quote': 'PREPAYMENT'},
    }]}


class Api:
    def __init__(self):
        self.data = case()
        self.pending = [{'request_id': '901', 'workflow_id': inv.WORKFLOW_ID,
                         'workflow_name': inv.WORKFLOW_NAME, 'operator_id': '77', 'received_at': '2026-09-17 10:00:00'}]
        self.done = []
        self.calls = []
        self.mode = 'success'

    def pending_rows(self):
        return deepcopy(self.pending)

    def handled_rows(self, **kwargs):
        return deepcopy(self.done)

    def request_context(self, request_id):
        return deepcopy(self.data), resources()

    def submit_invoice(self, request_id, remark, details):
        self.calls.append((request_id, remark, deepcopy(details)))
        if self.mode == 'timeout':
            raise TimeoutError('sensitive remote message')
        if self.mode == 'rejected':
            return {'code': 'ERROR'}
        if self.mode != 'ignored_fields':
            for table in details:
                original = next(t for t in self.data['workflowDetailTableInfos'] if t['tableDBName'] == table['tableDBName'])
                for record in table['workflowRequestTableRecords']:
                    row = next(r for r in original['workflowRequestTableRecords'] if r['recordOrder'] == record['recordOrder'])
                    values = {f['fieldName']: f['fieldValue'] for f in record['workflowRequestTableFields']}
                    for field in row['workflowRequestTableFields']:
                        if field['fieldName'] in values: field['fieldValue'] = values[field['fieldName']]
        self.done = deepcopy(self.pending)
        self.pending = []
        return {'code': 'SUCCESS'}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    from apps.oa_approval import runtime_control
    monkeypatch.setenv('OA_FROZEN_WRITER_LOCK_FILE', str(tmp_path / 'global.lock'))
    monkeypatch.setattr(runtime_control, 'stop_requested', lambda: False)
    api = Api()
    def download(rs, directory):
        directory.mkdir(parents=True, exist_ok=True)
        result = []
        for r in rs:
            p = directory / (str(r['id']) + '.pdf')
            p.write_bytes(b'%PDF-fixture-original')
            result.append({'resource_id': str(r['id']), 'path': str(p), 'pages': 1})
        return result
    engine = inv.InvoiceFlow(tmp_path / 'invoice', api, '77', 'test-key', download)
    attempt = engine.collect('901')
    material = engine.read_material(attempt)
    return engine, api, attempt, material


def authorize(engine):
    engine.authorize(confirm='YES')


def test_partial_recognition_preserves_other_fields_and_uses_original_row(harness):
    engine, api, attempt, material = harness
    before = deepcopy(api.data)
    engine.freeze(attempt, recognized(material))
    authorize(engine)
    result = engine.submit(attempt, confirm='YES')
    assert result['status'] == 'submitted'
    submitted = api.data
    assert set(api.calls[0][2][0]) == {'tableDBName', 'workflowRequestTableRecords'}
    assert submitted['workflowMainTableInfo'] == before['workflowMainTableInfo']
    row = submitted['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]
    assert row['recordOrder'] == 4
    values = {f['fieldName']: f['fieldValue'] for f in row['workflowRequestTableFields']}
    assert values == {'zfje': '10', 'fpje': '55.00', 'fphm': 'INV-01', 'fpdqr': '', 'scfp': 'doc-1'}
    assert '到期日' in api.calls[0][1]
    assert engine.submit(attempt, confirm='YES') == result
    assert len(api.calls) == 1


@pytest.mark.parametrize('change', ['actor', 'workflow', 'field', 'row', 'attachment'])
def test_changed_target_never_submits(harness, change):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    row = api.data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]
    if change == 'actor': api.pending[0]['operator_id'] = '88'
    if change == 'workflow': api.data['workflowBaseInfo']['workflowId'] = 'other'
    if change == 'field': row['workflowRequestTableFields'][0]['fieldValue'] = '99'
    if change == 'row': row['recordOrder'] = 9
    if change == 'attachment': row['workflowRequestTableFields'][-1]['fieldValue'] = 'new-doc'
    with pytest.raises(ValueError): engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_independent_authorization_required_and_exact(harness):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material))
    with pytest.raises(ValueError, match='authorization'): engine.submit(attempt, confirm='YES')
    authorize(engine)
    p = engine.root / 'write-policy.json'
    policy = json.loads(p.read_text()); policy['fields'].append('zfje'); p.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match='authorization'): engine.submit(attempt, confirm='YES')
    assert not api.calls


@pytest.mark.parametrize('mode', ['timeout', 'ignored_fields', 'rejected'])
def test_failed_or_uncertain_write_never_retries_even_new_attempt(harness, mode):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine); api.mode = mode
    result = engine.submit(attempt, confirm='YES')
    assert result['status'] != 'submitted'
    assert engine.submit(attempt, confirm='YES') == result
    assert len(api.calls) == 1
    api.pending = [{'request_id': '901', 'workflow_id': inv.WORKFLOW_ID, 'workflow_name': inv.WORKFLOW_NAME,
                    'operator_id': '77', 'received_at': '2026-09-17 10:00:00'}]
    second = engine.collect('901')
    engine.freeze(second, recognized(engine.read_material(second)))
    with pytest.raises(ValueError, match='already_attempted'): engine.submit(second, confirm='YES')
    assert len(api.calls) == 1


@pytest.mark.parametrize('change', ['foreign_attachment', 'bad_hash', 'extra_field', 'wrong_currency', 'bad_date', 'nonfinite', 'invented_status'])
def test_invalid_recognition_is_rejected(harness, change):
    engine, api, attempt, material = harness
    rec = recognized(material); row = rec['invoices'][0]
    if change == 'foreign_attachment': row['resource_id'] = '602'
    if change == 'bad_hash': row['sha256'] = 'bad'
    if change == 'extra_field': row['zfje'] = row['fpje']
    if change == 'wrong_currency': row['currency'] = 'EUR'
    if change == 'bad_date': row['fpdqr'] = {'status': 'recognized', 'value': '2026-02-30', 'page': 1, 'quote': 'Due date'}
    if change == 'nonfinite': row['fpje']['value'] = 'NaN'
    if change == 'invented_status': row['fphm']['status'] = 'guessed'
    with pytest.raises(ValueError): engine.freeze(attempt, rec)
    assert not api.calls


def test_modified_local_attachment_and_plan_block_writes(harness):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    p = engine.root / 'attempts' / attempt / material['attachments'][0]['path']
    p.write_bytes(b'changed')
    with pytest.raises(ValueError): engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_stop_and_missing_confirmation_block(harness, monkeypatch):
    from apps.oa_approval import runtime_control
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    with pytest.raises(ValueError, match='confirmation'): engine.submit(attempt, confirm='NO')
    monkeypatch.setattr(runtime_control, 'stop_requested', lambda: True)
    with pytest.raises(ValueError, match='stopped'): engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_unknown_journal_written_before_network_and_survives_interruption(harness):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    def interrupted(*args):
        journal = engine.root / 'receipts' / (inv.digest('901') + '.json')
        assert engine._read(journal)['status'] == 'unknown'
        api.calls.append(args)
        raise KeyboardInterrupt()
    api.submit_invoice = interrupted
    with pytest.raises(KeyboardInterrupt): engine.submit(attempt, confirm='YES')
    assert engine.submit(attempt, confirm='YES')['status'] == 'unknown'
    assert len(api.calls) == 1


def test_changed_remote_bytes_block_before_submit(harness):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    old = engine.download
    def changed(rs, directory):
        files = old(rs, directory)
        Path(files[0]['path']).write_bytes(b'%PDF-changed-original')
        return files
    engine.download = changed
    with pytest.raises(ValueError, match='attachment_changed'): engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_expired_and_tampered_plan_block(harness):
    from datetime import datetime, timezone, timedelta
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    path = engine.root / 'attempts' / attempt / 'plan.json'
    payload = engine._read(path)
    payload['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    engine._write(path, payload)
    with pytest.raises(ValueError, match='expired'): engine.submit(attempt, confirm='YES')
    document = json.loads(path.read_text()); document['payload']['expires_at'] = '2099-01-01T00:00:00+00:00'
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='artifact_invalid'): engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_conflicting_attachment_binding_is_rejected(harness):
    engine, api, _, _ = harness
    table = api.data['workflowDetailTableInfos'][0]
    duplicate = deepcopy(table['workflowRequestTableRecords'][0]); duplicate['recordOrder'] = 9
    table['workflowRequestTableRecords'].append(duplicate)
    with pytest.raises(ValueError, match='unbound'): engine.collect('901')


def test_multiple_rows_keep_their_own_invoice(harness):
    engine, api, _, _ = harness
    table = api.data['workflowDetailTableInfos'][0]
    second = deepcopy(table['workflowRequestTableRecords'][0]); second['recordOrder'] = 19
    second['workflowRequestTableFields'][-1]['filedHtmlShow'] = '<a href="/news/download.do?fileid=602&filename=invoice.pdf">invoice.pdf</a>'
    second['workflowRequestTableFields'][-1]['fieldValue'] = 'doc-2'
    table['workflowRequestTableRecords'].append(second)
    attempt = engine.collect('901'); material = engine.read_material(attempt)
    rec = recognized(material); other = deepcopy(rec['invoices'][0]); other['resource_id'] = '602'
    other['sha256'] = material['attachments'][1]['sha256']; other['fphm']['value'] = 'INV-02'
    rec['invoices'].insert(0, other)
    engine.freeze(attempt, rec); authorize(engine)
    assert engine.submit(attempt, confirm='YES')['status'] == 'submitted'
    rows = api.data['workflowDetailTableInfos'][0]['workflowRequestTableRecords']
    assert [(r['recordOrder'], next(f['fieldValue'] for f in r['workflowRequestTableFields'] if f['fieldName'] == 'fphm')) for r in rows] == [(4, 'INV-01'), (19, 'INV-02')]


def test_transport_uses_only_official_detail_payload(monkeypatch):
    from apps.oa_approval.overseas_invoice_api import InvoiceApi
    from urllib.parse import parse_qs
    from unittest.mock import Mock
    response = Mock(); response.read.return_value = b'{"code":"SUCCESS"}'
    context = Mock(); context.__enter__ = Mock(return_value=response); context.__exit__ = Mock(return_value=None)
    request = Mock(return_value=context)
    monkeypatch.setattr('apps.oa_approval.safe_http.urlopen', request)
    api = InvoiceApi('token-for-test', 'actor-for-test', app_id='test', base_url='https://example.invalid')
    details = [{'tableDBName': 'formtable_main_100_dt1', 'workflowRequestTableRecords': [{
        'recordOrder': 4, 'workflowRequestTableFields': [{'fieldName': 'fphm', 'fieldValue': 'INV-01'}]}]}]
    assert api.submit_invoice('901', '识别完成', details)['code'] == 'SUCCESS'
    req = request.call_args.args[0]
    body = parse_qs(req.data.decode())
    assert req.full_url == 'https://example.invalid/api/workflow/paService/submitRequest'
    assert set(body) == {'requestId', 'remark', 'detailData', 'otherParams'}
    assert json.loads(body['detailData'][0]) == details
    assert json.loads(body['otherParams'][0]) == {'src': 'submit', 'judgeFormMustInput': '1'}
    details[0]['deleteAll'] = '1'
    with pytest.raises(ValueError): api.submit_invoice('901', '识别完成', details)
    assert request.call_count == 1


def test_fresh_download_never_reads_cache(tmp_path, monkeypatch):
    from apps.oa_approval import attachments
    p = tmp_path / 'invoice.pdf'; p.write_bytes(b'%PDF original')
    monkeypatch.setattr(attachments.runtime_control, 'stop_requested', lambda: False)
    def forbidden(*args, **kwargs):
        raise AssertionError('must not read cached attachment')
    monkeypatch.setattr(attachments, '_cached_attachment_entry', forbidden)
    monkeypatch.setattr(attachments, '_download_resource_direct', lambda *a, **kw: (p, ''))
    monkeypatch.setattr(attachments, '_record_attachment_entry', lambda *a, **kw: None)
    files, errors = attachments.download_resources(resources(), dest_dir=tmp_path, timeout_sec=1, allow_browser_download=True, reuse_cache=False)
    assert not errors and len(files) == 1 and files[0]['reused'] is False


def test_development_cli_cannot_enable_or_submit(monkeypatch, tmp_path):
    from scripts import overseas_invoice as cli
    monkeypatch.setattr(cli, 'PROJECT_ROOT', tmp_path / 'dev')
    monkeypatch.setattr(cli, 'PRODUCTION_ROOT', tmp_path / 'prd')
    with pytest.raises(ValueError, match='production_checkout_required'): cli._production_guard()


def test_postcheck_accepts_numeric_storage_format_but_not_wrong_amount(harness):
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    original = api.submit_invoice
    def normalized(*args):
        result = original(*args)
        row = api.data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]
        next(f for f in row['workflowRequestTableFields'] if f['fieldName'] == 'fpje')['fieldValue'] = '55'
        return result
    api.submit_invoice = normalized
    assert engine.submit(attempt, confirm='YES')['status'] == 'submitted'


def test_zero_record_order_cannot_create_a_new_row(harness):
    engine, api, _, _ = harness
    api.data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['recordOrder'] = 0
    with pytest.raises(ValueError, match='row_identity_missing'): engine.collect('901')


def test_concurrent_submissions_send_once(harness):
    from concurrent.futures import ThreadPoolExecutor
    engine, api, attempt, material = harness
    engine.freeze(attempt, recognized(material)); authorize(engine)
    with ThreadPoolExecutor(2) as pool:
        calls = [pool.submit(engine.submit, attempt, confirm='YES') for _ in range(2)]
        results = [call.result() for call in calls]
    assert results[0] == results[1]
    assert len(api.calls) == 1


def test_production_guard_requires_production_working_directory(monkeypatch, tmp_path):
    from scripts import overseas_invoice as cli
    from unittest.mock import Mock
    production = tmp_path / 'prd'; production.mkdir()
    monkeypatch.setattr(cli, 'PROJECT_ROOT', production)
    monkeypatch.setattr(cli, 'PRODUCTION_ROOT', production)
    process = Mock(side_effect=AssertionError('must reject before running git'))
    monkeypatch.setattr(cli.subprocess, 'run', process)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match='production_checkout_required'): cli._production_guard()
    assert not process.called
