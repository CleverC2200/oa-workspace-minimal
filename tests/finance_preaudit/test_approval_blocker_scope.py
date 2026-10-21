import json
from pathlib import Path
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import app_review_shutdown as shutdown, controller_handoff, flow_engine
from apps.oa_finance_preaudit import host_chat_review as host, item_failure, reconciliation
from tests.finance_preaudit.test_app_review_shutdown import proofs
from tests.finance_preaudit.test_flow_engine_general_expense import _material
from tests.finance_preaudit.test_host_chat_review import exchange, native_call, seal_execution, start


@pytest.mark.parametrize('workflow', ['general_expense', 'small_marketing_fee'])
def test_public_validator_accepts_business_wording_without_fixed_phrases(tmp_path, workflow):
    def collect(request, _attempt):
        material = _material(request)
        material['workflow_key'] = workflow
        material['declared_fields'].append({'name': '预算科目', 'value': '普通推广费'})
        return material

    engine = flow_engine.FlowEngine(root=tmp_path,
        discover_candidates=lambda key: [{'request_id': 'offline', 'received_at': '2026-10-21', 'workflow_key': key}],
        collect_material=collect)
    action = engine.next_action(workflow)
    packet = json.loads(Path(action['review_packet_file']).read_text())
    items = [{'item_key': item['item_key'], 'status': 'uncertain', 'conclusion': '已有材料可读，具体费用归属请财务确认。',
              'evidence_refs': [], 'missing_evidence': ['具体费用归属']} for item in packet['items']]
    for item in items:
        if item['item_key'] in {'budget', 'payment_company_consistency'}:
            item.update(status='pass', conclusion='本阶段暂不核对年度预算。' if item['item_key'] == 'budget' else '三方公司名称一致，建议使用对应账户付款。',
                        evidence_refs=['material:declared_fields.0'], missing_evidence=[])
        if item['item_key'] == 'attachment_payment_evidence':
            item.update(status='fail', conclusion='实付0元，收货后将支付80元。', evidence_refs=['material:declared_fields.0'], missing_evidence=[])
    decision = {'schema_version': 1, 'kind': 'preaudit_review_decision', 'action_token': action['action_token'],
                'workflow_key': workflow, 'rule_version': packet['rule_version'], 'overall': 'needs_human', 'items': items}
    engine.validate_review_decision(Path(action['action_file']), decision)


@pytest.mark.parametrize('declaration,render', [('let', 'text(published);'), ('const', 'text (JSON . stringify ( published ));'), ('var', 'text(published);')])
def test_public_submission_accepts_equivalent_native_result_wrappers(exchange, declaration, render):
    _, draft, owner, child, *_ = exchange
    thread, _results, request = start(exchange)
    command = host.draft_command(request, draft, json.loads(draft.read_text()), owner=owner)
    output = json.loads(child.read_text().splitlines()[2])['payload']['output']
    args = json.dumps({'cmd': command, 'max_output_tokens': 2000})
    if declaration == 'var':
        args = '{\nmax_output_tokens:2000,\ncmd:' + json.dumps(command) + ',\n}'
    code = '// @exec: {"yield_time_ms":1000}\n' + f'{declaration} published = await\n tools . exec_command ( ' + args + ' );\n' + render
    line = native_call(child, 'functions.exec', code, 'alternate-publication', output)
    try:
        assert host.submit(request, draft, owner=owner, source=child, source_line=line)['status'] == 'published'
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('prior_count', [0, 4])
def test_shutdown_accepts_equivalent_wait_wrapper(exchange, prior_count):
    _, _, owner, child, _, manifest, _ = exchange
    shutdown.begin(owner=owner, reason='completed')
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    args = proofs(owner, child)
    rows = args['source'].read_text().splitlines()
    call = json.loads(rows[args['wait_line'] - 1])
    prefix = 'text(await tools.exec_command({cmd:"true"}));' * prior_count
    call['payload']['input'] = prefix + 'let completed = await\n tools . write_stdin ( {chars:"",session_id:"original-session",} ); text (completed);'
    rows[args['wait_line'] - 1] = json.dumps(call)
    output = json.loads(rows[args['wait_line']])
    output['payload']['output'] = json.dumps([*({'exit_code': 0, 'output': 'other command'} for _ in range(prior_count)), json.loads(output['payload']['output'])])
    rows[args['wait_line']] = json.dumps(output)
    args['source'].write_text('\n'.join(rows) + '\n')
    assert shutdown.finish(owner=owner, **args)['status'] == 'verified'


