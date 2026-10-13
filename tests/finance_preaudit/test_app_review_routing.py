from pathlib import Path

import pytest

from apps.oa_finance_preaudit import production_config, controller_handoff, codex_review_worker
from apps.oa_approval import runtime_control


def test_default_openai_round_uses_app_transport_without_claiming_model(monkeypatch):
    monkeypatch.delenv('OA_REVIEW_PROVIDER', raising=False)
    monkeypatch.delenv('OA_REVIEW_TRANSPORT', raising=False)
    assert production_config.review_settings() == {
        'provider': 'openai', 'model': 'host-current-chat', 'reasoning_effort': 'unknown',
        'model_provider': 'openai', 'requested_service_tier': 'unknown', 'transport': 'host_chat',
    }


def test_openai_cli_selection_is_rejected_and_never_falls_back(monkeypatch):
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'cli')
    with pytest.raises(ValueError, match='openai_review_cli_forbidden'):
        production_config.review_settings()


def test_direct_review_execution_cannot_launch_openai_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_review_worker.subprocess, 'Popen',
                        lambda *a, **k: pytest.fail('OpenAI CLI launched'))
    result = codex_review_worker._execute_codex_raw(
        tmp_path, tmp_path / 'missing-action.json', 60, read_only=True, stderr_summary={})
    assert result.error_code == 'openai_review_cli_forbidden'
    assert result.process_state == 'not_started'


def test_real_controller_manifest_accepts_app_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'controller.lock'))
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'review_configuration': production_config.review_settings('openai', transport='host_chat')})
        assert controller_handoff.status()['status'] == 'active'
        controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
        assert controller_handoff.status()['manifest_state'] == 'completed'


@pytest.mark.parametrize('mutation', [None, 'missing_transport', 'invented_effort', 'fake_process'])
def test_app_timing_is_read_by_real_flow_completion(tmp_path, mutation):
    import json
    from apps.oa_finance_preaudit import flow_engine
    from tests.finance_preaudit.test_flow_engine import _travel_material, _passing_decision
    engine = flow_engine.FlowEngine(
        root=tmp_path / 'runtime',
        discover_candidates=lambda key: [{'request_id':'recorded-request',
            'received_at':'2026-08-22T09:00:00+08:00', 'workflow_key':key}],
        collect_material=lambda request, _: _travel_material(request))
    action = engine.next_action('travel_expense')
    action_path = Path(action['action_file'])
    packet = json.loads(Path(action['review_packet_file']).read_text())
    timing = {'schema_version':1, 'kind':'preaudit_agent_timing',
              'action_token':action['action_token'], 'workflow_key':'travel_expense',
              **production_config.HOST_REVIEW_IDENTITY,
              'wall_seconds':12, 'process_attempts':0, 'token_usage':{'status':'unknown'}}
    if mutation == 'missing_transport': timing.pop('transport')
    if mutation == 'invented_effort': timing['reasoning_effort'] = 'medium'
    if mutation == 'fake_process': timing['process_attempts'] = 1
    action_path.with_name('agent-timing.json').write_text(json.dumps(timing))
    decision_path = tmp_path / 'decision.json'
    decision_path.write_text(json.dumps(_passing_decision(action, packet)))
    completed = engine.complete_review(action_path, decision_path)
    if mutation is None:
        assert completed['action'] == 'closed', completed
    else:
        assert completed['reason'] == 'agent_timing_invalid', completed


def test_readback_preserves_verified_execution_owners_for_app_registration(tmp_path, monkeypatch):
    import json
    import os
    from apps.oa_finance_preaudit import approval_guard
    from scripts.finance_preaudit_run import read_execution
    from tests.finance_preaudit.test_execution_readback import recorded_execution
    path, owner = recorded_execution(tmp_path, monkeypatch)
    receipt = json.loads(path.read_text())
    process = approval_guard.process_identity(os.getpid())
    receipt['owner_processes'] = [process]
    path.write_text(json.dumps(receipt))
    result = read_execution(path, owner_task_id=owner, invocation_id=path.parent.name)
    assert result['owner_processes'] == [process]
    receipt['owner_processes'] = [{'pid':process['pid']}]
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='controller_execution_evidence_failed'):
        read_execution(path, owner_task_id=owner, invocation_id=path.parent.name)


def test_historical_openai_cli_receipt_is_readable_with_app_default(tmp_path, monkeypatch):
    import hashlib
    import json
    from scripts.finance_preaudit_run import read_execution
    from tests.finance_preaudit.test_execution_readback import recorded_execution
    path, owner = recorded_execution(tmp_path, monkeypatch)
    receipt = json.loads(path.read_text())
    mirror = path.with_name('controller-manifest.json')
    manifest = json.loads(mirror.read_text())
    manifest['review_configuration'] = production_config.LEGACY_OPENAI_IDENTITY
    mirror.write_text(json.dumps(manifest))
    receipt['review_configuration'] = production_config.LEGACY_OPENAI_IDENTITY
    receipt['manifest_sha256'] = hashlib.sha256(mirror.read_bytes()).hexdigest()
    path.write_text(json.dumps(receipt))
    result = read_execution(path, owner_task_id=owner, invocation_id=path.parent.name)
    assert result['status'] == 'completed'
    assert result['review_configuration']['model'] == 'gpt-6.1-sol'


@pytest.mark.parametrize('provider,transport,error', [
    ('openai','cli','openai_review_cli_forbidden'),
    ('deepseek','host_chat','host_review_provider_mismatch'),
])
def test_invalid_transport_stops_before_controller_start(provider, transport, error, monkeypatch, capsys):
    from uuid import uuid4
    from scripts import finance_preaudit_run
    monkeypatch.setattr(finance_preaudit_run.auth, 'load_local_env', lambda: None)
    monkeypatch.setattr(finance_preaudit_run.subprocess, 'Popen',
                        lambda *a, **k: pytest.fail('invalid selection launched controller'))
    assert finance_preaudit_run.main(['--owner-task-id', str(uuid4()), '--review-provider', provider,
                                     '--review-transport', transport, '--','--rolling']) == 1
    import json
    assert json.loads(capsys.readouterr().out)['error_code'] == 'controller_execution_evidence_failed'
