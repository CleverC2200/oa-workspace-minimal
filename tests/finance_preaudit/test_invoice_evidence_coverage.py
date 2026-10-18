from __future__ import annotations

# Select the retained supplier explicitly for recorded CLI/slot behavior.
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401

import json

import pytest

from apps.oa_approval import invoice_buyers
from apps.oa_finance_preaudit.case_bundle import collect_case


CONFIG = {
    'flow_key': 'tpm_small_marketing_fee',
    'workflow_id': '195039',
    'workflow_name': 'TPM小额市场费用报销申请流程',
}
NUMBER = '00000000000000000101'


def linked_field(data_id='101', number=NUMBER):
    return {'fieldName': 'glfp', 'fieldShowName': '关联发票',
            'fieldValue': data_id, 'fieldShowValue': number}


class InvoiceApi:
    def __init__(self, rows=None, config=None):
        self.config = config or CONFIG
        self.rows = rows or [[linked_field()]]

    def request_context(self, _request_id):
        return {'workflowBaseInfo': {
            'workflowId': self.config['workflow_id'],
            'workflowName': self.config['workflow_name'],
        }, 'workflowDetailTableInfos': [{'workflowRequestTableRecords': [
            {'workflowRequestTableFields': fields} for fields in self.rows
        ]}]}, []

    def get(self, path, params=None):
        if path == invoice_buyers.TOKEN_PATH:
            return {'status': '1', 'token': 'synthetic-token'}
        if path == invoice_buyers.MAPPING_PATH:
            return {'status': '1', 'targetIds': 'cloud-' + params['ids']}
        raise AssertionError('Unexpected external API')


@pytest.fixture
def cloud_http(monkeypatch):
    payloads = {'101': {
        'number': NUMBER,
        'comm_info': {
            'pro': {'cname': '电子发票（铁路电子客票）', 'type': 47, 'date': '2026-09-01'},
            'price': {'total': '43.00'},
            'buyer': {'company': '示例食品有限公司'},
            'payer': {},
        },
        'flist': [{'id': 'file-101', 'type': 'pdf', 'size': 1024, 'del': 0,
                   'name': 'private-name.pdf', 'url': 'https://private.invalid/file'}],
        'user': {'name': 'private-person'},
        'modify_info': {'pdf': 'https://private.invalid/token'},
    }}

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps({'actionMsg': {'code': '0'}, 'data': self.data}).encode()

    def urlopen(request, **_kwargs):
        data_id = json.loads(request.data)['dataId']
        return Response(payloads[data_id])

    monkeypatch.setattr(invoice_buyers.safe_http, 'urlopen', urlopen)
    return payloads


def collect(api=None):
    api = api or InvoiceApi()
    return collect_case('synthetic-request', oa_api=api, configs=[api.config],
                        allow_browser_download=False)


def test_linked_rail_ticket_retains_authoritative_fields_without_duplicate_upload(cloud_http):
    material = collect()
    entry = material['invoice_evidence']['invoices'][0]
    assert entry['invoice_no'] == NUMBER
    assert entry['data_id'] == '101'
    assert entry['source_field_paths'] == ['detail[0].row[0].field[0]']
    assert entry['fields']['invoice_type'] == {
        'value': '电子发票（铁路电子客票）', 'read_status': 'present',
    }
    assert entry['fields']['total'] == {'value': '43.00', 'read_status': 'present'}
    assert entry['fields']['seller'] == {'value': '', 'read_status': 'not_provided'}
    assert entry['files'] == [{
        'file_id': 'file-101', 'media_type': 'pdf', 'size_bytes': 1024,
        'read_status': 'record_present', 'body_read_status': 'not_read',
    }]
    assert material['resources'] == []
    assert material['prepared']['images'] == []
    assert entry['body_read_status'] == 'not_read'
    assert not any(fact['name'] == '发票销售方' for fact in material['authority_facts'])
    assert not any(sentinel in json.dumps(material) for sentinel in (
        'private-name', 'private-person', 'private.invalid', 'synthetic-token',
    ))


def test_cloud_invoice_number_conflict_never_reaches_trusted_material(cloud_http):
    cloud_http['101']['number'] = '00000000000000000999'
    with pytest.raises(RuntimeError, match='^invoice_buyer_read_failed$'):
        collect()