def test_historical_audit_gap_does_not_block_a_fresh_execution(exchange, monkeypatch):
    _, _, owner, _, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='failed', children_reaped=True, health='degraded')
    path = host.registry_file(owner)
    original = path.read_bytes()
    monkeypatch.setattr(runtime_control, "controller_active", lambda: False)
    host.assert_previous_rounds_closed(owner=owner)
    host.assert_previous_rounds_closed(owner=str(uuid4()))
    assert path.read_bytes() == original
    assert 'shutdown' not in json.loads(original)


@pytest.mark.parametrize('active', ['controller_active', 'writer_active', 'active_request_writers'])
def test_current_activity_still_blocks_a_fresh_execution(exchange, monkeypatch, active):
    _, _, owner, _, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    monkeypatch.setattr(runtime_control, "controller_active", lambda: False)
    monkeypatch.setattr(runtime_control, active, lambda: True)
    with pytest.raises(ValueError, match='previous_round_incomplete'):
        host.assert_previous_rounds_closed(owner=owner)


@pytest.mark.parametrize('code', ['attachment_content_incomplete', 'legacy_doc_conversion_failed', 'material_budget_exceeded'])
def test_material_failure_leaves_the_next_fifo_item_reviewable(tmp_path, code):
    candidates = [{'request_id': str(index), 'received_at': f'2026-10-21T09:0{index}:00+08:00',
                   'workflow_key': 'general_expense'} for index in range(2)]

    def collect(request, _attempt):
        if request == '0':
            raise ValueError(code)
        return _material(request)

    engine = flow_engine.FlowEngine(root=tmp_path, discover_candidates=lambda _key: candidates, collect_material=collect)
    failed = engine.next_action('general_expense')
    assert failed['action'] == 'needs_admin'
    report = reconciliation.reconcile(tmp_path, pending_rows=candidates, handled_rows=[])
    assert report['continuation']['status'] == 'ready'
    assert report['quarantine']['pending_quarantined'] == 1
    following = engine.next_action('general_expense')
    assert following['action'] == 'review_required', following
    assert json.loads(Path(following['state_file']).read_text())['request_id'] == '1'
    assert not runtime_control.stop_requested()


@pytest.mark.parametrize('damage', [None, 'writer', 'changed_receipt'])
def test_historical_material_scope_is_recomputed_without_rewriting_evidence(tmp_path, damage):
    def collect(_request, _attempt):
        raise ValueError('attachment_content_incomplete')

    candidate = {'request_id': 'offline-old', 'received_at': '2026-10-21', 'workflow_key': 'general_expense'}
    engine = flow_engine.FlowEngine(root=tmp_path, discover_candidates=lambda _key: [candidate], collect_material=collect)
    failed = engine.next_action('general_expense')
    attempt = Path(failed['action_file']).parent
    path = attempt / item_failure.RECEIPT_FILE
    receipt = json.loads(path.read_text())
    receipt['scope'] = 'controller'
    path.write_text(json.dumps(receipt))
    if damage == 'writer':
        (attempt / 'writer-boundary.json').write_text('{}')
    elif damage == 'changed_receipt':
        path.write_text('{}')
    before = {file: file.read_bytes() for file in attempt.iterdir() if file.is_file()}
    report = reconciliation.reconcile(tmp_path, pending_rows=[candidate], handled_rows=[])
    assert report['continuation']['status'] == ('ready' if damage is None else 'blocked')
    if damage is None:
        assert report['manual_review']['items'][0]['scope'] == 'item'
    assert {file: file.read_bytes() for file in before} == before
