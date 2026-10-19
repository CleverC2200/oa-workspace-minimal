from copy import deepcopy
import json

import pytest

from apps.oa_approval import overseas_invoice as inv
from tests.finance_preaudit.test_overseas_invoice import case, recognized, resources
from tests.finance_preaudit.test_overseas_invoice import harness as harness


@pytest.fixture
def empty_invoice(harness, monkeypatch):
    from apps.oa_approval import official_sql
    engine, api, _, _ = harness
    api.data['currentNodeId'] = inv.RPA_NODE_ID
    api.data['workflowMainTableInfo']['tableDBName'] = 'formtable_main_1295'
    table = api.data['workflowDetailTableInfos'][0]
    table['tableDBName'] = 'formtable_main_1295_dt1'
    field = table['workflowRequestTableRecords'][0]['workflowRequestTableFields'][-1]
    field.update(fieldValue='', fieldHtmlType='6', fieldShowValue='[该字段暂不支持]',
                 filedHtmlShow="<span style='color:#ACA899'>[该字段暂不支持]</span>")
    api.resources = []
    api.source_rows = [{'requestid': 901, 'detail_id': 4, 'scfp': None}]
    api.source_queries = []
    api.source_error = None
    api.request_context = lambda _request: (deepcopy(api.data), deepcopy(api.resources))
    def query(name, params, **kwargs):
        api.source_queries.append((name, params, kwargs))
        if api.source_error:
            raise api.source_error
        return deepcopy([row for row in api.source_rows if row['detail_id'] == params['detail_id']])
    monkeypatch.setattr(official_sql, '_query_rows', query)
    return engine, api


def freeze_empty(engine):
    attempt = engine.collect('901')
    material = engine.read_material(attempt)
    engine.freeze(attempt, {'material_digest': inv.digest(material), 'invoices': []})
    return attempt, material


def test_empty_invoice_submits_without_backfill_and_preserves_bound_source(empty_invoice):
    engine, api = empty_invoice
    before = deepcopy(api.data)
    attempt, material = freeze_empty(engine)
    engine.authorize(confirm='YES', allow_no_invoice=True)
    receipt = engine.submit(attempt, confirm='YES')
    assert receipt['status'] == 'submitted'
    assert api.calls[0][2] == []
    assert api.data == before
    assert material['attachments'] == []
    assert material['snapshot']['rows'][0]['invoice_empty_source'] == {
        'source': 'official_sql', 'request_id': '901', 'detail_id': '4',
        'table': 'formtable_main_1295_dt1', 'field_name': 'scfp'}
    assert len(api.source_queries) >= 4
    assert all(query == ('overseas_invoice_attachment_fields',
                        {'request_id': 901, 'detail_id': 4}, {'require_connection': True})
               for query in api.source_queries)
    assert engine.submit(attempt, confirm='YES') == receipt
    assert len(api.calls) == 1


def test_pending_discovery_filters_other_flows_and_orders_exact_candidates(empty_invoice):
    engine, api = empty_invoice
    original = deepcopy(api.pending[0])
    api.pending = [
        {**original, 'request_id': '903', 'received_at': '2026-09-18 10:00:00'},
        {**original, 'request_id': '905', 'workflow_id': 'other', 'workflow_name': 'other'},
        {**original, 'request_id': '902'},
        original,
    ]
    engine._write(engine.root / 'receipts' / (inv.digest('902') + '.json'),
                  {'status': 'unknown', 'attempt': 'existing'})
    rows = engine.pending()
    assert [row['request_id'] for row in rows] == ['901', '902', '903']
    assert rows[1]['receipt_status'] == 'unknown'


@pytest.mark.parametrize('change', ['actor', 'workflow_name', 'duplicate'])
def test_pending_discovery_rejects_conflicting_identity(empty_invoice, change):
    engine, api = empty_invoice
    if change == 'actor': api.pending[0]['operator_id'] = '88'
    if change == 'workflow_name': api.pending[0]['workflow_name'] = 'other'
    if change == 'duplicate': api.pending *= 2
    with pytest.raises(ValueError):
        engine.pending()



def test_empty_invoice_requires_expanded_independent_authorization(empty_invoice):
    engine, api = empty_invoice
    attempt, _ = freeze_empty(engine)
    engine.authorize(confirm='YES')
    with pytest.raises(ValueError, match='no_invoice_not_authorized'):
        engine.submit(attempt, confirm='YES')
    assert not api.calls