def test_each_invoice_preserves_exact_multiple_row_binding_and_partial_fields(cloud_http):
    cloud_http['202'] = {'number': '00000000000000000202', 'comm_info': {
        'pro': {'cname': '增值税普通发票'},
        'price': {'total': '74.00'},
    }}
    material = collect(InvoiceApi(rows=[
        [linked_field()], [linked_field()],
        [linked_field('202', '00000000000000000202')],
    ]))
    entries = material['invoice_evidence']['invoices']
    assert [entry['invoice_no'] for entry in entries] == [NUMBER, '00000000000000000202']
    assert entries[0]['source_field_paths'] == [
        'detail[0].row[0].field[0]', 'detail[0].row[1].field[0]',
    ]
    assert entries[1]['source_field_paths'] == ['detail[0].row[2].field[0]']
    assert entries[1]['fields']['issue_date']['read_status'] == 'not_provided'
    assert entries[1]['fields']['buyer']['read_status'] == 'not_provided'
    assert entries[1]['file_records_status'] == 'not_provided'
    assert entries[1]['body_read_status'] == 'not_read'
    facts = {(fact['name'], fact['subject_ref']): fact['value'] for fact in material['authority_facts']}
    assert facts[('发票价税合计', NUMBER)] == '43.00'
    assert facts[('发票价税合计', '00000000000000000202')] == '74.00'
    assert facts[('发票类型', NUMBER)] == '电子发票（铁路电子客票）'
    assert facts[('发票开票日期', NUMBER)] == '2026-09-01'


def test_worker_reads_frozen_invoice_coverage_and_preview_closes_without_write(
    tmp_path, monkeypatch, cloud_http,
):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-fake-key")
    import io
    from pathlib import Path

    from apps import json_artifact
    from apps.oa_finance_preaudit import codex_review_worker, flow_engine, read_material

    project = tmp_path / 'project'
    engine = flow_engine.FlowEngine(
        root=project / 'runtime' / 'finance_preaudit_flows',
        discover_candidates=lambda key: [{
            'request_id': 'synthetic-request', 'workflow_key': key,
            'received_at': '2026-09-01T09:00:00+08:00',
        }],
        collect_material=lambda _request_id, _root: collect(),
    )
    action = engine.next_action(CONFIG['flow_key'], mode='preview')
    packet = json.loads(Path(action['review_packet_file']).read_text())
    action_file = Path(action['action_file'])
    observed = []

    class ReviewerProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, output):
            self.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            self.decision_file = Path(command[command.index('--output-last-message') + 1])
            self.output = output

        def communicate(self, *, input, timeout):
            assert 'material.invoice_evidence' in input
            view = read_material.read(action_file)
            assert '[material.invoice_evidence]' in view
            assert '电子发票（铁路电子客票）' in view and '43.00' in view
            assert 'not_read' in view and 'not_provided' in view
            assert '明细表1第1行：detail[0].row[0]' in view
            assert 'detail[0].row[0].field[0]' in view
            observed.append(view)
            # Transport stub: all business conclusions remain unverified.
            json_artifact.write_json_atomic(self.decision_file, {
                'schema_version': 1, 'kind': 'preaudit_review_decision',
                'action_token': action['action_token'],
                'workflow_key': CONFIG['flow_key'], 'rule_version': packet['rule_version'],
                'overall': 'needs_human',
                'company_comparison': {
                    'observed_refs': [record['evidence_ref'] for record in packet['company_comparison_contract']['records']],
                    'result': 'unknown',
                    'reason': '示例食品有限公司的公司角色尚未核对。',
                },
                'items': [{
                    'item_key': item['item_key'], 'status': 'uncertain',
                    'conclusion': '该项目业务材料尚未核验，请财务核对原单。',
                    'evidence_refs': ['material.invoice_evidence.invoices[0].fields.total'],
                    'missing_evidence': ['该项目业务材料尚未核验'],
                } for item in packet['items']],
            })
            self.output.write('{"type":"turn.completed"}\n')
            self.output.flush()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(codex_review_worker.subprocess, 'Popen',
                        lambda command, **kwargs: ReviewerProcess(command, kwargs['stdout']))
    result = codex_review_worker.review({
        'project_root': str(project), 'action_file': str(action_file), 'timeout_seconds': 60,
    })
    assert result['status'] == 'reviewed'
    assert observed and result['write_state'] == 'not_requested'
    decision_file = action_file.with_name('review-decision.json')
    json_artifact.write_json_atomic(decision_file, result['decision'])
    closed = engine.complete_review(action_file, decision_file)
    assert closed['action'] == 'closed'
    assert closed['business_state'] == 'needs_human'
    assert closed['write_state'] == 'not_requested'


