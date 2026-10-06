"""Native contract checks are mechanism evidence, not business acceptance."""
from __future__ import annotations

import json
import shutil

import pytest

from scripts import quality


def test_missing_native_cli_is_not_verified_and_blocks_reuse(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    record = tmp_path / 'native-contract.json'
    monkeypatch.setenv('PATH', '')
    assert quality.main(['native-contract', '--record', str(record)]) == 1
    result = json.loads(record.read_text())
    assert result['status'] == 'not_verified'
    assert result['reason'] == 'native_cli_unavailable'
    assert result['paid_dispatch_ready'] is False


@pytest.fixture(scope='module')
def native_record(tmp_path_factory):
    if shutil.which('codex') is None:
        pytest.skip('fixed native CLI unavailable; the quality entry records not_verified')
    root = tmp_path_factory.mktemp('native-contract')
    root.chmod(0o700)
    record = root / 'native-contract.json'
    assert quality.main(['native-contract', '--record', str(record)]) == 0
    return record


def test_real_native_fallback_reaches_worker_preview_with_all_original_fields(native_record):
    result = json.loads(native_record.read_text())
    case = result['cases']['fallback']
    assert case['model_context']['exact_stdout'] is True
    assert case['model_context']['field_rows_matched'] == 798
    assert case['model_context']['truncated'] is False
    assert case['preview'] == 'verified_closed'
    assert case['consumer_preserved_original_object'] is True
    assert case['process_state'] == 'exited'
    assert result['paid_dispatch_ready'] is True


def test_fallback_images_reach_next_request_in_original_encoded_order(native_record):
    case = json.loads(native_record.read_text())['cases']['fallback']
    assert case['tool_images_match_fixture'] is True
    assert len(case['tool_image_sha256']) == 2
    assert case['tool_image_sha256'][0] != case['tool_image_sha256'][1]
    assert case['rollout_tool_image_sha256'] == case['tool_image_sha256']


def test_missing_fallback_image_proof_cannot_be_reused(native_record, tmp_path):
    from apps.oa_finance_preaudit import native_review_contract

    value = json.loads(native_record.read_text())
    value['cases']['fallback']['tool_images_match_fixture'] = False
    value['cases']['fallback']['tool_image_sha256'] = []
    record = tmp_path / 'missing-fallback-image.json'
    record.write_text(json.dumps(value))
    record.chmod(0o600)
    assert native_review_contract.readiness(record)['status'] == 'not_verified'


def test_default_history_truncation_is_caught_despite_complete_cli_stdout(native_record):
    case = json.loads(native_record.read_text())['cases']['default_truncation']
    assert case['cli_stdout_complete'] is True
    assert case['model_context']['exact_stdout'] is False
    assert case['model_context']['truncated'] is True
    assert case['model_context']['rollout_matches_next_request'] is True
    assert case['status'] == 'not_verified'
    assert case['paid_dispatch_ready'] is False


def test_direct_and_other_workflows_keep_default_and_illegal_original_is_rejected(native_record):
    cases = json.loads(native_record.read_text())['cases']
    direct = cases['direct']
    assert direct['history_limit'] is None
    assert direct['inline_input_complete'] is True
    assert direct['initial_images_match_fixture'] is True
    assert direct['preview'] == 'verified_closed'
    assert cases['other_workflow']['history_limit'] is None
    assert cases['other_workflow']['preview'] == 'verified_closed'
    invalid = cases['invalid_json']
    assert invalid['invalid_original_rejected'] is True
    assert invalid['preview'] == 'rejected'
    assert invalid['native_raw_equals_vendor'] is True


def test_only_current_bound_native_proof_can_be_reused(native_record, tmp_path):
    from apps.oa_finance_preaudit import native_review_contract

    assert quality.main(['native-contract', '--record', str(native_record), '--reuse']) == 0
    original = json.loads(native_record.read_text())
    for changed in ('cli_sha256', 'source_files', 'call_profiles'):
        copy = json.loads(native_record.read_text())
        copy['binding'][changed] = 'changed'
        target = tmp_path / (changed + '.json')
        target.write_text(json.dumps(copy))
        target.chmod(0o600)
        assert native_review_contract.readiness(target)['paid_dispatch_ready'] is False
    copy = json.loads(native_record.read_text())
    copy['artifacts'][str(tmp_path / 'missing-native-evidence')] = '0' * 64
    target = tmp_path / 'changed-data.json'
    target.write_text(json.dumps(copy))
    target.chmod(0o600)
    assert native_review_contract.readiness(target)['status'] == 'not_verified'
    assert json.loads(native_record.read_text()) == original
