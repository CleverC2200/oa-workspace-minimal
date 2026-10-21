import json
import time

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle
from apps.oa_finance_preaudit import app_review_shutdown as shutdown, controller_handoff, host_chat_review as host
from tests.finance_preaudit.test_app_review_shutdown import proofs
from tests.finance_preaudit.test_host_chat_review import exchange, native_call, seal_execution


def packaging_round(exchange):
    action, _, owner, child, _, manifest, _ = exchange
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    receipt = seal_execution(owner, manifest)
    host.enter_packaging(owner=owner)
    request = host.publish(action, prompt='Observe one package page.', timeout_seconds=60, kind='packaging_draft')
    return owner, child, controller_handoff._read_manifest(), receipt, request


def heartbeat_notice(monkeypatch, owner, child, index, *, source_owner=None, code='rate_limit', cli=False):
    source = f'main:{source_owner or owner}:packaging-{index}'
    stamp = time.time()
    monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter([(code, source, stamp)]))
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, f'packaging-heartbeat-{index}', {})
    run_id = controller_handoff._read_manifest()['run_id']
    if cli:
        return host.main(['--operation', 'heartbeat', '--include-pending', '--owner-task-id', owner,
            '--run-id', run_id, '--source-session-file', str(parent), '--source-line', str(line)])
    return host.heartbeat(owner=owner, source=parent, source_line=line, run_id=run_id)


def test_packaging_heartbeat_shares_finance_budget_and_third_error_returns_stop(exchange, monkeypatch, capsys):
    _, _, owner, child, _, manifest, _ = exchange
    stamp = time.time()
    monkeypatch.setattr(guard, 'project_official_errors', lambda _: iter([
        ('rate_limit', f'main:{owner}:finance-failure', stamp)]))
    guard.observe_project_errors(runtime_control.PROJECT_ROOT)
    owner, child, completed, receipt, request = packaging_round(exchange)
    original_manifest = controller_handoff.manifest_file().read_bytes()
    original_receipt = receipt.read_bytes()
    reviewer = host.registered_reviewers(owner=owner)[0]['reviewer_task_id']
    second = heartbeat_notice(monkeypatch, owner, child, 2, source_owner=reviewer, code='server_overloaded')
    assert second['transient_retries']['failures'] == 2
    assert second['transient_retries']['retry_allowed'] is True
    guard.observe_project_errors(runtime_control.PROJECT_ROOT, owner_task_id=owner)
    assert guard.retry_budget(manifest['run_id'])['failures'] == 2
    assert not runtime_control.stop_requested()
    assert heartbeat_notice(monkeypatch, owner, child, 3, cli=True) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped['transient_retries']['failures'] == 3
    assert stopped['stop_reason'] == guard.RETRY_STOP_REASON
    assert stopped['requests'] == [] and stopped['round']['ready'] is False
    assert stopped['round']['capacity'] == 0
    assert guard.cooldown() is None
    assert controller_handoff._read_manifest() == completed
    assert controller_handoff.manifest_file().read_bytes() == original_manifest
    assert receipt.read_bytes() == original_receipt
    assert not (lifecycle.run_directory(manifest['run_id']) / 'closed-readonly.json').exists()
    with pytest.raises(ValueError, match='stop_requested'):
        host.publish_draft(request, request.with_name('late-decision.json'), {}, owner=owner)


@pytest.mark.parametrize('manual_stop', [False, True])
def test_packaging_retry_stop_clears_only_after_verified_shutdown(exchange, monkeypatch, manual_stop):
    owner, child, manifest, receipt, request = packaging_round(exchange)
    original_manifest = controller_handoff.manifest_file().read_bytes()
    original_receipt = receipt.read_bytes()
    for index in range(3):
        heartbeat_notice(monkeypatch, owner, child, index)
    with pytest.raises(ValueError, match='shutdown_incomplete'):
        lifecycle.clear_packaging_retry_stop(manifest, owner=owner)
    assert runtime_control.stop_reason() == guard.RETRY_STOP_REASON
    shutdown.begin(owner=owner, reason='reviewer_failed', run_id=manifest['run_id'])
    with pytest.raises(ValueError, match='children_not_idle'):
        shutdown.finish(owner=owner, run_id=manifest['run_id'], **proofs(owner, child, status='running'))
    assert runtime_control.stop_reason() == guard.RETRY_STOP_REASON
    if manual_stop:
        runtime_control.request_stop()
    result = shutdown.finish(owner=owner, run_id=manifest['run_id'], **proofs(owner, child))
    assert result['status'] == 'verified'
    assert runtime_control.stop_requested() is manual_stop
    assert guard.cooldown() is None
    assert controller_handoff.manifest_file().read_bytes() == original_manifest
    assert receipt.read_bytes() == original_receipt
    assert shutdown.sealed_round(owner=owner, run_id=manifest['run_id'])['shutdown']['status'] == 'verified'
    with pytest.raises(ValueError, match='stop_requested' if manual_stop else 'parent_lost_or_closed'):
        host.publish_draft(request, request.with_name('late-decision.json'), {}, owner=owner)


def test_packaging_unreadable_official_history_preserves_safety_stop(exchange, monkeypatch):
    owner, child, _, _, _ = packaging_round(exchange)
    def unavailable(*_args, **_kwargs):
        raise ValueError('approval_official_history_unavailable')
    monkeypatch.setattr(guard, 'observe_project_errors', unavailable)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'unreadable-history', {})
    with pytest.raises(ValueError, match='official_history_unavailable'):
        host.heartbeat(owner=owner, source=parent, source_line=line)
    assert runtime_control.stop_reason() == 'codex_review_technical_failure'
    assert guard.cooldown() is None
