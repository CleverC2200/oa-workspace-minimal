from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apps.oa_approval import overseas_invoice
from apps.oa_finance_preaudit import cli
from tests.finance_preaudit.test_host_chat_review import exchange


def bound_file(path: Path, payload: dict) -> dict:
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def no_live(_root):
    raise AssertionError('file summary must not query OA or runtime status')


def test_file_summary_retains_overseas_unknown_and_excludes_only_explicit_scope(tmp_path, capsys):
    workflows = [
        {'workflow_id': overseas_invoice.WORKFLOW_ID, 'workflow_name': overseas_invoice.WORKFLOW_NAME,
         'flow_key': '', 'support_status': 'unsupported_workflow', 'count': 1},
        {'workflow_id': 'excluded-rd', 'workflow_name': '研发费用',
         'flow_key': '', 'support_status': 'unsupported_workflow', 'count': 6},
        {'workflow_id': 'excluded-conference', 'workflow_name': '会务费用',
         'flow_key': '', 'support_status': 'unsupported_workflow', 'count': 1},
    ]
    status = {'schema_version': 1, 'kind': 'preaudit_runtime_status',
        'generated_at': '2026-10-01T12:00:00+08:00',
        'live_check': {'schema_version': 1, 'kind': 'preaudit_live_status',
            'oa': {'execution_identity': 'verified', 'pending_total': 8,
                   'supported': 0, 'unsupported': 8, 'by_workflow': {}, 'workflow_counts': workflows}},
        'reconciliation': {'schema_version': 1, 'kind': 'preaudit_reconciliation', 'read_only': True,
            'quarantine': {'actionable_pending': 0, 'pending_quarantined': 0},
            'continuation': {'status': 'ready', 'blocking_reasons': {}}, 'pending_dispositions': {'items': []}},
        'controller_handoff': {'ready_for_start': False, 'reason': 'original_invocation_unsealed'}}
    unknown = {'schema_version': 1, 'kind': 'overseas_invoice_result',
               'capability_status': {'no_invoice_submit': {'implementation': 'available',
                   'authorization': 'enabled', 'deployment': 'unknown', 'business_acceptance': 'not_verified'}},
               'unknown_dispositions': [{'attempt': 'a' * 32, 'receipt_status': 'unknown',
                                         'next_action': 'readonly_postcheck_or_manual_reconciliation'}]}
    manifest = {'status_file': bound_file(tmp_path / 'status.json', status),
        'overseas_status_file': bound_file(tmp_path / 'overseas.json', unknown),
        'excluded_workflows': [{k: row[k] for k in ('workflow_id', 'workflow_name')} for row in workflows[1:]],
        'completed': {'owner_task_id': '00000000-0000-0000-0000-000000000001', 'run_ids': ['0' * 32]}}
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps(manifest))
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pending']['finance'] == 0
    assert result['pending']['overseas'] == 1
    assert result['pending']['out_of_scope'] == 7
    assert result['history_unknown']['overseas'] == 1
    assert result['all_clear'] is False
    assert result['completed']['counts']['completed'] == 0
    assert result['completed']['counts']['invalid'] == 1
    assert result['cron']['status'] == 'unknown'
    assert result['startup_authorized'] is False
    assert result['snapshot']['generated_at'] == status['generated_at']
    assert result['snapshot']['currentness'] == 'not_rechecked'
    assert result['controller_handoff'] == status['controller_handoff']
    assert result['capability_status']['no_invoice_submit']['business_acceptance'] == 'not_verified'
    assert result['sources']['status_file'] == manifest['status_file']


def test_missing_file_inputs_never_report_zero_or_readiness(tmp_path, capsys):
    source = tmp_path / 'inputs.json'
    source.write_text('{}')
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result['pending'].values()) == {'unknown'}
    assert result['history_unknown'] == {'finance': 'unknown', 'overseas': 'unknown'}
    assert result['completed']['counts'] is None
    assert result['all_clear'] is False
    assert result['business_acceptance'] == 'not_verified'


def test_mutated_source_is_rejected_and_no_live_fallback_occurs(tmp_path, capsys):
    snapshot = tmp_path / 'status.json'
    metadata = bound_file(snapshot, {'schema_version': 1, 'kind': 'preaudit_runtime_status'})
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps({'status_file': metadata}))
    snapshot.write_text('{}')
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['failure']['code'] == 'status_summary_source_invalid'
    assert 'pending' not in result