@pytest.mark.parametrize('change', ['nonnull', 'unavailable', 'missing', 'duplicate', 'wrong_request', 'wrong_node', 'html_conflict'])
def test_unconfirmed_empty_invoice_never_creates_submission(empty_invoice, change):
    engine, api = empty_invoice
    if change == 'nonnull': api.source_rows[0]['scfp'] = 'unread-document'
    if change == 'unavailable': api.source_error = RuntimeError('official_sql_unavailable')
    if change == 'missing': api.source_rows = []
    if change == 'duplicate': api.source_rows *= 2
    if change == 'wrong_request': api.source_rows[0]['requestid'] = 902
    if change == 'wrong_node': api.data['currentNodeId'] = '999'
    if change == 'html_conflict':
        api.data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'][-1]['filedHtmlShow'] = '<span onclick="toDocument(99)">[该字段暂不支持]</span>'
    with pytest.raises(ValueError):
        engine.collect('901')
    assert not api.calls


def test_new_attachment_between_freeze_and_submit_blocks_empty_invoice(empty_invoice):
    engine, api = empty_invoice
    attempt, _ = freeze_empty(engine)
    engine.authorize(confirm='YES', allow_no_invoice=True)
    api.source_rows[0]['scfp'] = 'new-document'
    with pytest.raises(ValueError, match='attachment_unbound'):
        engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_mixed_rows_only_backfill_the_bound_invoice(empty_invoice):
    engine, api = empty_invoice
    records = api.data['workflowDetailTableInfos'][0]['workflowRequestTableRecords']
    second = deepcopy(case()['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0])
    second['recordOrder'] = 19
    records.append(second)
    api.resources = resources()
    before = deepcopy(records[0])
    attempt = engine.collect('901')
    engine.freeze(attempt, recognized(engine.read_material(attempt)))
    engine.authorize(confirm='YES', allow_no_invoice=True)
    assert engine.submit(attempt, confirm='YES')['status'] == 'submitted'
    submitted = api.calls[0][2][0]['workflowRequestTableRecords']
    assert [row['recordOrder'] for row in submitted] == [19]
    assert records[0] == before


def test_empty_invoice_unknown_never_retries_and_postcheck_retains_receipt(empty_invoice):
    engine, api = empty_invoice
    attempt, _ = freeze_empty(engine)
    engine.authorize(confirm='YES', allow_no_invoice=True)
    original = api.submit_invoice
    def lost_response(*args):
        original(*args)
        raise TimeoutError('synthetic lost response')
    api.submit_invoice = lost_response
    assert engine.submit(attempt, confirm='YES')['status'] == 'unknown'
    journal = engine.root / 'receipts' / (inv.digest('901') + '.json')
    original_bytes = journal.read_bytes()
    assert engine.postcheck(attempt)['status'] == 'confirmed_submitted'
    assert journal.read_bytes() == original_bytes
    assert engine.submit(attempt, confirm='YES')['status'] == 'unknown'
    assert len(api.calls) == 1


def test_active_finance_controller_blocks_overseas_write(empty_invoice, monkeypatch):
    from apps.oa_approval import runtime_control
    engine, api = empty_invoice
    attempt, _ = freeze_empty(engine)
    engine.authorize(confirm='YES', allow_no_invoice=True)
    monkeypatch.setattr(runtime_control, 'controller_active', lambda: True)
    with pytest.raises(ValueError, match='controller_active'):
        engine.submit(attempt, confirm='YES')
    assert not api.calls


def test_pending_cli_and_no_invoice_freeze_use_current_identity(empty_invoice, monkeypatch, capsys):
    from scripts import overseas_invoice as cli
    engine, api = empty_invoice
    monkeypatch.setattr(cli.auth, 'load_local_env', lambda: None)
    monkeypatch.setattr(cli.auth, 'env', lambda key: {'OA_UID': '77', 'OA_APP_SECRET': 'test-key'}[key])
    monkeypatch.setattr(cli, 'ROOT', engine.root)
    monkeypatch.setattr(cli.overseas_invoice_api, 'build_api', lambda: api)
    monkeypatch.setattr(cli, '_production_guard', lambda: None)
    assert cli.main(['pending']) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row['request_id'] for row in result['pending']] == ['901']
    assert result['pending'][0]['receipt_status'] is None
    attempt = engine.collect('901')
    assert cli.main(['freeze', '--attempt', attempt, '--no-invoice']) == 0
    assert json.loads(capsys.readouterr().out)['rows'] == 0
    assert cli.main(['authorize', '--confirm-write', 'YES', '--allow-no-invoice', 'YES']) == 0
    capsys.readouterr()
    assert cli.main(['status']) == 0
    assert json.loads(capsys.readouterr().out)['no_invoice_enabled'] is True
    assert cli.main(['submit', '--attempt', attempt, '--confirm-write', 'YES']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'submitted'
    assert len(api.calls) == 1