@pytest.mark.parametrize('config', [
    {'flow_key': 'travel_expense', 'workflow_id': '138023', 'workflow_name': '差旅费报销流程'},
    {'flow_key': 'business_entertainment', 'workflow_id': '138024', 'workflow_name': '业务招待费报销申请流程'},
    {'flow_key': 'business_support_fee', 'workflow_id': '138025', 'workflow_name': '业务支持费报销流程'},
    {'flow_key': 'small_marketing_fee', 'workflow_id': '138026', 'workflow_name': '小额市场费用报销申请流程'},
    CONFIG,
    {'flow_key': 'general_expense', 'workflow_id': '195021', 'workflow_name': '通用费用报销申请流程'},
])
def test_six_finance_collectors_preserve_same_invoice_coverage(config, monkeypatch, cloud_http):
    from apps.oa_approval import travel_orders

    api = InvoiceApi(config=config)
    if config['flow_key'] == 'travel_expense':
        original = api.request_context

        def request_context(request_id):
            workflow, resources = original(request_id)
            workflow['workflowMainTableInfo'] = {'requestRecords': [{
                'workflowRequestTableFields': [{
                    'fieldName': 'lcbh', 'fieldShowName': '流程编号',
                    'fieldShowValue': 'SYNTHETIC-TRAVEL',
                }, {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': '0'}],
            }]}
            return workflow, resources

        api.request_context = request_context
        # Browser I/O boundaries only; collection and relation checks run normally.
        monkeypatch.setattr(travel_orders, '_ensure_cdp_available', lambda: True)
        monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', lambda *_a, **_k: [{
            'text': 'SYNTHETIC-TRAVEL', 'resources': [], 'attachment_scan_status': 'empty',
        }])
    material = collect_case('synthetic-request', oa_api=api, configs=[config],
                            allow_browser_download=config['flow_key'] == 'travel_expense')
    assert material['invoice_evidence']['invoices'][0]['fields']['total']['value'] == '43.00'
    assert material['invoice_evidence']['invoices'][0]['fields']['seller']['read_status'] == 'not_provided'
    assert any(fact['name'] == '发票价税合计' and fact['value'] == '43.00'
               for fact in material['authority_facts'])


def test_service_failure_stops_before_review_and_retains_detail(tmp_path, monkeypatch):
    from pathlib import Path
    from apps.oa_finance_preaudit import flow_engine

    monkeypatch.setattr(invoice_buyers.safe_http, 'urlopen',
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError('synthetic-read-failure')))
    with pytest.raises(RuntimeError, match='^invoice_buyer_read_failed$'):
        collect()
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda key: [{'request_id': 'synthetic-request', 'workflow_key': key,
                                              'received_at': '2026-09-01T09:00:00+08:00'}],
        collect_material=lambda _request, _root: collect(),
    )
    action = engine.next_action(CONFIG['flow_key'], mode='preview')
    assert action['action'] == 'needs_admin'
    assert action['reason'] == 'material_collection_failed'
    assert action['failure_detail_code'] == 'invoice_buyer_read_failed'
    assert not Path(action['action_file']).with_name('review-packet.json').exists()
    assert action['write_state'] == 'not_requested'


def test_changing_new_invoice_fields_breaks_frozen_material_before_preview_completion(tmp_path, cloud_http):
    from pathlib import Path
    from apps import json_artifact
    from apps.oa_finance_preaudit import flow_engine, read_material

    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda key: [{'request_id': 'synthetic-request', 'workflow_key': key,
                                              'received_at': '2026-09-01T09:00:00+08:00'}],
        collect_material=lambda _request, _root: collect(),
    )
    action = engine.next_action(CONFIG['flow_key'], mode='preview')
    packet = json.loads(Path(action['review_packet_file']).read_text())
    material_path = Path(packet['material_file'])
    material = json.loads(material_path.read_text())
    material['invoice_evidence']['invoices'][0]['fields']['total']['value'] = '999.00'
    json_artifact.write_json_atomic(material_path, material)
    with pytest.raises(ValueError, match='^frozen_material_changed$'):
        read_material.read(Path(action['action_file']))
    decision_path = tmp_path / 'decision.json'
    json_artifact.write_json_atomic(decision_path, {})
    result = engine.complete_review(Path(action['action_file']), decision_path)
    assert result['action'] == 'needs_admin'
    assert result['reason'] == 'frozen_material_changed'
    assert result['write_state'] == 'not_requested'


def test_deleted_and_malformed_cloud_files_do_not_become_readable_evidence(cloud_http):
    cloud_http['101']['flist'] = [
        {'id': 'deleted-file', 'type': 'pdf', 'del': '1', 'size': 10},
        {'id': {'token': 'private-file-token'}, 'type': 'pdf', 'del': 0},
        {'id': 'valid-file', 'type': 'jpg', 'del': 0, 'size': True},
        {'type': 'pdf'},
        'not-a-file-record',
    ]
    entry = collect()['invoice_evidence']['invoices'][0]
    assert entry['files'] == [{
        'file_id': 'valid-file', 'media_type': 'jpg',
        'read_status': 'record_present', 'body_read_status': 'not_read',
    }]
    assert 'private-file-token' not in json.dumps(entry)


def test_cloud_number_missing_does_not_replace_oa_link_with_inferred_fact(cloud_http):
    del cloud_http['101']['number']
    entry = collect()['invoice_evidence']['invoices'][0]
    assert entry['invoice_no'] == NUMBER
    assert entry['fields']['invoice_number'] == {'value': '', 'read_status': 'not_provided'}
    assert entry['fields']['total'] == {'value': '43.00', 'read_status': 'present'}