def test_unclassified_unsupported_is_not_subtracted_as_out_of_scope(tmp_path, capsys):
    status = {'schema_version': 1, 'kind': 'preaudit_runtime_status',
        'generated_at': '2026-10-01T12:00:00+08:00',
        'live_check': {'schema_version': 1, 'kind': 'preaudit_live_status',
                       'oa': {'execution_identity': 'verified', 'pending_total': 2,
            'supported': 1, 'unsupported': 1, 'workflow_counts': [
                {'workflow_id': 'packaging-flow', 'workflow_name': 'packaging',
                 'flow_key': 'packaging_layout_approval', 'support_status': 'supported', 'count': 1},
                {'workflow_id': 'unknown-flow', 'workflow_name': 'other',
                 'flow_key': '', 'support_status': 'unsupported_workflow', 'count': 1}]}}}
    manifest = {'status_file': bound_file(tmp_path / 'status.json', status)}
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps(manifest))
    args = ['status', '--root', str(tmp_path), '--summary-input-file', str(source)]
    assert cli.main(args, runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pending'] == {'finance': 0, 'packaging': 1, 'overseas': 0,
                                 'out_of_scope': 0, 'unclassified': 1}
    manifest['excluded_workflows'] = [{'workflow_id': 'packaging-flow', 'workflow_name': 'packaging'}]
    source.write_text(json.dumps(manifest))
    assert cli.main(args, runtime_status=no_live) == 1
    assert json.loads(capsys.readouterr().out)['failure']['code'] == 'status_summary_scope_invalid'


def test_cron_file_reports_config_snapshot_without_changing_acceptance(tmp_path, capsys):
    cron = tmp_path / 'automation.toml'
    raw = b'kind = "cron"\nstatus = "PAUSED"\n'
    cron.write_bytes(raw)
    manifest = {'cron_file': {'path': str(cron), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}}
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps(manifest))
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['cron'] == {'status': 'unknown', 'currentness': 'not_rechecked',
                              'provided_configuration': {'id': None, 'status': 'PAUSED'}}
    assert result['deployment'] == 'unknown'
    assert result['business_acceptance'] == 'not_verified'
    assert cron.read_bytes() == raw


def test_caller_cannot_supply_completion_counts(tmp_path, capsys):
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps({'completed': {
        'owner_task_id': '00000000-0000-0000-0000-000000000001', 'run_ids': ['0' * 32],
        'counts': {'completed': 24}}}))
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 1
    assert json.loads(capsys.readouterr().out)['failure']['code'] == 'status_summary_input_invalid'


@pytest.mark.parametrize("controlled", [True, False])
def test_file_summary_uses_canonical_receipts_instead_of_supplied_totals(exchange, tmp_path, capsys, controlled):
    from tests.finance_preaudit.test_completed_receipts import completed_case

    root, owner, run, *_ = completed_case(exchange, tmp_path, controlled=controlled)
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps({'completed': {'owner_task_id': owner, 'run_ids': [run, run]}}))
    original = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert cli.main(['status', '--root', str(root), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['completed']['counts'] == {'completed': int(controlled), 'excluded': int(not controlled),
                                              'duplicate': 0, 'invalid': 0}
    assert result['completed']['selected_rounds'] == 1
    assert result['completed']['evidence_status'] == 'canonical_revalidation'
    assert result['completed']['business_acceptance'] == 'not_verified'
    assert all(path.read_bytes() == raw for path, raw in original.items())


def test_truncated_finance_unknown_history_remains_unknown(tmp_path, capsys):
    source = tmp_path / 'inputs.json'
    status = {'schema_version': 1, 'kind': 'preaudit_runtime_status',
        'generated_at': '2026-10-01T12:00:00+08:00',
        'reconciliation': {'manual_review': {'read_only': True, 'total': 1,
                            'items': [], 'visible': 0, 'display_truncated': True}}}
    source.write_text(json.dumps({'status_file': bound_file(tmp_path / 'status.json', status)}))
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['history_unknown']['finance'] == 'unknown'


def test_other_cron_identity_cannot_be_reported_as_oa_cron(tmp_path, capsys):
    cron = tmp_path / 'other.toml'
    raw = b'id = "other-job"\nkind = "cron"\nstatus = "ACTIVE"\n'
    cron.write_bytes(raw)
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps({'cron_file': {'path': str(cron), 'bytes': len(raw),
                                               'sha256': hashlib.sha256(raw).hexdigest()}}))
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['cron']['status'] == 'unknown'
    assert result['cron']['provided_configuration'] == {'id': 'other-job', 'status': 'ACTIVE'}
    assert result['startup_authorized'] is False


def test_unknown_source_metadata_fields_are_rejected_without_echo(tmp_path, capsys):
    metadata = bound_file(tmp_path / 'status.json', {'schema_version': 1, 'kind': 'preaudit_runtime_status'})
    metadata['unexpected_private_field'] = 'private-content'
    source = tmp_path / 'inputs.json'
    source.write_text(json.dumps({'status_file': metadata}))
    assert cli.main(['status', '--root', str(tmp_path), '--summary-input-file', str(source)],
                    runtime_status=no_live) == 1
    raw = capsys.readouterr().out
    assert 'private-content' not in raw
    assert json.loads(raw)['failure']['code'] == 'status_summary_source_invalid'
